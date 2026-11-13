# Deploying ai_camera_service (inside the admin backend)

The traffic feeds: `POST /traffic/rate`, `/traffic/in-pass`, `/traffic/hotzone` write the
three camera feeds straight into the six `api_data` tables globalapi2 writes today. Spec:
`.scratch/ai-camera-traffic-feed/spec.md`; rationale:
`docs/adr/0003-traffic-feeds-write-to-api-data-directly.md`. Until cutover those three only shadow
globalapi2 (§1). The fourth feed, `POST /traffic/zone-hour`, writes Zone Hours to a local SQLite
file that Alitech reads (§1, Zone Hour file); rationale:
`docs/adr/0010-zone-hours-in-local-sqlite-old-feeds-shadow.md`.
What branch clients send, and how they must react to each answer: [`../docs/client-contract.md`](../docs/client-contract.md).

It runs inside the admin backend: mounted at `/ai-camera-service` on `127.0.0.1:8011`, in the
same process, venv and systemd unit (`ai-camera-admin-backend`). Rationale:
`docs/adr/0009-traffic-feeds-live-in-the-admin-backend.md`.

## 0. Deploy order — load-bearing, do not reorder

**1. An all-green parity run pasted into ticket 04.**

`parity_check.py` replays rows globalapi2 actually wrote through our planners and reports any
column that differs. It is the gate: **do not switch any branch client to these endpoints
until a run is green and its output is in the `## Comments` of
`.scratch/ai-camera-traffic-feed/issues/04-parity-verifier-against-php-rows.md`.** It cannot
guard a restart: the endpoints go live on the admin backend's next restart, and that restarts
for admin work too.

```bash
cd ai-camera-admin/backend/ai_camera_service
../.venv/bin/python parity_check.py            # 3 days back, 20 rows/feed
```

Exit 0 = green. Exit 1 = something outside the bill columns differs — that blocks the client switch.
A bill-column difference alone does not: an invoice entered after globalapi2's last send
legitimately raises our Bill Count.

The script only ever SELECTs (a guard inside it raises on anything else), but it reads all six
tables on 209 **and** the POS host, which takes step 2's `SELECT` rights. So either the DB
team runs it, or the `SELECT` half of step 2 lands first. Applying step 2 in full before the run
is also safe: nothing calls the new endpoints until the client switch, so write rights sit idle.

**2. Grants on 209 and on the POS host — one ticket for the DB team.**

```sql
-- api_data @ 10.10.10.209 — the six tables the three feeds upsert
GRANT SELECT, INSERT, UPDATE ON api_data.shoping_traffic_analytic     TO '<NEW_DB_USER>'@'<service-host>';
GRANT SELECT, INSERT, UPDATE ON api_data.shoping_traffic_avg          TO '<NEW_DB_USER>'@'<service-host>';
GRANT SELECT, INSERT, UPDATE ON api_data.shoping_traffic_hotzone      TO '<NEW_DB_USER>'@'<service-host>';
GRANT SELECT, INSERT, UPDATE ON api_data.shoping_traffic_hotzone_mono TO '<NEW_DB_USER>'@'<service-host>';
GRANT SELECT, INSERT, UPDATE ON api_data.emp_demo                     TO '<NEW_DB_USER>'@'<service-host>';
GRANT SELECT, INSERT, UPDATE ON api_data.demo_tier                    TO '<NEW_DB_USER>'@'<service-host>';
-- POS host (globalapi2 calls this connection `db_branch`). What the service actually reads is
-- ar_invoice, ar_refer and cart in every branch schema — /traffic/in-pass counts the five bill
-- columns there, one schema per branch, named after branch_cuscode. The exact form is the DB
-- team's call; a database-wildcard grant is the usual shape:
GRANT SELECT ON `%`.* TO '<POS_DB_USER>'@'<service-host>';
FLUSH PRIVILEGES;
```

`<service-host>` is the box the admin backend runs on (10.10.10.85).

**The wildcard line grants more than the service needs — say so when you file the ticket.**
`` `%`.* `` is every table in every schema on that host, not the three tables named above. It is
written that way because the branch schemas are one per branch and new ones appear, so naming
them is a grant that goes stale. If the DB team would rather scope it, three table-level grants
per branch schema is the same thing, narrower; the service reads nothing else and would not
notice.

