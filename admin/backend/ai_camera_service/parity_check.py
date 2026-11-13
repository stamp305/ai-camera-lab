#!/usr/bin/env python3
"""Parity verifier: prove each traffic feed writes what globalapi's PHP wrote.

Issue: `.scratch/ai-camera-traffic-feed/issues/04-parity-verifier-against-php-rows.md`
Spec:  `.scratch/ai-camera-traffic-feed/spec.md`

Take rows `api_traffic_analytic` actually wrote on a settled day, rebuild the request body
the branch client would have sent for each one, run our own planners over it — once as a
fresh insert, once as an update against the stored row — and diff the columns we would
write against the columns that are there. An all-green run is the gate for the deploy
ticket (05).

    # from ai-camera-admin/backend/ai_camera_service/
    ../.venv/bin/python parity_check.py                     # 3 days back, 20 rows
    ../.venv/bin/python parity_check.py --date 2026-09-01 --limit 50
    ../.venv/bin/python parity_check.py --branch 51000017

## It never writes

Every statement is a SELECT, on 209 and on the POS host both. `read_only()` wraps
`app.run_query` for the life of the process, so an INSERT or UPDATE raises instead of
running — a property of the process, not a promise in a docstring.

## What it can and cannot catch

The body is rebuilt *from* the stored row, so a client-sent column (walk_in, count_zone3,
day_of_week…) is compared against the value it was rebuilt from and can only ever agree.
That is the ticket's design, and these are what remain — every one a way our write path
could really differ from the PHP's:

  - a column the PHP filled that our plan never writes (an update leaves it stale, an
    insert leaves it at the column default) — `diff_columns` checks that direction too.
    A column no feed was ever meant to own belongs in IGNORED_COLUMNS, with the reason
  - a column our plan writes that the PHP's row has not got, or has NULL in
  - the five bill columns, which we compute ourselves — the whole point of the exercise
  - the stored form of a value: `hour` planned as `9` against a varchar column holding
    `'09'` really would store `'9'`, and issue 02 left that question to this run. The
    stored column decides the comparison, so the same `9` against an INT column is no
    finding at all — see `same`
  - the row an UPDATE is aimed at, not only the values it sets — a plan writing the right
    numbers onto the wrong row is the PHP bug this whole feature exists to fix
  - which child rows a Hotzone Round gets: emp_demo always, `_mono` only when a zone was
    counted, demo_tier only when the Mac zone had demos — checked both ways against the
    children the PHP really created
"""
import argparse
import sys
from collections import namedtuple
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from dotenv import find_dotenv, load_dotenv

# Both forms on purpose: `read_only` replaces `appmod.run_query`, so every read this file
# makes has to go through the module attribute for the guard to see it. The names imported
# below are constants and pure planners, plus fetch_bill_counts — which reads through app's
# own module global and is therefore guarded too. Never `from app import run_query`.
import app as appmod
from app import (ANALYTIC_TABLE, AVG_TABLE, DEMO_FIELDS, DEMO_TABLE, DT_FMT, HOTZONE_TABLE,
                 MONO_FIELDS, MONO_TABLE, TIER_FIELDS, TIER_TABLE, HotzoneRequest, NewId,
                 StaffingRatioRequest, TrafficRowRequest, fetch_bill_counts, plan_hotzone,
                 plan_staffing_ratio, plan_traffic_row)

# The five the service counts on the POS host itself. A mismatch here is reported apart and
# does not fail the run: a bill entered after globalapi's last send is legitimately ours.
BILL_COLUMNS = frozenset({"count_bill", "count_sale", "count_pos", "count_web", "count_oc"})

# Out of scope by the ticket: row ids and the links between them carry no meaning across a
# rebuild, and insert_date_time is a clock reading.
IGNORED_COLUMNS = frozenset({"id", "mono_id", "hotzone_id", "demo_id", "insert_date_time"})

# Fixed so a plan is reproducible; every column it reaches is ignored anyway.
FROZEN_CLOCK = datetime(2000, 1, 1)

