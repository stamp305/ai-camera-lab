"""Staffing Ratio feed tests: POST /traffic/rate upserts one row in shoping_traffic_avg
(issue 01, the walking skeleton for the three traffic feeds).

Seams (pre-agreed in .scratch/ai-camera-traffic-feed/spec.md, Testing Decisions):
  - the HTTP endpoint via starlette.testclient.TestClient
  - mysql.connector replaced with the recording fake in tests/fakedb.py
  - the planner called directly, with no database at all — the seam the parity
    verifier (issue 04) drives

Run (from ai-camera-admin/backend/ai_camera_service/):
    ../.venv/bin/python -m unittest -v
"""
import unittest
from datetime import datetime

from starlette.testclient import TestClient

from tests.fakedb import FakeDBTestCase, written_columns

import app as appmod

AVG_TABLE = "shoping_traffic_avg"

# The columns the PHP writes for this feed, in the order it merges them (spec §Per-endpoint
# fields). day_of_week is deliberately absent — the PHP never stored one here.
EXPECTED_COLUMNS = ["branch_cuscode", "region_id", "region_name", "province_id",
                    "province_name", "timestamps", "insert_date_time",
                    "hour", "avg_emp", "avg_cus", "rate", "zone_type"]


class StaffingRatioTestCase(FakeDBTestCase):
    """A valid Staffing Ratio body; no existing row unless a test cans one."""

    def post(self, **overrides):
        payload = {
            "branch": "10000001",
            "region_id": "N",
            "region_name": "ภาคเหนือ",
            "province_id": 50,
            "province_name": "เชียงใหม่",
            "timestamp": "2026-08-18 09:50:00",
            "zone_type": "MONO",
            "hour": "09",
            "avg_emp": 2.5,
            "avg_cus": 5.0,
            "rate": 0.5,
        }
        payload.update(overrides)
        return self.client.post("/traffic/rate", json=payload)

    def existing_row(self, row_id):
        self.db.rows[AVG_TABLE] = [{"id": row_id}]

    def the_write(self):
        """The single write statement, or fails the test."""
        writes = self.db.writes()
        self.assertEqual(len(writes), 1)
        return writes[0]


class TestInsert(StaffingRatioTestCase):
    def test_no_existing_row_inserts_one_and_answers_with_its_id(self):
        resp = self.post()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"id": 101, "action": "inserted"})   # fake's first id

    def test_insert_carries_every_column_and_no_day_of_week(self):
        self.post()
        sql, params = self.the_write()
        self.assertTrue(sql.startswith(f"INSERT INTO {AVG_TABLE} "))
        columns = written_columns(sql, params)
        self.assertEqual(list(columns), EXPECTED_COLUMNS)
        self.assertNotIn("day_of_week", columns)

    def test_insert_values_are_the_body_verbatim(self):
        self.post()
        columns = written_columns(*self.the_write())
        insert_date_time = columns.pop("insert_date_time")     # server clock, checked below
        self.assertEqual(columns, {
            "branch_cuscode": "10000001", "region_id": "N", "region_name": "ภาคเหนือ",
            "province_id": 50, "province_name": "เชียงใหม่",
            "timestamps": "2026-08-18 09:50:00",
            "hour": "09", "avg_emp": 2.5, "avg_cus": 5.0, "rate": 0.5,
            "zone_type": "MONO"})
        datetime.strptime(insert_date_time, appmod.DT_FMT)      # server-filled, parseable

    def test_hour_keeps_the_client_s_zero_padding(self):
        # The client sends "09"; nothing here may turn it into 9, whatever the column type is.
        self.post()
        self.assertEqual(written_columns(*self.the_write())["hour"], "09")

    def test_insert_is_committed(self):
        self.post()
        self.assertEqual(self.db.commits, 1)


class TestUpsertKey(StaffingRatioTestCase):
    def test_lookup_is_branch_timestamps_and_zone_type_on_the_avg_table(self):
        self.post()
        sql, params = self.db.calls[0]
        self.assertIn(f"FROM {AVG_TABLE}", sql)
        self.assertIn("branch_cuscode = %s", sql)
        self.assertIn("timestamps = %s", sql)
        self.assertIn("zone_type = %s", sql)
        self.assertEqual(params, ("10000001", "2026-08-18 09:50:00", "MONO"))

    def test_a_loosely_spelled_timestamp_reaches_lookup_and_write_canonical(self):
        """strptime takes "9:50:0"; what reaches 209 is the one spelling, DT_FMT's."""
        self.post(timestamp="2026-08-18 9:50:0")
        self.assertEqual(self.db.calls[0][1][1], "2026-08-18 09:50:00")
        self.assertEqual(written_columns(*self.the_write())["timestamps"], "2026-08-18 09:50:00")

    def test_lookup_runs_before_any_write(self):
        self.post()
        self.assertTrue(self.db.calls[0][0].upper().startswith("SELECT"))