**3. Then restart the admin backend:** `sudo systemctl restart ai-camera-admin-backend`.

**Nothing arrives yet.** The three endpoints receive no traffic until the frontend exposes them
(`.scratch/ai-camera-service-into-admin/issues/03-frontend-routes-behind-device-key.md`) and the
branch clients are switched over, which is separate work. A restart on this code
changes nothing a client sees; it only makes the endpoints answerable for the smoke check below.

## 1. Configure

The admin backend loads one `.env` at startup — python-dotenv's nearest, walking up from
`backend/` — and on .85 that is `/home/ubuntu/advice/python_server/.env`. The feed code reads its
variables from there and loads nothing itself. See
[`.env.ai_camera_service.example`](../.env.ai_camera_service.example) for the complete variable
list with comments — that file and this one must agree. Every variable `app.py` reads:

| variable | used by | unset means |
|---|---|---|
| `NEW_DB_USER` / `NEW_DB_PASS` | everything on `api_data`@209 — all three feeds | every endpoint 500s except `GET /`, which touches no database |
| `POS_DB_HOST` / `POS_DB_USER` / `POS_DB_PASS` | the POS host, read-only, for the five bill columns | `/traffic/in-pass` 500s on every call; the other two feeds are fine |
| `TRAFFIC_FEEDS_SHADOW` | the three feeds' shadow mode (ADR 0010) | **shadow** — the default. Only exactly `0` turns real writes on |
| `ZONE_HOUR_DB_PATH` | `/traffic/zone-hour` — the SQLite file it writes (ADR 0010) | `/traffic/zone-hour` 500s on every call; there is no default path. The other feeds are fine |

**Shadow mode is on unless `TRAFFIC_FEEDS_SHADOW=0`.** Unset, empty, `1`, `false` — anything but
`0` — and `/traffic/rate`, `/in-pass` and `/hotzone` only validate: a valid body answers
`200 {"action": "shadow"}`, a malformed one still answers 400, and neither 209 nor the POS host is
touched. globalapi2 stays the writer while branches dual-send. Each accepted call logs one INFO
line in `journalctl -u ai-camera-admin-backend`:

```
INFO:     traffic shadow feed=rate branch=51000017 zone_type=IT timestamp=2026-09-01 14:10:00
```

Add `TRAFFIC_FEEDS_SHADOW=0` to the root `.env` at cutover only; the answers shown in §2c–§2e assume it.
The `.env` is loaded once at startup, so a change to this line takes effect only after
`sudo systemctl restart ai-camera-admin-backend`. While shadow is on, the DB settings above are not
read by these three feeds at all.

The shared root `.env` on a prod box usually carries the `NEW_DB_*` pair already.
The three `POS_DB_*` are new with the traffic feeds and will not be there — add them to that
file. Do not create a `.env` in `backend/` or `ai-camera-admin/` to hold them: python-dotenv
stops at the nearest one, so it would hide the root file from the whole admin backend.

**Zone Hour file (`ZONE_HOUR_DB_PATH`).** An absolute path, in the same root `.env`, naming the
SQLite file `/traffic/zone-hour` writes and Alitech reads (Alitech's own `.env` names the same
path). The service creates the file, its `zone_hour` table and index on the first write, in WAL
mode, so a deploy needs no migration step. It does not create the directory: that must exist and
be writable by the backend's user, because WAL keeps `-wal` and `-shm` files beside the database.
Keep the three together; copying only the main file while the backend runs can lose recent hours.
The feed is never shadowed: SQLite is the only copy of a Zone Hour, so it always writes. Like every
line here, a new or changed `ZONE_HOUR_DB_PATH` takes effect only after
`sudo systemctl restart ai-camera-admin-backend`. Check it once a branch has sent:

```bash
sqlite3 "$ZONE_HOUR_DB_PATH" "SELECT branch_cuscode, timestamps, zone_type, zone_name, dwell_sum_seconds, dwell_count FROM zone_hour ORDER BY timestamps DESC LIMIT 10"
```

