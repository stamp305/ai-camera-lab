"""AI Camera service: the traffic feeds branch clients send.

Traffic feeds (see .scratch/ai-camera-traffic-feed/spec.md): the three feeds globalapi2's
`api_traffic_analytic` used to write now arrive here and go straight to api_data @209, on
the same tables every existing reader keys on — docs/adr/0003. They only shadow globalapi2
until cutover, and the fourth feed, Zone Hour, goes to a local SQLite file — docs/adr/0010.

Served by the admin backend, which mounts `app` at /ai-camera-service
(app_ai_camera_backend.py, docs/adr/0009). It has no process of its own.
"""
import logging
import os
import sqlite3
from collections import Counter, namedtuple
from contextlib import closing, contextmanager
from datetime import datetime, timedelta
from typing import List, Literal, Optional

from fastapi import FastAPI, Header
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

DT_FMT = "%Y-%m-%d %H:%M:%S"           # Thai local time, matching DB `timestamps`
AVG_TABLE = "shoping_traffic_avg"      # where the Staffing Ratio feed lands
HOTZONE_TABLE = "shoping_traffic_hotzone"          # and the four the Hotzone Round fans out to
MONO_TABLE = "shoping_traffic_hotzone_mono"        # people per product zone, keyed mono_id
DEMO_TABLE = "emp_demo"                            # demos per zone, keyed demo_id
TIER_TABLE = "demo_tier"                           # the Mac zone's demos by tier, keyed id
ANALYTIC_TABLE = "shoping_traffic_analytic"   # where the Traffic Row feed lands
TRAFFIC_ROW_HOUR = timedelta(hours=1)   # a Traffic Row counts the hour after its timestamp
ZONE_HOUR_TABLE = "zone_hour"   # in the SQLite file at ZONE_HOUR_DB_PATH, not on 209 (ADR 0010)
# Which Hotzone Round body fields belong to which child table, in the PHP's column order.
# plan_hotzone writes from these, and parity_check.py folds a stored child row back into a
# body with them, so planner and verifier cannot drift apart. tests/test_traffic_hotzone.py
# keeps a list of its own on purpose — asserting the order against the tuple that produced
# it would prove nothing.
MONO_FIELDS = tuple(f"{n}_zone{i}" for i in range(1, 6) for n in ("name", "count"))
DEMO_FIELDS = (tuple(f"{n}_zone{i}_mono" for i in range(1, 6) for n in ("name", "count"))
               + ("name_zone_ce", "count_zone_ce"))
TIER_FIELDS = tuple(f"count_zone4_tier{i}" for i in (1, 2, 3))


class TrafficHeader(BaseModel):
    """The fields every traffic feed carries, and the three checks of spec §Validation.

    `branch` must be eight digits because /traffic/in-pass interpolates it as a POS schema
    name — the rule lives here so no feed can be added without it. `branch` and `timestamp`
    are the only fields not named after their columns (branch_cuscode, timestamps).

    `timestamp` comes back in its canonical spelling: strptime also takes "2026-10-15 10:0:0",
    and the Zone Hour file keys its resend on the raw text, so a differently spelled resend
    would add a second copy of the hour instead of replacing it. On the 209 feeds this
    changes nothing stored — their DATETIME columns normalise the string anyway.
    """
    branch: str = Field(pattern=r"^\d{8}$")
    region_id: str
    region_name: str
    province_id: int
    province_name: str
    timestamp: str
    zone_type: Literal["IT", "MONO"]

    @field_validator("timestamp")
    @classmethod
    def _parses_as_thai_local_time(cls, value: str) -> str:
        # ValueError → 400, raised before any SQL runs
        return datetime.strptime(value, DT_FMT).strftime(DT_FMT)


class OldFeedRequest(TrafficHeader):
    """The three feeds that write to 209.

    `rollout_no_globalapi2` exists only for the move off globalapi2: a branch's DLC sets it once
    it has stopped sending there, and only then is its body stored, because a client sending
    to both would make two writers race on a key with no unique index. Once every branch has
    moved, make storing the default and drop the field.
    """
    rollout_no_globalapi2: bool = False