class TestUpdate(StaffingRatioTestCase):
    def test_existing_row_is_updated_by_its_id_and_answered_with_it(self):
        self.existing_row(77)
        resp = self.post()
        self.assertEqual(resp.json(), {"id": 77, "action": "updated"})
        sql, params = self.the_write()
        self.assertTrue(sql.startswith(f"UPDATE {AVG_TABLE} SET "))
        self.assertTrue(sql.endswith("WHERE id = %s"))
        self.assertEqual(written_columns(sql, params)["id"], 77)

    def test_update_refreshes_every_column_including_insert_date_time(self):
        # The PHP overwrites insert_date_time on a re-send; the backfill's "seeded row"
        # heuristic already lives with that (spec §Upsert key and rule).
        self.existing_row(77)
        self.post()
        columns = written_columns(*self.the_write())
        del columns["id"]
        self.assertEqual(list(columns), EXPECTED_COLUMNS)

    def test_resend_updates_instead_of_inserting_a_second_row(self):
        self.post()                                  # first send: no row yet → INSERT
        self.existing_row(101)                        # ... now it exists
        self.post()
        kinds = [sql.split()[0] for sql, _ in self.db.writes()]
        self.assertEqual(kinds, ["INSERT", "UPDATE"])

    def test_several_existing_rows_update_the_first(self):
        # As the PHP does: it takes [0] of the result set and updates that id.
        self.db.rows[AVG_TABLE] = [{"id": 77}, {"id": 78}, {"id": 79}]
        resp = self.post()
        self.assertEqual(resp.json(), {"id": 77, "action": "updated"})
        self.assertEqual(written_columns(*self.the_write())["id"], 77)


class TestValidation(StaffingRatioTestCase):
    def test_branch_that_is_not_exactly_eight_digits_is_rejected(self):
        # Not cosmetic: /traffic/in-pass interpolates branch as a POS schema name.
        for bad in ("1000001", "100000012", "1000000a", "", "10000001 ", "'; DROP"):
            self.assertEqual(self.post(branch=bad).status_code, 400, bad)

    def test_timestamp_outside_the_stored_format_is_rejected(self):
        for bad in ("2026/08/18 09:50:00", "18-08-2026 09:50:00",
                    "2026-08-18T09:50:00", "2026-08-18", "not a date"):
            self.assertEqual(self.post(timestamp=bad).status_code, 400, bad)

    def test_zone_type_outside_it_mono_is_rejected(self):
        for bad in ("GADGET", "mono", "it", ""):
            self.assertEqual(self.post(zone_type=bad).status_code, 400, bad)

    def test_missing_field_is_rejected(self):
        payload = {"branch": "10000001"}
        self.assertEqual(self.client.post("/traffic/rate", json=payload).status_code, 400)

    def test_both_zone_types_are_accepted(self):
        for zone in ("IT", "MONO"):
            self.assertEqual(self.post(zone_type=zone).status_code, 200, zone)

    def test_province_id_is_accepted_as_the_string_the_client_s_map_holds(self):
        # log_service's PROVINCE_MAP holds '50', not 50 — the body may carry either.
        resp = self.post(province_id="50")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(written_columns(*self.the_write())["province_id"], 50)

    def test_region_id_is_a_code_not_a_number(self):
        # log_service's REGION_MAP holds "N"/"NE"/"C" — a numeric type here would reject them.
        self.assertEqual(self.post(region_id="NE").status_code, 200)

    def test_rejected_bodies_never_reach_the_database(self):
        self.post(branch="123")
        self.post(timestamp="garbage")
        self.post(zone_type="GADGET")
        self.assertEqual(self.db.calls, [])


class TestMalformedBody(StaffingRatioTestCase):
    """spec §Responses gives a malformed body 400, not FastAPI's default 422."""

    def test_body_shape_error_is_400(self):
        self.assertEqual(self.client.post("/traffic/rate", json={}).status_code, 400)