A Zone Hour answers `{"action": "inserted" | "replaced", "zones": <n>}`. `replaced` means that
branch + Zone Type + `timestamp` had rows before: they are deleted and the new zones inserted in one
transaction, so a resend never double-counts. A 400 means nothing ran; a 500 means the previous
send for that hour is still in place.

## 2. Smoke check

### 2a. Health

```bash
curl -s 127.0.0.1:8011/ai-camera-service/
# → {"status":"ok"}
```

### 2b. The three traffic feeds — pick your key before you curl

These endpoints write into the same six tables every report reads. A smoke row is a real row.

**Read this before you send anything at a real key.** An update here is not a partial one: the
UPDATE `SET`s *every* column the body carries. Fire a sample body at an hour a branch really
traded and you have just overwritten that hour's real `walk_pass`, `walk_in`, `rate` and zone
counts with the made-up numbers below. The samples in §2c–§2e are there for their **shape** — the
field names and types — never for their values.

On **pre-prod, or a branch nobody reports on**, stop reading and use them as they stand.

On **prod**, two shapes are safe, and nothing else is:

**Replay a stored row** (the one to prefer). Run the verifying SELECT of §2c/§2d/§2e *first*, copy
that row's own numbers into the body, and send it. Every column is set to what it already held, so
only `insert_date_time` moves, the answer is `"action": "updated"` with the row's own id, and the
check is a real one: you have proved the service reproduces a row globalapi2 wrote. It is the same
thing a client re-send does, and the same thing `parity_check.py` gates on. (In-pass is the one
exception, and it is by design: the five bill columns are recounted from the POS host rather than
copied, so they can legitimately come back higher — see §2d.)

**Or take the insert path on a key nothing has ever written** — which is the only way to see
`"action": "inserted"`, and which leaves a row reports will count. Note the ids the responses give
you and hand the DB team the cleanup; the service has no `DELETE` right, by design:

```sql
DELETE FROM shoping_traffic_avg      WHERE id = <id>;
DELETE FROM shoping_traffic_analytic WHERE id = <id>;
-- hotzone: children first, then the round
DELETE FROM demo_tier                 WHERE hotzone_id = <id>;
DELETE FROM emp_demo                  WHERE hotzone_id = <id>;
DELETE FROM shoping_traffic_hotzone_mono WHERE mono_id = <the round's mono_id>;
DELETE FROM shoping_traffic_hotzone   WHERE id = <id>;
```

The samples below use branch `51000017`, Zone Type `IT` and Thai local time — swap in a branch of
your own. `timestamp` follows the client's convention: **the hour** for in-pass, **a 10-minute
boundary** for rate and hotzone. Nothing in the service enforces that (spec §Validation), so a
typo lands a row on a key no report looks at.

### 2c. `POST /traffic/rate` — one Staffing Ratio sample → `shoping_traffic_avg`

```bash
curl -s -X POST 127.0.0.1:8011/ai-camera-service/traffic/rate -H 'Content-Type: application/json' \
  -d '{"branch": "51000017", "region_id": "C", "region_name": "กรุงเทพและปริมณฑล",
       "province_id": 1, "province_name": "กรุงเทพมหานคร",
       "timestamp": "2026-09-01 14:10:00", "zone_type": "IT",
       "hour": "14", "avg_emp": 2.5, "avg_cus": 7.0, "rate": 2.8}'
# → {"id": 483921, "action": "updated"}
```

Verify the row on 209:

```sql
SELECT id, branch_cuscode, timestamps, zone_type, hour, avg_emp, avg_cus, rate, insert_date_time
FROM shoping_traffic_avg
WHERE branch_cuscode = '51000017' AND timestamps = '2026-09-01 14:10:00' AND zone_type = 'IT';
```

`id` must equal the response's, `avg_emp` / `avg_cus` / `rate` the numbers your body carried
(the row's own, if you took §2b's replay path), and `insert_date_time` must be a few seconds old. There is deliberately **no `day_of_week` column**
on this table — the PHP never stored one for this feed.