# The three feeds, each named by its route: CONTEXT.md § Staffing Ratio warns off the bare
# word "rate", and a route name tells the operator which endpoint the run is vouching for.
RATE_FEED, IN_PASS_FEED = "/traffic/rate", "/traffic/in-pass"
FEEDS = ((RATE_FEED, AVG_TABLE), (IN_PASS_FEED, ANALYTIC_TABLE),
         ("/traffic/hotzone", HOTZONE_TABLE))

DAY_FMT = "%Y-%m-%d"
SETTLED_DAYS = 3        # default --date: far enough back that late-entered bills have stopped

# One disagreement about one column. `paths` names the plan(s) that disagreed — "insert",
# "update", or "insert+update"; `planned` and `stored` are already in printable form.
Diff = namedtuple("Diff", "paths table column planned stored")

# One row of one table, planned and compared. `note` is set when the row could not be
# checked at all, which counts as a mismatch — an unverifiable row is not a green one.
RowCheck = namedtuple("RowCheck", "label diffs bill_diffs note", defaults=(None,))


class Note(str):
    """A word standing in for a value in a diff, printed as itself and not as a string.

    A str subclass so a plan stays printable, and its own type so `same` can never mistake
    one for a stored column that happens to read alike (NewId in app.py, same trick).
    """

    def __repr__(self):
        return str(self)


MISSING = Note("(none)")     # this side has no such column
WRITTEN = Note("written")    # this side writes a row here


# ---- comparing a planned column against the stored one ----------------------------------

def as_text(value):
    """One comparable form for whatever the driver handed back for a column."""
    if isinstance(value, datetime):
        return value.strftime(DT_FMT)
    if isinstance(value, date):
        return value.strftime(DAY_FMT)
    if isinstance(value, timedelta):     # a TIME column comes back as one
        return str(value)
    return value


def same(planned, stored) -> bool:
    """Would writing `planned` leave the column holding what `stored` holds?

    The stored side decides how to compare, because the column's own type decides what
    MySQL keeps. A numeric column coerces whatever it is given, so `'09'` planned for a
    column reading back `9` lands the same value the PHP landed and is no difference at
    all. A text column keeps what it is given verbatim, so `9` planned for a column
    holding `'09'` really would store `'9'` and lose the padding every reader keys on —
    which is the question issue 02 left to this run, answered by the column, not guessed.
    """
    planned, stored = as_text(planned), as_text(stored)
    if isinstance(stored, (int, float, Decimal)) and not isinstance(stored, bool):
        try:
            return Decimal(str(planned)) == Decimal(str(stored))
        except (InvalidOperation, ValueError):   # planned is not a number at all
            return False
    return type(planned) is type(stored) and planned == stored


def unwritten_is_harmless(value) -> bool:
    """A stored value our plan never writes that our plan would land anyway: NULL.

    A nullable column we leave out of an INSERT comes out NULL, which is what the row
    already holds, so there is nothing to report. Any other value is reported — the PHP put
    it there and our INSERT would not.

    ponytail: a nullable column with a non-NULL DEFAULT breaks the reasoning (we would land
    the default, not NULL). Rare enough to leave; read information_schema.COLUMNS for the
    defaults if a live run ever turns one up.
    """
    return value is None


def diff_columns(planned: dict, stored: dict) -> dict:
    """{column: (planned, stored)} for every column the two disagree on, both directions.

    The second direction — a column the PHP's row carries a value in that our plan never
    writes — is the one that catches a column we forgot: an update would leave it stale and
    an insert would leave it at the column default. A stored NULL is not reported, since
    that is what our INSERT would land there too (see unwritten_is_harmless).
    """
    names = list(planned) + [n for n in stored
                             if n not in planned and not unwritten_is_harmless(stored[n])]
    diffs = {}
    for name in names:
        if name in IGNORED_COLUMNS:
            continue
        left, right = planned.get(name, MISSING), stored.get(name, MISSING)
        if not same(left, right):
            diffs[name] = (left, right)
    return diffs


