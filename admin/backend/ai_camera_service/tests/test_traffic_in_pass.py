"""Traffic Row feed tests: POST /traffic/in-pass upserts one row in
shoping_traffic_analytic, with the five bill columns counted on the branch's POS schema
(issue 02, built on the rails issue 01 laid).

Seams (pre-agreed in .scratch/ai-camera-traffic-feed/spec.md, Testing Decisions):
  - the HTTP endpoint via starlette.testclient.TestClient
  - mysql.connector replaced with the recording fake in tests/fakedb.py — the POS host
    goes through the same fake; its connection is told apart by host, its statements by
    the schema-qualified `<branch>`.ar_invoice they read
  - the planner called directly, with no database at all — the seam the parity
    verifier (issue 04) drives

Run (from ai-camera-admin/backend/ai_camera_service/):
    ../.venv/bin/python -m unittest -v
"""
import unittest
from datetime import datetime
from unittest import mock

from starlette.testclient import TestClient

from tests.fakedb import FakeDBTestCase, written_columns

import app as appmod

ANALYTIC_TABLE = "shoping_traffic_analytic"
POS_HOST = "10.10.10.70"

# The columns the PHP writes for this feed, in the order it merges them
# (PushNotificationController.php lines 727–733 then 793–801).
EXPECTED_COLUMNS = ["branch_cuscode", "region_id", "region_name", "province_id",
                    "province_name", "timestamps", "insert_date_time",
                    "day_of_week", "hour", "walk_pass", "walk_in", "rate",
                    "count_bill", "count_sale", "zone_type",
                    "count_pos", "count_web", "count_oc"]

# One canned POS row, served to BOTH bill queries — they read the same table, so the fake
# hands them the same rows, and each query picks out only its own aliases. Every number is
# distinct, so a column filled from the wrong alias shows up as the wrong number.
POS_ANSWER = {"total_sale_bill": 7,        # → count_sale   (query A)
              "total_bill": 9,             # → count_bill   (query B)
              "total_pos_bill": 6,         # → count_pos
              "total_web_bill": 2,         # → count_web
              "total_ordecenter_bill": 1}  # → count_oc


class TrafficRowTestCase(FakeDBTestCase):
    """A valid Traffic Row body, a POS host that answers, and no existing row on 209
    unless a test cans one."""

    def setUp(self):
        super().setUp()
        self.env = mock.patch.dict("os.environ", {
            "POS_DB_HOST": POS_HOST, "POS_DB_USER": "pos_reader",
            "POS_DB_PASS": "pos_secret"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.db.rows["10000001.ar_invoice"] = [dict(POS_ANSWER)]

    def post(self, **overrides):
        payload = {
            "branch": "10000001",
            "region_id": "N",
            "region_name": "ภาคเหนือ",
            "province_id": 50,
            "province_name": "เชียงใหม่",
            "timestamp": "2026-08-18 09:00:00",
            "zone_type": "MONO",
            "day_of_week": "Tuesday",
            "hour": 9,
            "walk_pass": 120,
            "walk_in": 30,
            "rate": 0.25,
        }
        payload.update(overrides)
        return self.client.post("/traffic/in-pass", json=payload)

    def existing_row(self, row_id):
        self.db.rows[ANALYTIC_TABLE] = [{"id": row_id}]

    def the_write(self):
        """The single write statement, or fails the test."""
        writes = self.db.writes()
        self.assertEqual(len(writes), 1)
        return writes[0]

    def pos_calls(self):
        """The statements that went to the POS host, in the order they ran.

        `connects` is parallel to `calls` — run_query opens one connection per statement —
        so the host of a statement is the host of the connection beside it."""
        return [call for call, conn in zip(self.db.calls, self.db.connects)
                if conn.get("host") == POS_HOST]

    def sale_call(self):
        """Query A — the one that counts count_sale."""
        return next(c for c in self.pos_calls() if "total_sale_bill" in c[0])

    def breakdown_call(self):
        """Query B — the grouped join behind count_bill / pos / web / oc."""
        return next(c for c in self.pos_calls() if "total_ordecenter_bill" in c[0])


class TestInsert(TrafficRowTestCase):
    def test_no_existing_row_inserts_one_and_answers_with_its_id(self):
        resp = self.post()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"id": 101, "action": "inserted"})   # fake's first id

    def test_insert_carries_every_column_in_the_php_s_order(self):
        self.post()
        sql, params = self.the_write()
        self.assertTrue(sql.startswith(f"INSERT INTO {ANALYTIC_TABLE} "))
        self.assertEqual(list(written_columns(sql, params)), EXPECTED_COLUMNS)

    def test_insert_values_are_the_body_verbatim_plus_the_counted_bills(self):
        self.post()
        columns = written_columns(*self.the_write())
        insert_date_time = columns.pop("insert_date_time")     # server clock, checked below
        self.assertEqual(columns, {
            "branch_cuscode": "10000001", "region_id": "N", "region_name": "ภาคเหนือ",
            "province_id": 50, "province_name": "เชียงใหม่",
            "timestamps": "2026-08-18 09:00:00",
            "day_of_week": "Tuesday", "hour": 9, "walk_pass": 120, "walk_in": 30,
            "rate": 0.25, "zone_type": "MONO",
            "count_bill": 9, "count_sale": 7, "count_pos": 6, "count_web": 2,
            "count_oc": 1})
        datetime.strptime(insert_date_time, appmod.DT_FMT)      # server-filled, parseable

    def test_the_body_may_not_carry_bill_columns_of_its_own(self):
        # They are the service's to compute (spec User Story 7); a body that sent them
        # would be ignored, so the model refuses to take them at all.
        self.assertNotIn("count_bill", appmod.TrafficRowRequest.model_fields)

    def test_insert_is_committed(self):
        self.post()
        self.assertEqual(self.db.commits, 1)