Now **send the exact same curl again**. It must answer `{"id": 483921, "action": "updated"}` —
the same id it gave the first time. Re-run the SELECT: still one row, `insert_date_time` moved forward. A second row with a new id
means the upsert key is not matching — stop and check `timestamps` and `zone_type` are
character-for-character what you sent.

### 2d. `POST /traffic/in-pass` — one Traffic Row → `shoping_traffic_analytic`

This is the only feed that reads the POS host. The five bill columns are **not** in the body —
the service counts them itself, before it writes anything on 209.

```bash
curl -s -X POST 127.0.0.1:8011/ai-camera-service/traffic/in-pass -H 'Content-Type: application/json' \
  -d '{"branch": "51000017", "region_id": "C", "region_name": "กรุงเทพและปริมณฑล",
       "province_id": 1, "province_name": "กรุงเทพมหานคร",
       "timestamp": "2026-09-01 14:00:00", "zone_type": "IT",
       "day_of_week": "Tuesday", "hour": 14, "walk_pass": 310, "walk_in": 46, "rate": 14.84}'
# → {"id": 771204, "action": "updated"}
```

```sql
SELECT id, branch_cuscode, timestamps, zone_type, day_of_week, hour, walk_pass, walk_in, rate,
       count_bill, count_sale, count_pos, count_web, count_oc, insert_date_time
FROM shoping_traffic_analytic
WHERE branch_cuscode = '51000017' AND timestamps = '2026-09-01 14:00:00' AND zone_type = 'IT';
```