class StaffingRatioRequest(OldFeedRequest):
    """One Staffing Ratio sample → shoping_traffic_avg. No day_of_week: the PHP never
    stored one for this feed.

    `hour` stays the string the client already sends ("09"), so its zero-padding reaches
    the column untouched whatever that column's type turns out to be.
    """
    hour: str
    avg_emp: float
    avg_cus: float
    rate: float


class HotzoneRequest(OldFeedRequest):
    """One Hotzone Round → four linked tables (spec §Hotzone fan-out).

    Every field name below is its destination column, so the planner can map a group of
    them to a table by name alone; the client does the renaming its side (count_charged →
    count_active, nb_t_apple → nb_to_apple, count_learning_hub → count_learnhub).

    `hour` and `day_of_week` stay the strings the client already sends ("09", "Tuesday"),
    as on the Staffing Ratio feed. Every field is required: the PHP defaulted an absent
    one to 0/'', but the client sends the full set on every round.
    """
    day_of_week: str
    hour: str
    count_in: int
    count_apple: int
    count_nb: int
    nb_to_apple: int
    apple_to_nb: int
    count_active: int
    count_not_active: int
    count_learnhub: int
    engagement_wait: float
    # → shoping_traffic_hotzone_mono: people counted in each of the five product zones
    name_zone1: str
    count_zone1: int
    name_zone2: str
    count_zone2: int
    name_zone3: str
    count_zone3: int
    name_zone4: str
    count_zone4: int
    name_zone5: str
    count_zone5: int
    # → emp_demo: demos given in each of those zones, plus the CE zone
    name_zone1_mono: str
    count_zone1_mono: int
    name_zone2_mono: str
    count_zone2_mono: int
    name_zone3_mono: str
    count_zone3_mono: int
    name_zone4_mono: str
    count_zone4_mono: int
    name_zone5_mono: str
    count_zone5_mono: int
    name_zone_ce: str
    count_zone_ce: int
    # → demo_tier: the Mac zone's demos split by tier
    count_zone4_tier1: int
    count_zone4_tier2: int
    count_zone4_tier3: int


class TrafficRowRequest(OldFeedRequest):
    """One Traffic Row → shoping_traffic_analytic. The five bill columns are NOT here:
    the service computes them from the branch's POS schema (spec §Bill columns).

    `hour` is an int because this feed's client sends one (`int(hour) - 1` in
    job_send_logs.aggregate_in_pass_data) — unlike the rate feed, which sends "09".
    pydantic still accepts the string form, so neither shape is refused.
    """
    day_of_week: str
    hour: int
    walk_pass: int
    walk_in: int
    rate: float