class TestBillQueries(TrafficRowTestCase):
    """The two POS statements: what they read, over what window, filtered how."""

    def test_two_statements_go_to_the_pos_host_and_the_rest_to_209(self):
        self.post()
        self.assertEqual(len(self.pos_calls()), 2)
        self.assertEqual(len(self.db.calls), 4)      # 2 POS + the lookup + the write

    def test_the_pos_connection_uses_the_branch_db_credentials(self):
        self.post()
        pos_conn = next(c for c in self.db.connects if c.get("host") == POS_HOST)
        self.assertEqual(pos_conn, {"host": POS_HOST, "user": "pos_reader",
                                    "password": "pos_secret", "port": 3306,
                                    "connection_timeout": appmod.DB_CONNECT_TIMEOUT})

    def test_both_queries_read_the_branch_s_own_schema(self):
        self.post()
        self.assertIn("`10000001`.ar_invoice", self.sale_call()[0])
        for table in ("ar_invoice", "ar_refer", "cart"):
            self.assertIn(f"`10000001`.{table}", self.breakdown_call()[0])

    def test_the_window_is_the_timestamp_and_one_hour_later(self):
        self.post()
        window = ("2026-08-18 09:00:00", "2026-08-18 10:00:00")
        self.assertEqual(self.sale_call()[1], window)
        self.assertEqual(self.breakdown_call()[1], window)

    def test_the_window_is_inclusive_at_both_ends_as_the_php_has_it(self):
        self.post()
        for sql, _ in self.pos_calls():
            self.assertIn("abb_date >= %s", sql)
            self.assertIn("abb_date <= %s", sql)

    def test_mono_body_filters_mono_apple_equal_to_t(self):
        self.post(zone_type="MONO")
        for sql, _ in self.pos_calls():
            self.assertIn("and mono_apple = 'T'", sql)

    def test_it_body_filters_mono_apple_not_t_which_drops_nulls(self):
        self.post(zone_type="IT")
        for sql, _ in self.pos_calls():
            self.assertIn("and mono_apple != 'T'", sql)
            self.assertNotIn("mono_apple = 'T'", sql)

    def test_count_sale_is_the_selling_condition_codes_only(self):
        self.post()
        self.assertIn("condition_ IN ('00', '11', '12', '13', '14')", self.sale_call()[0])

    def test_the_breakdown_groups_one_row_per_invoice(self):
        self.post()
        sql = self.breakdown_call()[0]
        self.assertIn("GROUP BY ar_invoice.Invoice_ID", sql)
        self.assertIn("LEFT JOIN", sql)


class TestUpsertKey(TrafficRowTestCase):
    def test_lookup_is_branch_timestamps_and_zone_type_on_the_analytic_table(self):
        self.post()
        sql, params = next(c for c in self.db.calls if ANALYTIC_TABLE in c[0])
        self.assertIn(f"FROM {ANALYTIC_TABLE}", sql)
        self.assertEqual(params, ("10000001", "2026-08-18 09:00:00", "MONO"))

    def test_existing_row_is_updated_by_its_id_and_answered_with_it(self):
        self.existing_row(77)
        resp = self.post()
        self.assertEqual(resp.json(), {"id": 77, "action": "updated"})
        sql, params = self.the_write()
        self.assertTrue(sql.startswith(f"UPDATE {ANALYTIC_TABLE} SET "))
        self.assertTrue(sql.endswith("WHERE id = %s"))
        self.assertEqual(written_columns(sql, params)["id"], 77)

    def test_update_refreshes_every_column_including_the_bill_counts(self):
        # The PHP recomputes the bills on every send, so a re-send picks up late bills.
        self.existing_row(77)
        self.post()
        columns = written_columns(*self.the_write())
        del columns["id"]
        self.assertEqual(list(columns), EXPECTED_COLUMNS)
        self.assertEqual(columns["count_bill"], 9)

    def test_several_existing_rows_update_the_first(self):
        self.db.rows[ANALYTIC_TABLE] = [{"id": 77}, {"id": 78}]
        self.assertEqual(self.post().json(), {"id": 77, "action": "updated"})