**Then run the two POS queries by hand** and compare the five bill columns against what the PHP
would have written. These are the service's own statements (`app.py`, lifted from
`docs/PushNotificationController.php` — `count_sale` at lines 740–751, the grouped one at
762–785), with the window and the branch filled in.
The window is `[timestamp, timestamp + 1 hour]`, **inclusive at both ends** — that is the PHP's,
kept on purpose. The last line is the Zone Type filter: `mono_apple = 'T'` for a MONO round,
`mono_apple != 'T'` for an IT one (which excludes a NULL — also the PHP's, also kept).

```sql
-- → count_sale
SELECT COUNT(*) AS total_sale_bill
FROM `51000017`.ar_invoice
WHERE abb_date >= '2026-09-01 14:00:00' AND abb_date <= '2026-09-01 15:00:00'
  AND condition_ IN ('00', '11', '12', '13', '14')
  AND mono_apple != 'T';

-- → count_bill, count_pos, count_web, count_oc
SELECT COUNT(*) AS total_bill,
       IFNULL(SUM(IF(modul != 'STOCK' AND (cart_type IS NULL OR (cart_type != 'web' AND cart_type != 'order_center')), 1, 0)), 0) AS total_pos_bill,
       IFNULL(SUM(IF(cart_type = 'web', 1, 0)), 0) AS total_web_bill,
       IFNULL(SUM(IF(cart_type = 'order_center', 1, 0)), 0) AS total_ordecenter_bill
FROM (
    SELECT ar_invoice.modul, ar_invoice.Invoice_ID, cart.cart_type
    FROM `51000017`.ar_invoice
    LEFT JOIN `51000017`.ar_refer ON ar_invoice.Invoice_ID = ar_refer.invoice_id
    LEFT JOIN `51000017`.cart     ON ar_refer.cart_no = cart.cart_no AND ar_refer.cart_no != ''
    WHERE abb_date >= '2026-09-01 14:00:00' AND abb_date <= '2026-09-01 15:00:00'
      AND mono_apple != 'T'
    GROUP BY ar_invoice.Invoice_ID
) AS a;
```

The five numbers must equal the five columns exactly. If you chose an hour globalapi2 wrote and
our `count_*` came out **higher** than what was stored before the call, that is expected and not
a fault: an invoice entered after globalapi2's last send is counted now. Anything else is the same
class of difference `parity_check.py` gates on — go back to step 1.

Re-send the identical curl: `{"id": 771204, "action": "updated"}`, same id, one row, bill columns
recounted (so they may move if a bill landed in between — the PHP recounts on every send too).

### 2e. `POST /traffic/hotzone` — one Hotzone Round → four tables in one transaction

The long one: 43 fields — the seven-field header plus 36 spread over four linked rows, all under
one commit. Zone names are the branch's product-zone names (`-` for an unused slot), exactly as
the client sends them.

```bash
curl -s -X POST 127.0.0.1:8011/ai-camera-service/traffic/hotzone -H 'Content-Type: application/json' \
  -d '{"branch": "51000017", "region_id": "C", "region_name": "กรุงเทพและปริมณฑล",
       "province_id": 1, "province_name": "กรุงเทพมหานคร",
       "timestamp": "2026-09-01 14:10:00", "zone_type": "IT",
       "day_of_week": "Tuesday", "hour": "14",
       "count_in": 31, "count_apple": 5, "count_nb": 9, "nb_to_apple": 2, "apple_to_nb": 1,
       "count_active": 4, "count_not_active": 2, "count_learnhub": 3, "engagement_wait": 42.5,
       "name_zone1": "printer",  "count_zone1": 2,
       "name_zone2": "monitor",  "count_zone2": 3,
       "name_zone3": "-",        "count_zone3": 0,
       "name_zone4": "imac&macbook", "count_zone4": 5,
       "name_zone5": "-",        "count_zone5": 0,
       "name_zone1_mono": "printer", "count_zone1_mono": 1,
       "name_zone2_mono": "monitor", "count_zone2_mono": 0,
       "name_zone3_mono": "-",       "count_zone3_mono": 0,
       "name_zone4_mono": "imac&macbook", "count_zone4_mono": 3,
       "name_zone5_mono": "-",       "count_zone5_mono": 0,
       "name_zone_ce": "apple",      "count_zone_ce": 2,
       "count_zone4_tier1": 1, "count_zone4_tier2": 1, "count_zone4_tier3": 1}'
# → {"id": 55210, "action": "updated"}
```

Verify the round and then each child, following the links the response's `id` gives you:

```sql
-- the round; note its id and its mono_id
SELECT id, mono_id, branch_cuscode, timestamps, zone_type, day_of_week, hour, count_in,
       count_apple, count_nb, nb_to_apple, apple_to_nb, count_active, count_not_active,
       count_learnhub, engagement_wait, insert_date_time
FROM shoping_traffic_hotzone
WHERE branch_cuscode = '51000017' AND timestamps = '2026-09-01 14:10:00' AND zone_type = 'IT';

SELECT * FROM shoping_traffic_hotzone_mono WHERE mono_id    = <the round's mono_id>;
SELECT * FROM emp_demo                     WHERE hotzone_id = <the round's id>;
SELECT * FROM demo_tier                    WHERE hotzone_id = <the round's id>;
```

What each child should show, and when it should be missing:

- **`_mono`** carries `name_zone1..5` / `count_zone1..5`, and the round reaches it by `mono_id`.
  Written only when at least one `count_zone1..5` is above zero; with all five at zero no row is
  written and `mono_id` is never set. On a round globalapi2 touched last, this row can hold counts
  older than the rest of the round: its update is the PHP's broken one, filtering on an `id`
  column the table has not got, so a re-send there dies at that statement having already updated
  the round and `emp_demo`. Our re-send repairs it — so check these counts against the body you
  just sent, not against what was there before.
- **`emp_demo`** carries `name_zone1_mono..5` / `count_zone1_mono..5`, `name_zone_ce`,
  `count_zone_ce`, and `hotzone_id`. **Always written on an insert**, zero demos included — so a
  round we inserted always has one. On an **update** it is updated and never inserted, silently
  doing nothing if the row is absent (the PHP's behaviour, kept: only pre-2024 globalapi2 rounds
  lack one). So on a replayed old round the verify SELECT can come back empty, and that is the
  round's history, not a fault of this call.
- **`demo_tier`** carries `count_zone4_tier1..3` plus `hotzone_id` and `demo_id`. Written only
  when `count_zone4_mono` is above zero, and its `demo_id` must equal the `emp_demo` row's — which
  is why, on an update of a round that has no `emp_demo` row, no `demo_tier` row is written
  either: there is nothing to link it to.

Re-send the identical curl: same id, `"action": "updated"`, and **still one row in each table** —
no duplicated child. That is the whole point of the fan-out being one transaction with a fixed
key. Then check `journalctl -u ai-camera-admin-backend -f` shows no traceback for either call.

### 2f. Reading the answers

Same three shapes on all three feeds:

- **`{"id": <n>, "action": "inserted" | "updated"}`** — the row is on 209 and `id` is the row you
  should SELECT. `"updated"` means a row already existed for
  `(branch_cuscode, timestamps, zone_type)`. On a key you chose because globalapi2 already wrote
  it, that is the expected answer and the deploy is fine. On a key you believed was fresh, it is
  telling you something: either that hour really was sent before, or your `timestamp` collided
  with a row you did not mean to overwrite — check before you send anything else.
- **`400`** with a pydantic `detail` list — the body is wrong and **nothing ran, on either host**.
  A missing or misspelled field, a `timestamp` that is not `YYYY-MM-DD HH:MM:SS`, a `zone_type`
  that is not `IT` or `MONO`, or a `branch` that is not exactly eight digits. The eight-digit rule
  is not cosmetic: `branch` becomes a schema name in the POS query. Fix the body and resend; there
  is nothing to clean up.

  One 400 will not be the operator's fault once clients are switched over: `province_id` and
  `region_id` are required, and the client computes them from a lookup that answers nothing for a
  province or region name spelled differently than the map has it. The PHP stored such a round
  with `province_id` 0; we refuse the whole round. Narrow — the map covers all 76 provinces — but
  it loses a round when it fires. Flagged in issue 03, not yet fixed, and it touches all three
  feeds because the check lives in their shared header.
- **`500`** — nothing was written. The three causes, in the order to check them:
  - the POS host (in-pass only) could not answer — bad or unset `POS_DB_*`, the `SELECT` grant
    of step 2 missing, or the branch has no schema of that name. The POS read runs **first**, on
    purpose, so an outage never stamps an hour with zero bills;
  - 209 refused the write — step 2's grants have not landed on this host;
  - a hotzone round failed part-way, and the transaction rolled it back. No half-written round
    survives a 500; re-send once the cause is fixed.

  `journalctl -u ai-camera-admin-backend -f` carries the traceback. A 500 is always safe to retry — the
  feeds are upserts, so a retry that lands writes the same row, not a second one.

- **`{"action": "shadow"}`** — shadow mode is on (§1): the body was valid and nothing was written,
  on purpose. Expected until cutover; with `TRAFFIC_FEEDS_SHADOW=0` this answer cannot appear.
- **`{"action": "dry_run", "would": "inserted" | "updated", "id": ..., ...}`** — the request carried
  the header `X-Traffic-Dry-Run: 1`, on rate, in-pass or hotzone, sent straight to `127.0.0.1:8011`
  on the box. The frontend does not forward that header, so a Branch cannot use it. It runs the same reads as a real send, in-pass's
  POS queries included, whatever shadow mode says, and answers with the planned `columns` (hotzone:
  `writes`). Nothing is written. In-pass needs `POS_DB_*` for this, so it is the way to test a
  real request on prod before cutover. `/traffic/zone-hour` has no dry run: the header is ignored
  there and the hour is written.
- **`"rollout_no_globalapi2": true` in the body** stores it whatever shadow mode says, and answers as with shadow
  off. The DLC `log_service` that sends only to the admin sets it. A branch still on the older DLC sends
  to globalapi2 too and does not, so it stays shadowed and no two writers race on 209. To move a
  branch, push it that DLC; to move it back, push the older one. Nothing changes on the server.
- Not an answer these endpoints give with shadow off: a **200 that wrote nothing**. That was globalapi2's habit and
  it is the reason this service exists.

## Notes

- **Only the frontend reaches it.** The backend binds 127.0.0.1:8011, and nginx and the frontend
  sit on the same box. Branches reach the feeds only through the frontend's
  `/ai-camera/api/traffic/*` routes, behind the Device Key gate (ADR 0009).
- **One worker is fine** with the traffic feeds on: per branch that is two calls every
  10 minutes (rate, hotzone) plus two an hour (in-pass, zone-hour). Every endpoint that touches a database is
  a sync `def`, so Starlette runs it in its threadpool and one slow POS read does not hold up the
  others.