def merge_paths(by_path: dict) -> list:
    """{path: {table: {column: (planned, stored)}}} → one Diff per distinct disagreement.

    A flat feed's two plans differ only in the action, so the same column would otherwise be
    reported twice; a difference only one path has keeps that path's name.
    """
    merged = {}
    for path, tables in by_path.items():
        for table, diffs in tables.items():
            for column, (planned, stored) in diffs.items():
                merged.setdefault((table, column, repr(planned), repr(stored)), []).append(path)
    return [Diff("+".join(paths), table, column, planned, stored)
            for (table, column, planned, stored), paths in merged.items()]


def label_of(row: dict) -> str:
    return (f"id={row.get('id')} branch {row.get('branch_cuscode')} "
            f"{as_text(row.get('timestamps'))} {row.get('zone_type')}")


# ---- rebuilding the request body out of the row the PHP wrote ---------------------------

def body_fields(model, row: dict) -> dict:
    """The request body for a stored row: every field of the model is named after its
    column, bar the two the spec renames (`branch`, `timestamp`).

    A field the model types as `str` takes the stored value as text, because that is what
    the client holds and sends — `hour` read back as `9` from an INT column is the column's
    type showing through, not a different body, and refusing it would fail the row over a
    question the comparison answers properly.
    """
    fields = {}
    for name, field in model.model_fields.items():
        if name in row:
            value = as_text(row[name])
            fields[name] = str(value) if field.annotation is str and value is not None else value
    fields["branch"] = str(row["branch_cuscode"])
    fields["timestamp"] = as_text(row["timestamps"])
    return fields


def hotzone_body_fields(row: dict, children: dict) -> dict:
    """As body_fields, with the three child rows folded back in — an absent child means the
    client sent zeros for its zones, which is exactly what makes the PHP skip it."""
    fields = body_fields(HotzoneRequest, row)
    for names, child in ((MONO_FIELDS, children.get(MONO_TABLE)),
                         (DEMO_FIELDS, children.get(DEMO_TABLE)),
                         (TIER_FIELDS, children.get(TIER_TABLE))):
        fields.update({name: (child[name] if child else blank(name)) for name in names})
    return fields


def blank(field: str):
    """What an absent child row means for one of its fields: no name, no count."""
    return "" if field.startswith("name") else 0


# ---- checking one row --------------------------------------------------------------------

def check_flat(req, plan, row: dict, table: str) -> RowCheck:
    """A Staffing Ratio or Traffic Row row, planned as an insert and as an update.

    No update target to check, unlike a Hotzone Round: these two planners return columns
    only, and the endpoint aims the UPDATE at `existing["id"]` itself.
    """
    by_path = {path: {table: diff_columns(plan(req, existing)[1], row)}
               for path, existing in (("insert", None), ("update", row))}
    diffs, bills = split_bills(merge_paths(by_path))
    return RowCheck(label_of(row), diffs, bills)


def check_hotzone(row: dict, children: dict) -> RowCheck:
    """A Hotzone Round: the round's own columns, its children's columns, and — both ways —
    which children the plan creates against which children the PHP created."""
    req = HotzoneRequest(**hotzone_body_fields(row, children))
    stored = {HOTZONE_TABLE: row, **{t: c for t, c in children.items() if c}}
    child_ids = {"demo_id": (children.get(DEMO_TABLE) or {}).get("demo_id"),
                 "tier_id": (children.get(TIER_TABLE) or {}).get("id")}
    by_path = {}
    for path, existing, ids in (("insert", None, None), ("update", row, child_ids)):
        writes = plan_hotzone(req, existing, ids)[1]
        planned = plan_columns(writes)
        tables = {}
        for table, columns in planned.items():
            tables[table] = (diff_columns(columns, stored[table]) if table in stored else
                             {"(whole row)": (WRITTEN, MISSING)})
        for table in stored:
            if table not in planned:
                tables[table] = {"(whole row)": (MISSING, WRITTEN)}
        for table, aim in update_targets(writes).items():
            if table in stored:
                tables[table].update(aim(stored[table]))
        by_path[path] = tables
    diffs, bills = split_bills(merge_paths(by_path))
    return RowCheck(label_of(row), diffs, bills)