class TestValidation(TrafficRowTestCase):
    def test_branch_that_is_not_eight_digits_never_reaches_the_pos_host(self):
        # branch is interpolated as a schema name, so the check must come first — a body
        # that got through would run its own SQL on whatever schema it named.
        for bad in ("1000001", "100000012", "1000000a", "", "`other`", "'; DROP"):
            self.assertEqual(self.post(branch=bad).status_code, 400, bad)
        self.assertEqual(self.db.calls, [])

    def test_bad_timestamp_or_zone_type_is_rejected_before_any_sql(self):
        self.assertEqual(self.post(timestamp="2026-08-18T09:00:00").status_code, 400)
        self.assertEqual(self.post(zone_type="GADGET").status_code, 400)
        self.assertEqual(self.db.calls, [])

    def test_missing_field_is_rejected(self):
        self.assertEqual(self.client.post("/traffic/in-pass", json={}).status_code, 400)

    def test_both_zone_types_are_accepted(self):
        for zone in ("IT", "MONO"):
            self.assertEqual(self.post(zone_type=zone).status_code, 200, zone)

    def test_hour_is_the_int_the_client_sends_and_a_string_form_still_passes(self):
        # job_send_logs sends int(hour) - 1, an int; a string form must not be refused.
        self.post()
        self.assertEqual(written_columns(*self.the_write())["hour"], 9)
        self.assertEqual(self.post(hour="9").status_code, 200)


class TestPosFailure(TrafficRowTestCase):
    """User Story 10: an hour whose bills cannot be counted is refused outright. A zero
    written now reads later as a quiet hour and nobody can tell it apart."""

    def setUp(self):
        super().setUp()
        self.client = TestClient(appmod.app, raise_server_exceptions=False)

    def test_pos_query_failure_answers_500_and_writes_nothing_on_209(self):
        self.db.fail_on = "total_sale_bill"
        self.assertEqual(self.post().status_code, 500)
        self.assert_zero_writes()
        self.assertEqual([c for c in self.db.calls if ANALYTIC_TABLE in c[0]], [])

    def test_breakdown_failure_answers_500_and_writes_nothing_on_209(self):
        self.db.fail_on = "total_ordecenter_bill"
        self.assertEqual(self.post().status_code, 500)
        self.assert_zero_writes()
        self.assertEqual([c for c in self.db.calls if ANALYTIC_TABLE in c[0]], [])

    def test_a_pos_host_that_answers_no_row_is_a_500_not_a_zero_bill_row(self):
        self.db.rows.pop("10000001.ar_invoice")
        self.assertEqual(self.post().status_code, 500)
        self.assert_zero_writes()

    def test_a_209_failure_is_still_a_500(self):
        self.db.fail_on = "INSERT"
        self.assertEqual(self.post().status_code, 500)


class TestPlannerIsPure(unittest.TestCase):
    """No database, no fake, no HTTP — the seam issue 04's parity verifier drives."""

    COUNTS = {"count_bill": 9, "count_sale": 7, "count_pos": 6, "count_web": 2,
              "count_oc": 1}

    def body(self, **overrides):
        fields = {
            "branch": "10000001", "region_id": "N", "region_name": "ภาคเหนือ",
            "province_id": 50, "province_name": "เชียงใหม่",
            "timestamp": "2026-08-18 09:00:00", "zone_type": "IT",
            "day_of_week": "Tuesday", "hour": 9, "walk_pass": 120, "walk_in": 30,
            "rate": 0.25,
        }
        fields.update(overrides)
        return appmod.TrafficRowRequest(**fields)

    def test_no_existing_row_plans_an_insert(self):
        action, columns = appmod.plan_traffic_row(self.body(), None, self.COUNTS)
        self.assertEqual(action, "inserted")
        self.assertEqual(list(columns), EXPECTED_COLUMNS)

    def test_existing_row_plans_an_update_with_the_same_columns(self):
        action, columns = appmod.plan_traffic_row(self.body(), {"id": 77}, self.COUNTS)
        self.assertEqual(action, "updated")
        self.assertEqual(list(columns), EXPECTED_COLUMNS)

    def test_the_counts_land_in_their_columns(self):
        _, columns = appmod.plan_traffic_row(self.body(), None, self.COUNTS)
        self.assertEqual({k: columns[k] for k in self.COUNTS}, self.COUNTS)

    def test_same_input_plans_the_same_columns_twice(self):
        frozen = datetime(2026, 8, 18, 10, 0, 30)
        self.assertEqual(
            appmod.plan_traffic_row(self.body(), None, self.COUNTS, now=frozen),
            appmod.plan_traffic_row(self.body(), None, self.COUNTS, now=frozen))


if __name__ == "__main__":
    unittest.main(verbosity=2)