class TestDatabaseFailure(StaffingRatioTestCase):
    """A failed write must surface as 500 — never a 200 that wrote nothing."""

    def setUp(self):
        super().setUp()
        self.client = TestClient(appmod.app, raise_server_exceptions=False)

    def test_insert_failure_answers_500(self):
        self.db.fail_on = "INSERT"
        self.assertEqual(self.post().status_code, 500)

    def test_update_failure_answers_500(self):
        self.existing_row(77)
        self.db.fail_on = "UPDATE"
        self.assertEqual(self.post().status_code, 500)

    def test_lookup_failure_answers_500_and_writes_nothing(self):
        self.db.fail_on = "SELECT"
        self.assertEqual(self.post().status_code, 500)
        self.assert_zero_writes()


class TestFakeRoutesByExactTable(FakeDBTestCase):
    """The fake itself: canned rows belong to one table name and no other. Issues 02 and 03
    lean on this — shoping_traffic_hotzone_mono must never be served the hotzone table's rows."""

    def test_a_table_never_serves_a_look_alike_table_s_rows(self):
        self.db.rows["shoping_traffic_hotzone"] = [{"id": 1}]
        self.db.rows["shoping_traffic_hotzone_mono"] = [{"id": 2}]
        rows = appmod.run_query("SELECT id FROM shoping_traffic_hotzone_mono WHERE id = %s", (2,))
        self.assertEqual(rows, [{"id": 2}])

    def test_a_table_with_no_canned_rows_returns_none(self):
        self.db.rows["shoping_traffic_hotzone"] = [{"id": 1}]
        self.assertEqual(appmod.run_query("SELECT id FROM emp_demo"), [])

    def test_a_schema_qualified_table_keys_on_its_whole_name(self):
        # Issue 02 reads `10000001`.ar_invoice on the POS host; the schema half alone
        # must not become the key, or every branch's POS rows would collide.
        self.db.rows["10000001.ar_invoice"] = [{"total_bill": 4}]
        rows = appmod.run_query("SELECT COUNT(*) AS total_bill FROM `10000001`.ar_invoice")
        self.assertEqual(rows, [{"total_bill": 4}])

    def test_insert_hands_back_a_new_id_each_time(self):
        first = appmod.run_query("INSERT INTO emp_demo (hotzone_id) VALUES (%s)", (1,), commit=True)
        second = appmod.run_query("INSERT INTO emp_demo (hotzone_id) VALUES (%s)", (2,), commit=True)
        self.assertEqual((first, second), (101, 102))


class TestPlannerIsPure(unittest.TestCase):
    """The planner runs with no database, no fake and no HTTP — this is what issue 04's
    parity verifier calls to replay rows globalapi wrote."""

    def body(self, **overrides):
        fields = {
            "branch": "10000001", "region_id": "N", "region_name": "ภาคเหนือ",
            "province_id": 50, "province_name": "เชียงใหม่",
            "timestamp": "2026-08-18 09:50:00", "zone_type": "IT",
            "hour": "09", "avg_emp": 2.5, "avg_cus": 5.0, "rate": 0.5,
        }
        fields.update(overrides)
        return appmod.StaffingRatioRequest(**fields)

    def test_no_existing_row_plans_an_insert(self):
        action, columns = appmod.plan_staffing_ratio(self.body(), None)
        self.assertEqual(action, "inserted")
        self.assertEqual(list(columns), EXPECTED_COLUMNS)

    def test_existing_row_plans_an_update_with_the_same_columns(self):
        action, columns = appmod.plan_staffing_ratio(self.body(), {"id": 77})
        self.assertEqual(action, "updated")
        self.assertEqual(list(columns), EXPECTED_COLUMNS)

    def test_clock_can_be_pinned_so_a_plan_is_reproducible(self):
        frozen = datetime(2026, 8, 18, 10, 0, 30)
        _, columns = appmod.plan_staffing_ratio(self.body(), None, now=frozen)
        self.assertEqual(columns["insert_date_time"], "2026-08-18 10:00:30")

    def test_same_input_plans_the_same_columns_twice(self):
        frozen = datetime(2026, 8, 18, 10, 0, 30)
        self.assertEqual(appmod.plan_staffing_ratio(self.body(), None, now=frozen),
                         appmod.plan_staffing_ratio(self.body(), None, now=frozen))


if __name__ == "__main__":
    unittest.main(verbosity=2)