def update_targets(writes) -> dict:
    """{table: a check of the row each UPDATE is aimed at}.

    Columns alone would not catch a plan updating the right values onto the wrong row — and
    that is the very bug this feature exists to fix: the PHP filters the `_mono` update on
    an `id` column the table has not got, so a re-send dies half-written (spec §Hotzone
    fan-out). A key the stored child row does not carry reads back as (none) here.

    An INSERT has no target, and a target the plan has not created yet (a NewId on the
    insert path) cannot be compared with a row that already exists.
    """
    def aim(key, value):
        return lambda stored_row: ({} if same(value, stored_row.get(key, MISSING))
                                   else {f"WHERE {key}": (value, stored_row.get(key, MISSING))})

    return {write.table: aim(*write.where) for write in writes
            if write.where and not isinstance(write.where[1], NewId)}


def plan_columns(writes) -> dict:
    """A hotzone plan's ordered writes → {table: every column that table is given}.

    The `mono_id` stamp is a second write on the round; merging it in costs nothing, since
    ids are out of the comparison anyway.
    """
    tables = {}
    for write in writes:
        tables.setdefault(write.table, {}).update(write.columns)
    return tables


def split_bills(diffs: list):
    """The five bill columns are reported apart and do not fail the run."""
    return ([d for d in diffs if d.column not in BILL_COLUMNS],
            [d for d in diffs if d.column in BILL_COLUMNS])


# ---- reading the day's rows (SELECT only) ------------------------------------------------

def read_only(run_query):
    """`app.run_query` with every statement that is not a SELECT refused.

    The verifier runs against production on two hosts. Making the read-only claim a wrapper
    means a future edit that plans a write fails loudly here instead of quietly changing a
    row somebody reports on.
    """
    def guarded(query, params=(), *, db="api_data", fetch="all", commit=False):
        if commit or not query.lstrip().upper().startswith("SELECT"):
            raise RuntimeError(f"parity_check is read-only; refused on {db}: "
                               f"{' '.join(query.split())[:80]}")
        return run_query(query, params, db=db, fetch=fetch)
    return guarded


def sample(table: str, args) -> list:
    """The rows written for --date, oldest first, at most --limit of them.

    Sorted here, not by ORDER BY id: with a LIMIT above the day's rows, MySQL may walk the
    whole primary key of a table on a host other teams use.
    """
    day = datetime.strptime(args.date, DAY_FMT)
    where, params = "timestamps >= %s AND timestamps < %s", [
        day.strftime(DT_FMT), (day + timedelta(days=1)).strftime(DT_FMT)]
    if args.branch:
        where += " AND branch_cuscode = %s"
        params.append(args.branch)
    rows = appmod.run_query(f"SELECT * FROM {table} WHERE {where} LIMIT {int(args.limit)}",
                            tuple(params))
    return sorted(rows, key=lambda row: row["id"])


def hotzone_children(row: dict) -> dict:
    """The three child rows the PHP linked to this round, each None when it made none."""
    def one(table, key, value):
        rows = appmod.run_query(f"SELECT * FROM {table} WHERE {key} = %s", (value,))
        return rows[0] if rows else None

    return {MONO_TABLE: one(MONO_TABLE, "mono_id", row["mono_id"]) if row.get("mono_id") else None,
            DEMO_TABLE: one(DEMO_TABLE, "hotzone_id", row["id"]),
            TIER_TABLE: one(TIER_TABLE, "hotzone_id", row["id"])}


