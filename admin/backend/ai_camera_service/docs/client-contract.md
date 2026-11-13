# Traffic feeds: client contract

For the developers who build the branch side (DLC `log_service` / `report_service`).
Everything here describes what the server does today. You should not need to read server code.

## At a glance

| Feed | What it is | How often | What the server does with it now | If it fails |
|---|---|---|---|---|
| `rate` | Staffing Ratio sample | every 10 min | checks the body, stores nothing (shadow) | drop it; never block globalapi2 |
| `in-pass` | Traffic Row (walk pass / walk in) | every hour | checks the body, stores nothing (shadow) | drop it; never block globalapi2 |
| `hotzone` | Hotzone Round | every 10 min | checks the body, stores nothing (shadow) | drop it; never block globalapi2 |
| `zone-hour` | Zone Hour: Dwell and Wait per zone | every hour | **stores it. Ours is the only copy** | **resend until you get a 2xx** |

## 1. URLs and headers

```text
POST https://app.advice.th/ai-camera/api/traffic/rate
POST https://app.advice.th/ai-camera/api/traffic/in-pass
POST https://app.advice.th/ai-camera/api/traffic/hotzone
POST https://app.advice.th/ai-camera/api/traffic/zone-hour
```

Every request carries the same three headers:

```text
Content-Type: application/json
X-Activation-Code: <the Branch's Activation Code, ADV-XXXX-XXXX-XXXX>
Authorization: Bearer <the Branch's Device Key>
```

These are the same headers `pipeline_upload` already sends. Take both values from the environment
(`AI_CAMERA_ACTIVATION_CODE`, `AI_CAMERA_DEVICE_KEY`). Never log the Device Key or the
`Authorization` header.

How the server judges them:

- Both headers are required. If one is missing, the answer is 401 and the server does not ask anything else.
- `Bearer` is matched in any case (`bearer`, `BEARER`), and spaces around the key are trimmed.
- The key must belong to that Activation Code, and the key must still be on record. Anything
  else (wrong key, another Branch's code, a key an admin has removed) is 401.
- The client IP does not matter. There is no IP allow-list and no keyless fallback for these four URLs.

## 2. Responses and what to do

| Status | Body | Meaning | What the client does |
|---|---|---|---|
| **200** | `{"action": "shadow"}` | old feed (`rate`, `in-pass`, `hotzone`): the body is valid. Nothing was stored, on purpose | nothing. Done |
| **200** | `{"action": "inserted", "zones": 3}` | `zone-hour`: the hour was stored. No earlier send existed for it | mark the hour sent |
| **200** | `{"action": "replaced", "zones": 3}` | `zone-hour`: an earlier send of this hour was replaced by this one | mark the hour sent |
| **400** | `{"detail": [ ... ]}` | the body is wrong. Nothing was stored | log `detail` and fix the code. **Do not resend the same body:** it will fail the same way |
| **401** | `{"error": "invalid_device_key"}` | the Device Key was not accepted | do not retry with the same key. The Branch must re-activate to get a new Device Key (if it still holds a key, an admin must remove it first). Keep unsent Zone Hours and send them once a new key is present |
| **503** | `{"error": "Upstream service unavailable"}` | the server could not check the key, or could not reach the feed service | temporary. Retry later with the same body. **Do not re-activate** |
| **500** | any | the server failed while storing. For `zone-hour`, the previous send of that hour (if any) is still in place | `zone-hour`: retry later. Old feeds: drop it |

`zone-hour` treats every non-2xx answer and every network error or timeout the same way: keep the
hour and send it again later. A resend is always safe (section 4.2).

`detail` in a 400 is a list. Each item has `loc`, which is the path to the bad field, and `msg`.
Example:

```text
{"detail": [{"type": "greater_than_equal", "loc": ["body", "zones", 0, "dwell_count"],
             "msg": "Input should be greater than or equal to 0", "input": -1, "ctx": {"ge": 0}}]}
```

A request to the old `/traffic/zone-service` URL now gets 403 or 404. That route has been removed.

## 3. The three old feeds: `rate`, `in-pass`, `hotzone`

### 3.1 Delivery rule: a fire-and-forget copy

globalapi2 is still the system of record for these three feeds. Keep sending to globalapi2 exactly
as you do today. Also send a **copy** of the same values, as JSON, to our URL.

- The copy **must never delay or block the send to globalapi2.** Send to globalapi2 first, or send
  the copy in the background with a short timeout. A failure or timeout from us must not stop,
  slow down, or retry the globalapi2 send.
- Do not queue or retry the copy. Whatever it answers, move on. Log a 400. Its `detail` shows a
  body problem we want fixed before cutover.
- A valid copy answers `200 {"action": "shadow"}` straight away. The server does not read or write
  any database for it, so it is fast.
- Later, at cutover, the server will start storing these feeds. The answer will then be
  `{"id": <n>, "action": "inserted" | "updated"}`. The URLs and bodies stay the same. Until we tell
  you, treat any 2xx as done.

### 3.2 Fields every feed carries (the header)

| Field | Type | Rule | Your value today |
|---|---|---|---|
| `branch` | **string** | exactly 8 digits, e.g. `"51000017"`. A JSON number is refused | `str(store["id"])` |
| `region_id` | string | required, not `null` | `REGION_MAP.get(region)`. Use the `"Non"` fallback that `zone_service.py` already uses |
| `region_name` | string | required | `store["region"]` |
| `province_id` | integer | required, not `null` | `PROVINCE_MAP.get(province)`. Use the `0` fallback that `zone_service.py` already uses |
| `province_name` | string | required | `store["province"]` |
| `timestamp` | string | `YYYY-MM-DD HH:MM:SS`, Thai local time. `T` or a time zone is refused | `data["timestamp"]` |
| `zone_type` | string | exactly `"IT"` or `"MONO"` (upper case) | `branch_type.upper()` |

Field names are case-sensitive. A missing field gives 400. Extra fields are ignored. If a value is a
MySQL `Decimal` (for example from `ROUND(AVG(...))`), convert it to `int` or `float` first. Python's
`json` cannot encode a `Decimal`.

### 3.3 `rate`: one Staffing Ratio sample

The header above, plus:

| Field | Type | Your value today |
|---|---|---|
| `hour` | **string**, e.g. `"14"` (a JSON number is refused) | `data["hour"]` |
| `avg_emp` | number | `data["avg_emp"]` |
| `avg_cus` | number | `data["avg_cus"]` |
| `rate` | number | `data["rate_ratio"]` |

There is no `day_of_week` for this feed.

### 3.4 `in-pass`: one Traffic Row

The header above, plus:

| Field | Type | Your value today |
|---|---|---|
| `day_of_week` | string, e.g. `"Tuesday"` | `data["date_of_week"]` |
| `hour` | integer, e.g. `13` | `data["hour"]` (already `int(hour) - 1`) |
| `walk_pass` | integer | `data["walk_pass"]` |
| `walk_in` | integer | `data["walk_in"]` |
| `rate` | number | `data["rate_ratio"]` |

Do not send bill counts. The server counts them itself.

### 3.5 `hotzone`: one Hotzone Round

The header above, plus 36 fields. Every field is required, including zeros and unused (`"-"`) zones.

| Field | Type | Your value today |
|---|---|---|
| `day_of_week` | string | `data["date_of_week"]` |
| `hour` | **string**, e.g. `"14"` | `data["hour"]` |
| `count_in` | integer | `data["count_in"]` |
| `count_apple` | integer | `data["count_apple"]` |
| `count_nb` | integer | `data["count_nb"]` |
| `nb_to_apple` | integer | `data["count_nb_t_apple"]` |
| `apple_to_nb` | integer | `data["count_apple_t_nb"]` |
| `count_active` | integer | `data["count_charged"]` |
| `count_not_active` | integer | `data["count_not_charged"]` |
| `count_learnhub` | integer | `data["count_learning_hub"]` |
| `engagement_wait` | number | `data["engagement_wait"]` |
| `name_zone1` … `name_zone5` | string | `mono_data["mono1"]["name"]` … `["mono5"]` |
| `count_zone1` … `count_zone5` | integer | `mono_data["mono1"]["count"]` … `["mono5"]` |
| `name_zone1_mono` … `name_zone5_mono` | string | `mono_data["mono1"]["name"]` … `["mono5"]` (the same names again) |
| `count_zone1_mono` … `count_zone5_mono` | integer | `sum(demo_data["demo_mono1"].values())` … `["demo_mono5"]` |
| `name_zone_ce` | string | `"ce"` |
| `count_zone_ce` | integer | `sum(demo_data["demo_ce"].values())` |
| `count_zone4_tier1` … `count_zone4_tier3` | integer | `demo_data["demo_mono4"]["tier_1"]` … `["tier_3"]` |

These are the same values you put in the globalapi2 array today, now with names. Where our name
differs from your `data` key (`nb_to_apple`, `apple_to_nb`, `count_active`, `count_not_active`,
`count_learnhub`, `day_of_week`), the table shows which key to use.

## 4. Zone Hour: `zone-hour`

### 4.1 What one body is

One body = **one hour** of **one branch** + **one Zone Type** (`IT` or `MONO`), with every zone of
that hour in `zones[]`. A branch that runs both Zone Types sends two bodies per hour.

The server identifies an hour by `branch` + `zone_type` + `timestamp`. It keeps one entry per zone.

### 4.2 Delivery rules

- **Resend until you get a 2xx.** The server's copy is the only one. If you drop an hour, it is lost.
  Keep the hour on disk until it gets a 2xx. That way a restart of the branch box or of the server
  loses nothing.
- **A resend is safe.** It **replaces** the whole hour: every zone stored earlier for that branch +
  Zone Type + `timestamp` is deleted, and the zones in the new body are inserted, all in one step.
  Nothing is ever counted twice.
- **A zone missing from a resend is removed.** The stored hour always equals the last body that got
  a 2xx.
- **`"zones": []` is valid.** It stores nothing for the hour, and it clears any zones an earlier send
  left. Send it for an hour with no zone activity.
- You may resend a past hour later with better numbers, for example once the visits that started in
  that hour have ended. The latest 2xx wins.
- If a send fails part-way (500), the previous send of that hour stays as it was.
- Use the **same `timestamp` text** for every send of one hour. Use the start of the hour
  (`2026-10-15 10:00:00` covers 10:00:00–10:59:59). The server rewrites a loose spelling to the
  canonical form (`2026-10-15 10:0:0` → `2026-10-15 10:00:00`), so both replace the same hour. Any
  other time, for example `10:05:00`, is a different hour key.

### 4.3 Body fields

Top level: the header from section 3.2 (`branch`, `region_id`, `region_name`, `province_id`,
`province_name`, `timestamp`, `zone_type`, with the same rules), plus:

| Field | Type | Rule |
|---|---|---|
| `day_of_week` | string | English weekday, e.g. `"Thursday"`. Stored as sent, not checked against `timestamp` |
| `hour` | integer | hour of day, `0`–`23`. Stored as sent, not checked against `timestamp`. Send the hour of `timestamp` |
| `long_wait_seconds` | number | the Long Wait threshold this branch used for this hour, e.g. `180` |
| `zones` | array | 0 to **200** zone objects. Required (send `[]` for none) |

Each object in `zones`:

| Field | Type | Rule | Meaning (section 4.5) |
|---|---|---|---|
| `zone_name` | string | not empty or only spaces. Stored exactly as sent. **Unique within the body** | the zone, e.g. `"Macbook"` |
| `dwell_sum_seconds` | number | ≥ 0 | total of the Dwells in this zone and hour, in seconds |
| `dwell_count` | integer | ≥ 0 | number of Dwells (one per Person) |
| `wait_sum_seconds` | number | ≥ 0 | total of the Waits, in seconds |
| `wait_count` | integer | ≥ 0 | number of Waits |
| `long_wait_count` | integer | ≥ 0 | number of Long Waits |
| `unserved_count` | integer | ≥ 0 | number of Unserved customers |

Things that give 400:

- the same `zone_name` twice in one body
- more than 200 zones
- a negative number
- a blank `zone_name`
- any header rule from section 3.2

`zone_name` is compared exactly. `"iPhone"` and `"iphone"` count as two zones, so merge case
variants before sending, as your `merge_rows` already does. Send totals and counts only, never
averages. The reader divides after adding hours, zones and branches together.

### 4.4 Example

The server accepts this body as written (the test suite posts this block from this file):

```json
{
  "branch": "51000017",
  "region_id": "C",
  "region_name": "กรุงเทพและปริมณฑล",
  "province_id": 1,
  "province_name": "กรุงเทพมหานคร",
  "timestamp": "2026-10-15 10:00:00",
  "zone_type": "IT",
  "day_of_week": "Thursday",
  "hour": 10,
  "long_wait_seconds": 180,
  "zones": [
    {"zone_name": "Macbook", "dwell_sum_seconds": 1920, "dwell_count": 6,
     "wait_sum_seconds": 376.97, "wait_count": 4, "long_wait_count": 1, "unserved_count": 0},
    {"zone_name": "iPhone", "dwell_sum_seconds": 845.5, "dwell_count": 5,
     "wait_sum_seconds": 210.0, "wait_count": 3, "long_wait_count": 2, "unserved_count": 2}
  ]
}
```

Answer: `200 {"action": "inserted", "zones": 2}` the first time. Every later send of this hour
answers `{"action": "replaced", ...}`.

In the `iPhone` zone, `long_wait_count` is 2. That can be one Wait of 180 s or more, plus one of the
two Unserved customers, whose stay reached 180 s.

### 4.5 Field definitions

These match the glossary the dashboards are built on. Count by these definitions so the numbers
mean what the dashboard shows.

- **Zone Hour**: one hour of one hotzone zone (Macbook, iPhone, …) at one branch + Zone Type. It holds
  the Dwells recorded there, as a total and a count, and the Waits, as a total and a count. It also
  holds how many were Long Waits and how many customers went Unserved. Totals and counts are kept,
  never averages, so hours, zones and branches can be added up before dividing.
- **Dwell**: the time one **Person** spends inside one hotzone zone, added up across all of their
  Tracks there. If they step out for more than about two seconds, the Dwell pauses. If they come
  back, it continues. A Person whose Dwell never reaches the zone's threshold is not recorded at all.
  A **Track** is one camera's short-lived following of a figure. One human gives many Tracks.
  A **Person** is one human recognised by ReID across Tracks and cameras. So `dwell_count` counts
  Persons, not Tracks.
- **Wait**: the time from a customer entering a zone to an employee entering the same zone. It is
  recorded only when the customer arrived first and is still there. Once per customer per zone.
- **Long Wait**: a customer who waited **at least** (≥) the threshold the branch sends in
  `long_wait_seconds` (180 seconds by default). That is either a Wait of that length, or an Unserved
  customer whose stay in the zone reached the threshold, since they were waiting the whole time.
- **Unserved**: a customer who entered a zone and left it before any employee came. An Unserved
  customer has no Wait, so they are never in `wait_sum_seconds` or `wait_count`. They do count as
  a Long Wait if their stay reached the threshold.

## 5. What changes for you (from the Zone Service Day sender)

Your current sender (`log_service/zone_service.py` and `report_service/zone_service.py`, posting
to `/traffic/zone-service`) is replaced. That route has been removed from the server: it now
answers 403 or 404, and nothing is stored. The changes:

| | Today (`zone-service`) | Now (`zone-hour`) |
|---|---|---|
| URL | `…/ai-camera/api/traffic/zone-service` (`ZONE_SERVICE_URL`) | `…/ai-camera/api/traffic/zone-hour` |
| How often | the whole day, resent every hour, marked sent once the day closes | **one body per hour**, per Zone Type |
| `timestamp` | `"<day> 00:00:00"` | the start of **that hour**, `"<day> HH:00:00"` |
| `hour` | inside each row | **top level**, one per body |
| Per-zone data | `rows[]`, one per (hour, zone), each with `hour` | `zones[]` for that hour only. **No `hour` inside a zone** |
| Retry and marker | per day (`upload_state/zone_service/<day>.json`, `ZONE_SERVICE_LOOKBACK_DAYS`) | per hour: keep each hour until it gets a 2xx |
| Success check | `status_code == 200` | any 2xx (today it is always 200) |
| Dwell | one per **Track** | one per **Person** (see below) |

What stays the same:

- the Device Key headers (`_headers()`)
- the header fields and their fallbacks (`"Non"` / `0`)
- `long_wait_seconds`
- the sums-and-counts approach, `merge_rows`' case-insensitive zone merge, and sending an empty
  list to clear

**Dwell must count per Person.** In both files, `DWELL_SQL` groups its inner query by
`GROUP BY name, ip, channel, track_id`, which makes one Dwell per Track per camera. A Person who
is lost and picked up again, or who is seen by two cameras, becomes several short Dwells. That
raises `dwell_count` and lowers the average. Count one Dwell per Person per zone, adding the time
of all of their Tracks there (section 4.5).

**The Wait side already matches.** `WAIT_SQL` meets all of these definitions:

- `wait_sum` and `wait_count` take only served rows.
- `long_wait_count` uses `>=`, and it includes Unserved customers whose stay
  (`TIMESTAMPDIFF(SECOND, customer_enter_time, timestamp)`) reached the threshold.
- `unserved_count` counts rows with no employee.

Keep it, but run it per hour instead of per day.

**Which hour a Dwell or Wait goes in.** Keep your current rule: the hour it started
(`HOUR(first_seen)` for Dwell, `HOUR(customer_enter_time)` for Wait). The server does not check it.

**Old feeds.** Add the fire-and-forget JSON copy from section 3 beside each globalapi2 send:
`rate_log`, `in_pass_log` and `fetch_hotzone_data` in `log_service/job_send_logs.py`, and the
senders that use `report_service/payload.py`. It is the same values you already put in the
globalapi2 array.

## 6. Floorplan heat: the pipeline zip → `floorplan_hour`

The Heatmap Floorplan card on Alitech is fed by the **pipeline zip** the branch already uploads
every hour. There is no `/traffic/` feed for it. The server preprocesses each zip into one record
per hour and keeps them in the Zone Hour SQLite file. Alitech reads them there.

### 6.1 The whole road

```text
Branch (log_service/pipeline_upload.py, every hour at minute 0)
  zips   pipeline/ (except pipeline_upload/, raw_upload/, zone_service/) + manifest.json
         floorplan/calibration.json, zone.json, history.json, images/<picture>   floorplan_service
         heatmap/buckets/camera/<ip>_<ch>/*.npz, heatmap/observations/*.jsonl      heatmap_service
         assignments/reid_*.jsonl, cusemp_*.jsonl                                  reid / cus_emp
  sends  POST https://app.advice.th/ai-camera/api/dummy-data/upload   (multipart: store_id + file)
            ↓  admin frontend gate (branch_rest) → backend /dummy-data/upload
Server (admin backend, data_service/core/services/pipeline_service.py)
  keeps  dummy_data/<store_id>/pipeline_<start_ts>.zip               the zip as sent (1 day)
  makes  processed/<store_id>/heatmaps/floorplan/<day>_<HH>00_<type>_<floorplan>.npz
         processed/<store_id>/floorplans/<picture>_<sha12>.png
         processed/<store_id>/heatmaps/<day>_<type>_<floorplan>.manifest.json
  writes <ZONE_HOUR_DB_PATH>  tables floorplan_hour, floorplan_hour_zone (absolute paths to the files above)
            ↓  read-only, same machine
Alitech  GET /my-products/ai-camera/analytics/data?section=floor-heatmap
         GET /my-products/ai-camera/analytics/floorplan-image/<branch>/<picture file name>
```

The zip is preprocessed as soon as it lands, and again by the queue, which runs every 60 s and
picks up any zip not yet done. Doing it twice is safe (section 6.5).

### 6.2 The upload

```text
POST https://app.advice.th/ai-camera/api/dummy-data/upload
Content-Type: multipart/form-data
X-Activation-Code / Authorization: Bearer …   (as section 1, when the branch has a Device Key)

store_id = 51000001
file     = pipeline.zip
```

- **Who may send:** with a Device Key, the key must be valid. Without one, the request passes only
  from the IP list (`TRANSITION_IPS`) for now.
- **Answer:**
  ```json
  {"message": "Pipeline zip uploaded successfully", "filename": "pipeline_1792130700.zip",
   "path": "dummy_data/51000001/pipeline_1792130700.zip", "start_ts": 1792130700.1505046}
  ```
- **Retries:** the branch deletes uploaded hourly files only after a 2xx, so a failed upload is
  zipped again at the next hour.

### 6.3 What the zip must carry

`manifest.json`. A real one, from store `51000001`:

```json
{
  "store_id": "51000001",
  "branch_types": ["it", "mono"],
  "cameras": {"192.168.20.240_10": "it", "192.168.20.240_14": "it", "192.168.20.240_15": "it",
              "192.168.20.240_21": "it", "192.168.20.241_3": "mono", "192.168.20.241_7": "mono",
              "192.168.20.241_12": "mono", "192.168.20.241_16": "mono"},
  "start_ts": 1792130700.1505046,
  "uploaded_at": 1792134000.046338
}
```

`cameras` gives each camera's counter type. That type becomes a record's `branch_type`, and
Alitech shows it as the branch `51000001 IT` / `51000001 MONO`.

`floorplan/calibration.json` (plain JSON inside the zip):

| Key | What | Used for |
|---|---|---|
| `calibrations[]` | `camera_ip`, `camera_channel`, `floorplan_id`, `image_url`, … per calibrated camera | which floorplan a camera's points land on, and that floorplan's picture |
| `floorplans[]` | `floorplan_id`, `image_url`, `image_updated_at` (unix, set only when the picture is replaced), `previous_image_url` | the current picture of each floorplan, and when it came in |
| `floorplan_versions[]` | `floorplan_id`, `image_url`, `valid_from` (unix; 0 = covers all before) | every picture since the last upload, so a picture replaced twice between uploads is not lost |

`floorplan/images/<file name of image_url>` holds the picture of every version listed. The
server matches a picture to its floorplan by this **file name**, so it must be the same name as
in `image_url` (`5eb9da92bead4dc99d451ac0a16196b2.png`). Only PNG or JPEG bytes count. A
`.DS_Store` or `__MACOSX/` entry is ignored.

`heatmap/buckets/camera/<ip>_<channel>/<YYYYMMDD>_<HHMMSS>.npz` holds the 5-minute grids. The
preprocess adds a floorplan's cameras together, hour by hour.

### 6.4 One record = one hour of one floorplan on one picture

The preprocess makes one payload per **branch_type × floorplan × picture version × hour**. Data
is split at a picture's `valid_from`: rows from before the swap stay with the old picture. A real
payload (store `51000001`, IT, 16 Oct 2026 13:00), with only 2 of its 5 zones shown:

```json
{
  "store_id": "51000001",
  "branch_type": "it",
  "start_ts": 1792130400,
  "floorplan_id": 2,
  "floorplan_valid_from": null,
  "avg_spent_seconds": 65,
  "median_spent_seconds": 14.6,
  "zones": [
    {"id": "zone-1770113930120", "name": "Notebook", "type": "hotzone", "subtype": null,
     "avg_spent_seconds": 65.1, "visit_count": 360},
    {"id": "rate-1770104529129", "name": "", "type": "rate", "subtype": "employee",
     "avg_spent_seconds": 51.9, "visit_count": 9}
  ],
  "floorplan_file_path": "processed/51000001/floorplans/5eb9da92bead4dc99d451ac0a16196b2_96dde1de25d7.png",
  "heatmap_file_path": "processed/51000001/heatmaps/floorplan/20261016_1300_it_2.npz",
  "heatmap_manifest_path": "processed/51000001/heatmaps/20261016_it_2.manifest.json"
}
```

| Field | Meaning |
|---|---|
| `start_ts` | start of the hour, unix seconds (Bangkok 13:00 here) |
| `floorplan_valid_from` | when this picture came into use, unix. `null` = the first picture, which covers everything before |
| `avg_spent_seconds`, `median_spent_seconds` | time customers spent in the floorplan that hour, per Person |
| `zones[]` | each zone of `zone.json` on this floorplan: average time inside and `visit_count`, the people who stayed at least 10 s |
| `zones[].points`, `zones[].color` | **hotzones only.** The polygon the store drew, carried from the camera frame onto the floorplan: `[[x, y], ...]` in percent of the picture (0-100), and its colour. `null` when the polygon could not be placed (below) |
| `heatmap_file_path` | the hour's grid: an `.npz` with `unique_grid`, 108 × 192, over the whole picture |
| `floorplan_file_path` | this version's picture (`null` when the zip carried none) |

**Zone polygons.** `zone.json` holds each polygon on its camera's frame (0-1). The preprocess
carries it onto the floorplan with that camera's `matrix` from `calibration.json`. The matrix is a
fit of the points the installer placed and runs away from them, so a polygon is placed only when
every corner lands within 6 % of the plan of the hull of the camera's calibrated floorplan points.
A zone outside that area has `points: null` and is not drawn: better absent than across the wrong
room. Nothing changes on the branch; the zip already carries both files.

`unique_grid` adds up, per cell, the people counted in each 5-minute bucket. It measures
**presence**, not visitors. Never show it as a number of customers.

### 6.5 What the server stores

Both tables live in the SQLite file at `ZONE_HOUR_DB_PATH`, beside `zone_hour`. They are made on
the first write.

```sql
-- 1 row per payload (branch_type × floorplan × picture version × hour)
CREATE TABLE floorplan_hour (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id              TEXT    NOT NULL,
    branch_type           TEXT    NOT NULL,   -- it / mono
    start_ts              REAL    NOT NULL,   -- start of the hour (unix)
    floorplan_id          INTEGER NOT NULL,
    floorplan_valid_from  REAL    NOT NULL DEFAULT 0,  -- payload null -> 0 (first version)
    avg_spent_seconds     REAL,
    median_spent_seconds  REAL,
    floorplan_file_path   TEXT,               -- this version's floorplan picture
    heatmap_file_path     TEXT    NOT NULL,
    heatmap_manifest_path TEXT,
    created_at            TEXT    NOT NULL,   -- insert time (Thai local)
    UNIQUE (store_id, branch_type, floorplan_id, floorplan_valid_from, start_ts)
);

-- 1 row per zone of that hour (payload["zones"])
CREATE TABLE floorplan_hour_zone (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    floorplan_hour_id   INTEGER NOT NULL REFERENCES floorplan_hour(id) ON DELETE CASCADE,
    zone_id             TEXT    NOT NULL,   -- zones[].id
    zone_name           TEXT    NOT NULL,   -- zones[].name
    zone_type           TEXT    NOT NULL,   -- zones[].type: hotzone / rate / in_pass / attendance
    zone_subtype        TEXT,               -- zones[].subtype
    avg_spent_seconds   REAL,               -- zones[].avg_spent_seconds
    visit_count         INTEGER NOT NULL,   -- zones[].visit_count
    zone_color          TEXT,               -- zones[].color, hotzones placed on the floorplan only
    zone_points         TEXT,               -- zones[].points as JSON [[x, y], ...], percent 0-100
    UNIQUE (floorplan_hour_id, zone_type, zone_id)
);
```

`zone_color` / `zone_points` came later: a file made without them gets them (`ALTER TABLE`) on its
next write, and rows from before stay `NULL` (no polygons until their hour is preprocessed again).

- **The three paths:** stored **absolute**, so a reader on the same machine opens them as they are.
  The files stay under `processed/`.
- **A later zip with the same hour** (same store, type, floorplan, version, `start_ts`) replaces
  that row, and its zones are replaced with it. Nothing is counted twice.
- **`ZONE_HOUR_DB_PATH` not set:** nothing is stored and a warning is logged. The zip is still
  preprocessed.

### 6.6 What Alitech does with it

- **`section=floor-heatmap`:** reads the range's `floorplan_hour` rows, opens each
  `heatmap_file_path` and adds the hours of a day together. It answers one cell per branch × day ×
  floorplan × version:

  ```json
  {"branch": "51000001 IT", "branchCode": "51000001", "day": "2026-10-16", "zoneType": "IT",
   "floorplanId": 2, "floorplanName": "Floorplan 2", "versionSince": "2000-01-01 00:00:00",
   "image": "5eb9da92bead4dc99d451ac0a16196b2_96dde1de25d7.png",
   "gridW": 192, "gridH": 108, "unique": 7887.0, "samples": 0.0, "hours": 1,
   "cells": [[13, 2.0, 0.0], "…"]}
  ```

- **Hotzone polygons:** a day's cell, and the `picture` row, carry `zones: [{id, name, color, points}]`
  when the floorplan has any: the hotzones of the version's **latest hour** of that day (of the
  range's latest day on the card), so a zone renamed or deleted since an earlier hour shows under
  today's name or not at all. The key is absent with none. The card draws them over the heat.
- **The picture:** `/floorplan-image/<branch>/<file name>` serves a picture only when a
  `floorplan_hour` row of that branch names it. A store is never drawn on another store's picture.
  A version with no picture gets its heat on a plain background.
- **Versions:** a floorplan whose picture changed within the range gets Ver.1, Ver.2, … in the
  card's top-right corner (buttons up to 3, a dropdown from 4). Each version is drawn on its own
  picture.
- **Floor filter:** reads "1st Floor" only, since no branch has a second floor yet.