class ZoneHourZone(BaseModel):
    """One zone of a Zone Hour (CONTEXT.md): totals and counts, never averages, so a
    reader can add hours, zones and branches up before it divides."""
    zone_name: str
    dwell_sum_seconds: float = Field(ge=0)
    dwell_count: int = Field(ge=0)
    wait_sum_seconds: float = Field(ge=0)
    wait_count: int = Field(ge=0)
    long_wait_count: int = Field(ge=0)
    unserved_count: int = Field(ge=0)

    @field_validator("zone_name")
    @classmethod
    def _names_something(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("zone_name is empty")
        return value   # stored as sent, surrounding spaces included


# A branch has a handful of zones; the cap only bounds what one body can make us validate
# and write, since any Device Key holder can send one.
MAX_ZONES = 200


class ZoneHourRequest(TrafficHeader):
    """One hour of one branch + Zone Type's zones → one SQLite row per zone (ADR 0010).

    `hour` and `day_of_week` are stored as sent, with no check against `timestamp`, by
    decision. An empty `zones` is valid: it clears that hour.
    """
    day_of_week: str
    hour: int
    long_wait_seconds: float
    zones: List[ZoneHourZone] = Field(max_length=MAX_ZONES)

    @field_validator("zones")
    @classmethod
    def _one_entry_per_zone(cls, zones: List[ZoneHourZone]) -> List[ZoneHourZone]:
        counts = Counter(zone.zone_name for zone in zones)
        repeated = sorted(name for name, n in counts.items() if n > 1)
        if repeated:
            raise ValueError(f"zone_name sent more than once: {repeated}")
        return zones


# A child of uvicorn's error logger, not __name__: the admin backend configures no root
# logger, so only uvicorn's handlers (log_level="info") put a line in journalctl.
log = logging.getLogger("uvicorn.error.ai_camera_service")


def feeds_shadowed(req: "OldFeedRequest") -> bool:
    """Shadow mode for /traffic/rate, /in-pass and /hotzone (ADR 0010), read per request.

    Off for a body that says `rollout_no_globalapi2`, or for everyone with exactly "0". Unset, empty or a
    typo keeps globalapi2 the writer for a client that also sends to it, so a restart with a
    lost `.env` line can never start a second writer on 209.
    """
    return os.getenv("TRAFFIC_FEEDS_SHADOW") != "0" and not req.rollout_no_globalapi2


def shadow_answer(feed: str, req: TrafficHeader) -> dict:
    """The body was valid — pydantic already ran — so say so and touch no host at all.

    The one log line is how the operator sees which branches already dual-send. It names the
    body's key only: the Device Key and Authorization header never reach this app.
    """
    log.info("traffic shadow feed=%s branch=%s zone_type=%s timestamp=%s",
             feed, req.branch, req.zone_type, req.timestamp)
    return {"action": "shadow"}


# An operator's test on the box, on 127.0.0.1:8011: every read and the plan, whatever shadow
# mode says, then the answer instead of the write. A header, because the frontend forwards
# only the body and Content-Type, so a Branch's Device Key can never turn this on and read
# another Branch's bills. Zone Hour ignores it and writes.
DRY_RUN = Header(False, alias="X-Traffic-Dry-Run")


def dry_run_answer(feed: str, req: TrafficHeader, action: str, existing, **plan) -> dict:
    """What this body would have done. Nothing has been written when this is returned."""
    log.info("traffic dry run feed=%s branch=%s zone_type=%s timestamp=%s would=%s",
             feed, req.branch, req.zone_type, req.timestamp, action)
    return {"action": "dry_run", "would": action, "id": existing["id"] if existing else None,
            **plan}


DB_CONNECT_TIMEOUT = 10   # seconds; a host that will not answer must not hold a thread for long

# Built lazily, per connection: creds come from the .env the admin backend loads at startup.
DB_CONFIG = {
    "api_data": lambda: {"host": "10.10.10.209", "user": os.getenv("NEW_DB_USER"),
                         "password": os.getenv("NEW_DB_PASS"), "database": "api_data",
                         "connection_timeout": DB_CONNECT_TIMEOUT},
    # The POS host globalapi calls `db_branch`. No `database`: every table below is
    # schema-qualified with the branch code, which IS the schema name there.
    "pos": lambda: {"host": os.getenv("POS_DB_HOST"), "user": os.getenv("POS_DB_USER"),
                    "password": os.getenv("POS_DB_PASS"), "port": 3306,
                    "connection_timeout": DB_CONNECT_TIMEOUT},
}


def run_query(query: str, params: tuple = (), *, db: str = "api_data", fetch: str = "all",
              commit: bool = False):
    """Short-lived connection per statement, in the monitor_ai_camera run_query shape.

    Returns the rows read, or — with commit=True — the cursor's lastrowid, which is the
    new row's id after an INSERT.

    mysql.connector is imported here (not at module top) so tests can swap in a fake module.
    """
    import mysql.connector
    with closing(mysql.connector.connect(**DB_CONFIG[db]())) as conn:
        with closing(conn.cursor(dictionary=True)) as cursor:
            cursor.execute(query, params)
            if commit:
                conn.commit()
                return cursor.lastrowid
            return cursor.fetchone() if fetch == "one" else cursor.fetchall()


@contextmanager
def transaction():
    """One connection and one commit for a whole request; any error rolls the lot back.

    A Hotzone Round is four linked rows on one host, so a crash half-way must leave none
    of them (spec User Story 9, docs/adr/0003). run_query's connection-per-statement shape
    stays for the rate and in-pass feeds, whose single write needs no transaction.
    """
    import mysql.connector
    with closing(mysql.connector.connect(**DB_CONFIG["api_data"]())) as conn:
        cursor = conn.cursor(dictionary=True)
        try:
            yield cursor
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()


def run_stmt(cursor, query: str, params: tuple = (), *, fetch: str = "all",
             write: bool = False):
    """Run one statement on the caller's transaction cursor, or — with cursor=None — on a
    short-lived connection of its own that commits it (run_query).

    Returns the rows read, or the new row's id for a write.
    """
    if cursor is None:
        return run_query(query, params, fetch=fetch, commit=write)
    cursor.execute(query, params)
    if write:
        return cursor.lastrowid
    return cursor.fetchone() if fetch == "one" else cursor.fetchall()


# ---- Traffic feeds: the upsert every feed shares (spec §Upsert key and rule) -------------
# The key is (branch_cuscode, timestamps, zone_type) and it is application logic only —
# the tables on 209 carry no unique index, exactly as under globalapi.

def find_existing_row(table: str, req: TrafficHeader, columns: str = "id", *, cursor=None):
    """The row this body upserts onto, or None if the feed has not sent this key before.

    Several hits → the first, as the PHP does (it updates row [0] of the result set).
    `columns` is caller-supplied SQL — this module's own text, never a request body, the
    same rule upsert_row interpolates table and column names under.
    """
    rows = run_stmt(
        cursor,
        f"SELECT {columns} FROM {table}"
        " WHERE branch_cuscode = %s AND timestamps = %s AND zone_type = %s",
        (req.branch, req.timestamp, req.zone_type))
    return rows[0] if rows else None


def upsert_row(table: str, columns: dict, row_id=None, *, key: str = "id",
               cursor=None) -> int:
    """INSERT the planned columns, or UPDATE them onto the row where `key` = row_id.
    Returns the row's id — the new one for an INSERT.

    `key` is not always "id": shoping_traffic_hotzone_mono is keyed by mono_id, and
    emp_demo's row for a round is reached by hotzone_id.

    With a cursor the statement joins the caller's transaction; without one it gets a
    connection of its own and commits alone (run_query's shape, what the rate and in-pass
    feeds use).

    Table, column and key names come from this module only — never from a request body —
    so interpolating them is safe; every value is still a parameter.
    """
    names = list(columns)
    if row_id is None:
        return run_stmt(
            cursor,
            f"INSERT INTO {table} ({', '.join(names)})"
            f" VALUES ({', '.join(['%s'] * len(names))})",
            tuple(columns.values()), write=True)
    run_stmt(cursor,
             f"UPDATE {table} SET {', '.join(f'{n} = %s' for n in names)} WHERE {key} = %s",
             tuple(columns.values()) + (row_id,), write=True)
    return row_id


def header_columns(req: TrafficHeader, now: Optional[datetime] = None) -> dict:
    """The seven columns every feed writes, in the order the PHP merges them.

    insert_date_time is refreshed on an update too, as the PHP does — the in-pass
    backfill's "seeded row" heuristic already lives with that.
    """
    return {
        "branch_cuscode": req.branch,
        "region_id": req.region_id,
        "region_name": req.region_name,
        "province_id": req.province_id,
        "province_name": req.province_name,
        "timestamps": req.timestamp,
        "insert_date_time": (now or datetime.now()).strftime(DT_FMT),
    }


def plan_staffing_ratio(req: StaffingRatioRequest, existing, now: Optional[datetime] = None):
    """Pure planner: validated body + the existing row for the key (or None) →
    (action, the columns to write).

    No database in here. That is what lets the parity verifier replay rows globalapi
    wrote and compare our columns against them with no host in the loop.
    """
    columns = header_columns(req, now)
    columns.update(hour=req.hour, avg_emp=req.avg_emp, avg_cus=req.avg_cus,
                   rate=req.rate, zone_type=req.zone_type)
    return ("updated" if existing else "inserted"), columns


# ---- Traffic Row: the five bill columns, counted on the branch's own POS schema ----------
# The two statements below are PushNotificationController.php's, lifted from
# `api_traffic_analytic` lines 740–785, with one deliberate deviation: the PHP pastes the
# window dates straight into the SQL text, we bind them as %s. Same semantics, no quoting
# to get wrong. `{branch}` is still interpolated — it is a schema name, which cannot be a
# parameter — and TrafficHeader has already proved it is eight digits.
#
# The PHP's own mono fragments (lines 758–761). IT is `!= 'T'`, which excludes a NULL
# mono_apple; kept as-is, because that is the row set globalapi has been counting.
ZONE_TYPE_FILTER = {"MONO": "and mono_apple = 'T'", "IT": "and mono_apple != 'T'"}

# count_sale (PHP lines 740–751): invoices in the window whose condition_ is a selling one.
SALE_BILL_SQL = """
SELECT COUNT(*) AS total_sale_bill
FROM `{branch}`.ar_invoice
WHERE abb_date >= %s
    AND abb_date <= %s
    AND condition_ IN ('00', '11', '12', '13', '14')
    {mono}
"""

# count_bill / count_pos / count_web / count_oc (PHP lines 762–785): one row per invoice
# from the grouped join, then four aggregates over it.
#
# ponytail: GROUP BY Invoice_ID with non-aggregated modul/cart_type selected — MySQL picks
# a row per group and that is the one counted. Verbatim from the PHP on purpose; if
# count_pos ever disagrees with a hand count, this is the line to look at.
BILL_BREAKDOWN_SQL = """
SELECT
    COUNT(*) as total_bill,
    IFNULL(SUM(IF(modul != 'STOCK' AND (cart_type IS NULL OR (cart_type != 'web' AND cart_type != 'order_center')), 1, 0)), 0) as total_pos_bill,
    IFNULL(SUM(IF(cart_type = 'web', 1, 0)), 0) as total_web_bill,
    IFNULL(SUM(IF(cart_type = 'order_center', 1, 0)), 0) as total_ordecenter_bill
FROM (
    SELECT
        ar_invoice.modul,
        ar_invoice.Invoice_ID,
        cart.cart_type
    FROM `{branch}`.ar_invoice
    LEFT JOIN `{branch}`.ar_refer
        ON ar_invoice.Invoice_ID = ar_refer.invoice_id
    LEFT JOIN `{branch}`.cart
        ON ar_refer.cart_no = cart.cart_no AND ar_refer.cart_no != ''
    WHERE abb_date >= %s
        AND abb_date <= %s
        {mono}
    GROUP BY ar_invoice.Invoice_ID
) as a
"""


def fetch_bill_counts(req: TrafficRowRequest) -> dict:
    """The five bill columns for this Traffic Row's hour, counted on the POS host.

    The window is [timestamp, timestamp + 1 hour] — inclusive at BOTH ends, as the PHP
    has it, so a bill landing exactly on the hour is counted by two rows. Kept: changing
    it would move every reported Conversion Rate on cutover.

    Nothing here catches a database error. A POS host that cannot answer must reach the
    client as 500 with nothing written on 209 — zeros would read later as a quiet hour
    (spec User Story 10), so there is deliberately no default-to-zero path.
    """
    window = (req.timestamp,
              (datetime.strptime(req.timestamp, DT_FMT) + TRAFFIC_ROW_HOUR).strftime(DT_FMT))
    sql_args = {"branch": req.branch, "mono": ZONE_TYPE_FILTER[req.zone_type]}
    sale = run_query(SALE_BILL_SQL.format(**sql_args), window, db="pos", fetch="one")
    grouped = run_query(BILL_BREAKDOWN_SQL.format(**sql_args), window, db="pos", fetch="one")
    return {"count_bill": grouped["total_bill"], "count_sale": sale["total_sale_bill"],
            "count_pos": grouped["total_pos_bill"], "count_web": grouped["total_web_bill"],
            "count_oc": grouped["total_ordecenter_bill"]}


def plan_traffic_row(req: TrafficRowRequest, existing, counts: dict,
                     now: Optional[datetime] = None):
    """Pure planner, as plan_staffing_ratio — plus the five counts fetch_bill_counts read.

    Column order is the PHP's merge order, so 04's verifier can diff ours against a row
    globalapi wrote without reordering anything.
    """
    columns = header_columns(req, now)
    columns.update(day_of_week=req.day_of_week, hour=req.hour, walk_pass=req.walk_pass,
                   walk_in=req.walk_in, rate=req.rate, count_bill=counts["count_bill"],
                   count_sale=counts["count_sale"], zone_type=req.zone_type,
                   count_pos=counts["count_pos"], count_web=counts["count_web"],
                   count_oc=counts["count_oc"])
    return ("updated" if existing else "inserted"), columns


# ---- Hotzone Round: four linked tables, planned pure then applied on one cursor ---------

class NewId(str):
    """A row id the plan has not created yet. The executor swaps in the real one once the
    INSERT that returns it has run.

    A str subclass so a plan stays printable and comparable, and its own type so the
    executor's isinstance check can tell a placeholder from a body value that reads alike.
    """


HOTZONE_ID, MONO_ID, DEMO_ID = NewId("hotzone"), NewId("mono"), NewId("demo")

# One statement of a plan. `where` None → INSERT; ("column", value) → UPDATE keyed on that
# column. `returns` names the NewId an INSERT's id binds to. Values may be NewIds.
Write = namedtuple("Write", "table columns where returns", defaults=(None, None))


def fetch_children(cursor, hotzone_id) -> dict:
    """The child ids an update needs: emp_demo's demo_id (what a new demo_tier row links
    to) and the demo_tier row's own id. Either may be absent — a round written before
    demo_tier existed has neither.

    Read on every re-send, where the PHP reads them only when it has Mac demos to file:
    two indexed lookups is the price of leaving every "when" in the planner, so the
    endpoint never has to re-test a condition the planner already owns.
    """
    demo = run_stmt(cursor, f"SELECT demo_id FROM {DEMO_TABLE} WHERE hotzone_id = %s",
                    (hotzone_id,), fetch="one")
    tier = run_stmt(cursor, f"SELECT id FROM {TIER_TABLE} WHERE hotzone_id = %s",
                    (hotzone_id,), fetch="one")
    return {"demo_id": demo["demo_id"] if demo else None,
            "tier_id": tier["id"] if tier else None}


def plan_hotzone(req: HotzoneRequest, existing, children: Optional[dict] = None,
                 now: Optional[datetime] = None):
    """Pure planner: validated body + the existing hotzone row and its children (or None)
    → (action, the ordered writes). No database in here, same as plan_staffing_ratio.

    Branch for branch the PHP's `hotzone` block, with its one bug fixed — see the WHERE on
    the _mono update below.
    """
    children = children or {}
    hotzone = header_columns(req, now)
    hotzone.update(day_of_week=req.day_of_week, hour=req.hour, count_in=req.count_in,
                   count_apple=req.count_apple, count_nb=req.count_nb,
                   nb_to_apple=req.nb_to_apple, apple_to_nb=req.apple_to_nb,
                   count_active=req.count_active, count_not_active=req.count_not_active,
                   zone_type=req.zone_type, count_learnhub=req.count_learnhub,
                   engagement_wait=req.engagement_wait)
    # Each child table takes the body fields named after its columns, in the PHP's order.
    mono = {name: getattr(req, name) for name in MONO_FIELDS}
    demo = {name: getattr(req, name) for name in DEMO_FIELDS}
    tier = {name: getattr(req, name) for name in TIER_FIELDS}

    any_zone = any(getattr(req, f"count_zone{i}") > 0 for i in range(1, 6))

    def link_new_mono(hotzone_id):
        """Insert the _mono row, then stamp its id on the round — both paths do this."""
        return [Write(MONO_TABLE, mono, None, MONO_ID),
                Write(HOTZONE_TABLE, {"mono_id": MONO_ID}, ("id", hotzone_id))]
    writes = []
    if existing is None:
        writes.append(Write(HOTZONE_TABLE, hotzone, None, HOTZONE_ID))
        if any_zone:
            writes += link_new_mono(HOTZONE_ID)
        # Always, zero demos included — every round has an emp_demo row (CONTEXT.md § Demo Round).
        writes.append(Write(DEMO_TABLE, {**demo, "hotzone_id": HOTZONE_ID}, None, DEMO_ID))
        if req.count_zone4_mono > 0:
            writes.append(Write(TIER_TABLE,
                                {**tier, "hotzone_id": HOTZONE_ID, "demo_id": DEMO_ID}))
        return "inserted", writes

    row_id = existing["id"]
    writes.append(Write(HOTZONE_TABLE, hotzone, ("id", row_id)))
    # Updated, never inserted: the PHP updates silently when the row is absent and we keep
    # that — only pre-2024 globalapi rounds lack one and nobody re-sends those.
    writes.append(Write(DEMO_TABLE, demo, ("hotzone_id", row_id)))
    if any_zone:
        mono_id = existing.get("mono_id") or 0
        if mono_id > 0:
            # By mono_id, the table's own key. The PHP filters this on an `id` column the
            # table has not got, which is what makes a re-send die half-written today.
            writes.append(Write(MONO_TABLE, mono, ("mono_id", mono_id)))
        else:
            writes += link_new_mono(row_id)
    if req.count_zone4_mono > 0:
        if children.get("tier_id"):
            writes.append(Write(TIER_TABLE, tier, ("id", children["tier_id"])))
        elif children.get("demo_id"):
            writes.append(Write(TIER_TABLE, {**tier, "hotzone_id": row_id,
                                             "demo_id": children["demo_id"]}))
        # Neither: nothing to link a tier row to, so none is written (as the PHP does).
    return "updated", writes


def apply_writes(writes, cursor) -> dict:
    """Run a plan on one cursor, resolving each NewId as the INSERT that makes it returns.
    The only place a hotzone plan touches the connection. Returns {NewId: real id}.
    """
    ids = {}

    def resolve(value):
        return ids[value] if isinstance(value, NewId) else value

    for write in writes:
        columns = {name: resolve(value) for name, value in write.columns.items()}
        key, row_id = write.where or ("id", None)
        new_id = upsert_row(write.table, columns, resolve(row_id), key=key, cursor=cursor)
        if write.returns:
            ids[write.returns] = new_id
    return ids


app = FastAPI(title="ai_camera_service")


@app.exception_handler(RequestValidationError)
async def malformed_body_is_400(request, exc: RequestValidationError):
    """A malformed body answers 400 carrying pydantic's own detail (spec §Responses), not
    FastAPI's default 422."""
    return JSONResponse({"detail": jsonable_encoder(exc.errors())}, status_code=400)


@app.get("/")
async def health():
    return {"status": "ok"}


@app.post("/traffic/rate")
def traffic_rate(req: StaffingRatioRequest, dry_run: bool = DRY_RUN):
    """One Staffing Ratio sample → one row in shoping_traffic_avg, inserted or updated.

    Sync `def` on purpose: the DB calls block, so Starlette runs this in its threadpool
    instead of stalling the event loop. Fetch, plan, apply — and nothing swallows a database error,
    so a failed write surfaces as 500 instead of a 200 that wrote nothing.
    """
    if feeds_shadowed(req) and not dry_run:
        return shadow_answer("rate", req)
    existing = find_existing_row(AVG_TABLE, req)
    action, columns = plan_staffing_ratio(req, existing)
    if dry_run:
        return dry_run_answer("rate", req, action, existing, columns=columns)
    row_id = upsert_row(AVG_TABLE, columns, existing["id"] if existing else None)
    return {"id": row_id, "action": action}


@app.post("/traffic/hotzone")
def traffic_hotzone(req: HotzoneRequest, dry_run: bool = DRY_RUN):
    """One Hotzone Round → up to four linked rows, all under a single commit.

    Every statement of the request — the lookups included — runs on the one connection
    `transaction()` opens, so a failure part-way leaves no half-written round and answers
    500 rather than a 200 that wrote some of it (spec User Story 9).
    """
    if feeds_shadowed(req) and not dry_run:
        return shadow_answer("hotzone", req)
    with transaction() as cursor:
        existing = find_existing_row(HOTZONE_TABLE, req, "id, mono_id", cursor=cursor)
        children = fetch_children(cursor, existing["id"]) if existing else None
        action, writes = plan_hotzone(req, existing, children)
        if dry_run:
            return dry_run_answer("hotzone", req, action, existing,
                                  writes=[write._asdict() for write in writes])
        ids = apply_writes(writes, cursor)
    return {"id": existing["id"] if existing else ids[HOTZONE_ID], "action": action}


@app.post("/traffic/in-pass")
def traffic_in_pass(req: TrafficRowRequest, dry_run: bool = DRY_RUN):
    """One Traffic Row → one row in shoping_traffic_analytic, inserted or updated, with
    the five bill columns counted on the branch's POS schema.

    The POS read comes FIRST and on purpose: a POS outage then raises before a single
    statement has reached 209, so no hour is ever stamped with zero bills.
    """
    if feeds_shadowed(req) and not dry_run:
        return shadow_answer("in-pass", req)
    counts = fetch_bill_counts(req)
    existing = find_existing_row(ANALYTIC_TABLE, req)
    action, columns = plan_traffic_row(req, existing, counts)
    if dry_run:
        return dry_run_answer("in-pass", req, action, existing, columns=columns)
    row_id = upsert_row(ANALYTIC_TABLE, columns, existing["id"] if existing else None)
    return {"id": row_id, "action": action}


# ---- Zone Hour: a local SQLite file Alitech reads, not 209 (ADR 0010) -------------------
# Created on first write, so a deploy needs no migration step. Columns keep the 209 tables'
# names (header_columns'), so a reader of either finds branch_cuscode and timestamps. The
# unique index leads with timestamps for the readers' date range, and doubles as the
# resend key plus zone_name — a second row for one zone of one hour cannot exist.
ZONE_HOUR_DDL = f"""
CREATE TABLE IF NOT EXISTS {ZONE_HOUR_TABLE} (
    branch_cuscode    TEXT    NOT NULL,
    region_id         TEXT    NOT NULL,
    region_name       TEXT    NOT NULL,
    province_id       INTEGER NOT NULL,
    province_name     TEXT    NOT NULL,
    timestamps        TEXT    NOT NULL,   -- the body's timestamp, as sent
    insert_date_time  TEXT    NOT NULL,   -- server clock, Thai local, DT_FMT
    zone_type         TEXT    NOT NULL,
    day_of_week       TEXT    NOT NULL,
    hour              INTEGER NOT NULL,
    long_wait_seconds REAL    NOT NULL,
    zone_name         TEXT    NOT NULL,
    dwell_sum_seconds REAL    NOT NULL,
    dwell_count       INTEGER NOT NULL,
    wait_sum_seconds  REAL    NOT NULL,
    wait_count        INTEGER NOT NULL,
    long_wait_count   INTEGER NOT NULL,
    unserved_count    INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS {ZONE_HOUR_TABLE}_key
    ON {ZONE_HOUR_TABLE} (timestamps, branch_cuscode, zone_type, zone_name);
"""


def zone_hour_db() -> sqlite3.Connection:
    """A fresh connection to the Zone Hour file, in WAL mode so Alitech can read while we
    write, with the table and index made if absent.

    ZONE_HOUR_DB_PATH unset or empty raises, which answers 500: there is deliberately no
    default path, since a file nobody reads would turn every Zone Hour into a 200 that
    stored nothing where Alitech looks.
    """
    path = os.getenv("ZONE_HOUR_DB_PATH")
    if not path:
        raise RuntimeError("ZONE_HOUR_DB_PATH is not set")
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(ZONE_HOUR_DDL)
    except Exception:
        conn.close()
        raise
    return conn


@app.post("/traffic/zone-hour")
def traffic_zone_hour(req: ZoneHourRequest):
    """One Zone Hour → that hour's rows for this branch and Zone Type, replaced whole.

    Delete-then-insert in one SQLite transaction, so a resend — the client resends until it
    gets a 2xx — never double-counts, a zone dropped from it is gone, and a failure part-way
    leaves the previous send in place. Never shadowed: SQLite is the only copy. Nothing
    catches a sqlite3 error; it answers 500.
    """
    header = header_columns(req)
    header.update(zone_type=req.zone_type, day_of_week=req.day_of_week, hour=req.hour,
                  long_wait_seconds=req.long_wait_seconds)
    names = list(header) + list(ZoneHourZone.model_fields)
    insert = (f"INSERT INTO {ZONE_HOUR_TABLE} ({', '.join(names)})"
              f" VALUES ({', '.join(':' + n for n in names)})")
    with closing(zone_hour_db()) as conn:
        with conn:   # commits on the way out, rolls back on any error
            removed = conn.execute(
                f"DELETE FROM {ZONE_HOUR_TABLE}"
                " WHERE branch_cuscode = ? AND zone_type = ? AND timestamps = ?",
                (req.branch, req.zone_type, req.timestamp)).rowcount
            conn.executemany(insert, [{**header, **zone.model_dump()} for zone in req.zones])
    return {"action": "replaced" if removed else "inserted", "zones": len(req.zones)}