def check_feed(name: str, table: str, args) -> list:
    """Every sampled row of one feed, checked. A row that cannot be checked — a body that
    will not rebuild, a POS host that will not answer — comes back as a note."""
    checks = []
    for row in sample(table, args):
        try:
            if name == RATE_FEED:
                req = StaffingRatioRequest(**body_fields(StaffingRatioRequest, row))
                checks.append(check_flat(req, plan_with_clock(plan_staffing_ratio), row,
                                         AVG_TABLE))
            elif name == IN_PASS_FEED:
                req = TrafficRowRequest(**body_fields(TrafficRowRequest, row))
                counts = fetch_bill_counts(req)     # the real POS queries, read-only
                plan = plan_with_clock(plan_traffic_row, counts)
                checks.append(check_flat(req, plan, row, ANALYTIC_TABLE))
            else:
                checks.append(check_hotzone(row, hotzone_children(row)))
        except Exception as exc:
            checks.append(RowCheck(label_of(row), [], [], f"{type(exc).__name__}: {exc}"))
    return checks


def plan_with_clock(planner, *extra):
    """A planner called the way check_flat wants it, on a clock that never moves."""
    return lambda req, existing: planner(req, existing, *extra, now=FROZEN_CLOCK)


# ---- the report --------------------------------------------------------------------------

def print_feed(name: str, table: str, checks: list) -> bool:
    """One feed's table, then every mismatch it found. True when nothing blocks the deploy."""
    exact = [c for c in checks if not c.diffs and not c.bill_diffs and not c.note]
    print(f"\n── {name} — {table} " + "─" * max(0, 58 - len(name) - len(table)))
    print(f"   rows checked {len(checks):>4}    exact {len(exact):>4}    "
          f"mismatched {len(checks) - len(exact):>4}")
    if not checks:
        print("   !! nothing to check on this day — a run that verified no row is not a pass")
        return False
    blocking = [c for c in checks if c.diffs or c.note]
    for check in blocking:
        print(f"\n   ✗ {check.label}")
        if check.note:
            print(f"       could not check this row — {check.note}")
        for paths, tbl, column, planned, stored in check.diffs:
            print(f"       [{paths}] {tbl}.{column}: planned {planned}  stored {stored}")
    bills = [c for c in checks if c.bill_diffs]
    if bills:
        print(f"\n   bill columns ({len(bills)} row(s)) — counted by us, not sent by the client.")
        print("   A higher count on our side is expected: bills entered after globalapi's")
        print("   last send for the hour are real bills it never saw. Judge, do not assume.")
        for check in bills:
            print(f"     · {check.label}")
            for paths, tbl, column, planned, stored in check.bill_diffs:
                print(f"       [{paths}] {tbl}.{column}: ours {planned}  globalapi {stored}")
    return not blocking


def parse_args(argv=None):
    default_date = (date.today() - timedelta(days=SETTLED_DAYS)).strftime(DAY_FMT)
    parser = argparse.ArgumentParser(
        description="Prove each traffic feed plans what globalapi's PHP wrote. "
                    "Reads only — it writes nothing, on either host.")
    parser.add_argument("--date", default=default_date, metavar="YYYY-MM-DD",
                        help=f"the settled day to sample (default {default_date}, "
                             f"{SETTLED_DAYS} days back — late bills have stopped moving)")
    parser.add_argument("--limit", type=int, default=20,
                        help="rows sampled per feed (default 20)")
    parser.add_argument("--branch", default=None, metavar="BRANCH",
                        help="only this branch_cuscode (default: every branch)")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    env_path = find_dotenv()        # the backend's env: NEW_DB_* on 209, POS_DB_* on POS
    if env_path:
        load_dotenv(env_path)
    appmod.run_query = read_only(appmod.run_query)

    print(f"parity check — {args.date}, up to {args.limit} rows per feed"
          + (f", branch {args.branch}" if args.branch else ", every branch"))
    print("reads only; nothing is written on 209 or on the POS host")
    green = True
    for name, table in FEEDS:
        green &= print_feed(name, table, check_feed(name, table, args))
    print("\n" + ("ALL GREEN — every planned column matches the row globalapi wrote."
                  if green else
                  "RED — the mismatches above are outside the bill columns; 05 waits."))
    return 0 if green else 1


if __name__ == "__main__":
    sys.exit(main())
