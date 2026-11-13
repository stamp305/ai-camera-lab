"""Hotzone Round feed tests: POST /traffic/hotzone writes four linked tables under one
commit (issue 03).

Seams are issue 01's, unchanged: the HTTP endpoint via TestClient, the recording fake in
tests/fakedb.py, and the planner called with no database at all.

The branch rules under test come from the PHP the feed replaces
(docs/PushNotificationController.php, api_traffic_analytic, the `hotzone` block) with one
correction: the `_mono` row is found by mono_id, not by an `id` column the table has not
got. That miss is why a re-sent round fails half-written today.

Run (from ai-camera-admin/backend/ai_camera_service/):
    ../.venv/bin/python -m unittest -v
"""
import unittest
from datetime import datetime
from typing import Optional

from starlette.testclient import TestClient

from tests.fakedb import FakeDBTestCase, written_columns

import app as appmod

HOTZONE, MONO = "shoping_traffic_hotzone", "shoping_traffic_hotzone_mono"
DEMO, TIER = "emp_demo", "demo_tier"

# The columns each table gets, in the order the PHP merges them. The link columns
# (hotzone_id, demo_id) are appended by the insert paths and asserted separately.
HOTZONE_COLUMNS = ["branch_cuscode", "region_id", "region_name", "province_id",
                   "province_name", "timestamps", "insert_date_time",
                   "day_of_week", "hour", "count_in", "count_apple", "count_nb",
                   "nb_to_apple", "apple_to_nb", "count_active", "count_not_active",
                   "zone_type", "count_learnhub", "engagement_wait"]
MONO_COLUMNS = [f"{n}_zone{i}" for i in range(1, 6) for n in ("name", "count")]
DEMO_COLUMNS = ([f"{n}_zone{i}_mono" for i in range(1, 6) for n in ("name", "count")]
                + ["name_zone_ce", "count_zone_ce"])
TIER_COLUMNS = ["count_zone4_tier1", "count_zone4_tier2", "count_zone4_tier3"]

NO_ZONES = {f"count_zone{i}": 0 for i in range(1, 6)}          # nobody in any product zone
NO_DEMOS = {**{f"count_zone{i}_mono": 0 for i in range(1, 6)}, "count_zone_ce": 0}


class HotzoneTestCase(FakeDBTestCase):
    """A valid Hotzone Round; no existing row unless a test cans one."""

    def post(self, **overrides):
        payload = {
            "branch": "10000001",
            "region_id": "N",
            "region_name": "ภาคเหนือ",
            "province_id": 50,
            "province_name": "เชียงใหม่",
            "timestamp": "2026-08-18 09:50:00",
            "zone_type": "MONO",
            "day_of_week": "Tuesday",
            "hour": "09",
            "count_in": 40, "count_apple": 12, "count_nb": 8,
            "nb_to_apple": 3, "apple_to_nb": 2,
            "count_active": 5, "count_not_active": 1,
            "count_learnhub": 4, "engagement_wait": 12.5,
            "name_zone1": "iphone", "count_zone1": 6,
            "name_zone2": "ipad", "count_zone2": 4,
            "name_zone3": "watch", "count_zone3": 0,
            "name_zone4": "macbook", "count_zone4": 9,
            "name_zone5": "accessory", "count_zone5": 1,
            "name_zone1_mono": "iphone", "count_zone1_mono": 2,
            "name_zone2_mono": "ipad", "count_zone2_mono": 1,
            "name_zone3_mono": "watch", "count_zone3_mono": 0,
            "name_zone4_mono": "macbook", "count_zone4_mono": 3,
            "name_zone5_mono": "accessory", "count_zone5_mono": 0,
            "name_zone_ce": "ce", "count_zone_ce": 5,
            "count_zone4_tier1": 1, "count_zone4_tier2": 1, "count_zone4_tier3": 1,
        }
        payload.update(overrides)
        return self.client.post("/traffic/hotzone", json=payload)

    def existing_round(self, row_id=77, mono_id: Optional[int] = 0):
        self.db.rows[HOTZONE] = [{"id": row_id, "mono_id": mono_id}]

    def only_write_to(self, table):
        """The single write aimed at `table`, or fails the test."""
        writes = self.db.writes_to(table)
        self.assertEqual(len(writes), 1, f"{table}: {writes}")
        return writes[0]

    def assert_untouched(self, table):
        self.assertEqual(self.db.writes_to(table), [])


class TestInsert(HotzoneTestCase):
    """No row for the key: hotzone → _mono (+ link) → emp_demo → demo_tier."""

    def test_answers_with_the_new_hotzone_row_s_id(self):
        resp = self.post()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"id": 101, "action": "inserted"})   # fake's first id

    def test_all_four_tables_are_written(self):
        self.post()
        for table in (HOTZONE, MONO, DEMO, TIER):
            self.assertTrue(self.db.writes_to(table), table)

    def test_hotzone_insert_carries_every_column(self):
        self.post()
        sql, params = self.db.writes_to(HOTZONE)[0]
        self.assertTrue(sql.startswith(f"INSERT INTO {HOTZONE} "))
        self.assertEqual(list(written_columns(sql, params)), HOTZONE_COLUMNS)

    def test_hotzone_insert_values_are_the_body_verbatim(self):
        self.post()
        columns = written_columns(*self.db.writes_to(HOTZONE)[0])
        datetime.strptime(columns.pop("insert_date_time"), appmod.DT_FMT)  # server clock
        self.assertEqual(columns, {
            "branch_cuscode": "10000001", "region_id": "N", "region_name": "ภาคเหนือ",
            "province_id": 50, "province_name": "เชียงใหม่",
            "timestamps": "2026-08-18 09:50:00", "day_of_week": "Tuesday", "hour": "09",
            "count_in": 40, "count_apple": 12, "count_nb": 8, "nb_to_apple": 3,
            "apple_to_nb": 2, "count_active": 5, "count_not_active": 1,
            "zone_type": "MONO", "count_learnhub": 4, "engagement_wait": 12.5})

    def test_mono_row_is_inserted_and_linked_back_to_the_hotzone_row(self):
        self.post()
        sql, params = self.db.writes_to(MONO)[0]
        self.assertTrue(sql.startswith(f"INSERT INTO {MONO} "))
        self.assertEqual(written_columns(sql, params),
                         {"name_zone1": "iphone", "count_zone1": 6,
                          "name_zone2": "ipad", "count_zone2": 4,
                          "name_zone3": "watch", "count_zone3": 0,
                          "name_zone4": "macbook", "count_zone4": 9,
                          "name_zone5": "accessory", "count_zone5": 1})
        link = self.db.writes_to(HOTZONE)[1]
        self.assertEqual(link, (f"UPDATE {HOTZONE} SET mono_id = %s WHERE id = %s",
                                (102, 101)))       # the _mono row's id onto the hotzone row

    def test_emp_demo_insert_carries_the_demo_columns_and_the_link(self):
        self.post()
        sql, params = self.only_write_to(DEMO)
        self.assertTrue(sql.startswith(f"INSERT INTO {DEMO} "))
        columns = written_columns(sql, params)
        self.assertEqual(list(columns), DEMO_COLUMNS + ["hotzone_id"])
        self.assertEqual(columns["count_zone4_mono"], 3)
        self.assertEqual(columns["count_zone_ce"], 5)
        self.assertEqual(columns["hotzone_id"], 101)

    def test_demo_tier_insert_links_both_the_round_and_the_emp_demo_row(self):
        self.post()
        sql, params = self.only_write_to(TIER)
        self.assertTrue(sql.startswith(f"INSERT INTO {TIER} "))
        self.assertEqual(written_columns(sql, params),
                         {"count_zone4_tier1": 1, "count_zone4_tier2": 1,
                          "count_zone4_tier3": 1, "hotzone_id": 101, "demo_id": 103})

    def test_the_whole_round_is_one_connection_and_one_commit(self):
        self.post()
        self.assertEqual(self.db.commits, 1)
        self.assertEqual(len(self.db.connects), 1)
        self.assertEqual(self.db.rollbacks, 0)

    def test_the_lookup_runs_before_any_write(self):
        self.post()
        sql, params = self.db.calls[0]
        self.assertIn(f"FROM {HOTZONE}", sql)
        self.assertEqual(params, ("10000001", "2026-08-18 09:50:00", "MONO"))


class TestInsertBranches(HotzoneTestCase):
    """Which child rows exist at all, on the insert path."""

    def test_no_zone_count_above_zero_writes_no_mono_row_and_no_link(self):
        self.post(**NO_ZONES)
        self.assert_untouched(MONO)
        self.assertEqual(len(self.db.writes_to(HOTZONE)), 1)   # the insert, no link update

    def test_a_single_zone_count_above_zero_is_enough(self):
        for zone in range(1, 6):
            with self.subTest(zone=zone):
                self.reset_db()
                self.post(**{**NO_ZONES, f"count_zone{zone}": 1})
                self.assertTrue(self.db.writes_to(MONO))

    def test_emp_demo_is_written_even_when_every_demo_count_is_zero(self):
        # CONTEXT.md § Demo Round: every round gets the row, zero demos included.
        self.post(**NO_DEMOS)
        self.assertTrue(self.db.writes_to(DEMO))

    def test_no_mac_demos_skips_demo_tier(self):
        self.post(count_zone4_mono=0)
        self.assert_untouched(TIER)

    def test_a_round_with_nothing_in_it_still_writes_hotzone_and_emp_demo(self):
        self.post(**{**NO_ZONES, **NO_DEMOS})
        self.assertEqual(len(self.db.writes_to(HOTZONE)), 1)
        self.assertTrue(self.db.writes_to(DEMO))
        self.assert_untouched(MONO)
        self.assert_untouched(TIER)
        self.assertEqual(self.db.commits, 1)


class TestUpdate(HotzoneTestCase):
    """A re-send of the same (branch, timestamps, Zone Type) updates in place."""

    def test_hotzone_is_updated_by_its_id_and_answered_with_it(self):
        self.existing_round(77)
        resp = self.post()
        self.assertEqual(resp.json(), {"id": 77, "action": "updated"})
        sql, params = self.db.writes_to(HOTZONE)[0]
        self.assertTrue(sql.startswith(f"UPDATE {HOTZONE} SET "))
        columns = written_columns(sql, params)
        self.assertEqual(columns.pop("id"), 77)
        self.assertEqual(list(columns), HOTZONE_COLUMNS)

    def test_emp_demo_is_updated_by_hotzone_id_and_never_inserted(self):
        # The PHP updates silently when the row is absent, and so do we: only pre-2024
        # globalapi rounds lack one and nobody re-sends those (spec §Hotzone fan-out).
        self.existing_round(77)
        self.post()
        sql, params = self.only_write_to(DEMO)
        self.assertTrue(sql.startswith(f"UPDATE {DEMO} SET "))
        columns = written_columns(sql, params)
        self.assertEqual(columns.pop("hotzone_id"), 77)
        self.assertEqual(list(columns), DEMO_COLUMNS)

    def test_a_linked_mono_row_is_updated_by_mono_id_not_by_id(self):
        # The bug this feed fixes: the PHP filters this UPDATE on an `id` column the
        # table does not have, so a re-send of a linked round dies half-written.
        self.existing_round(77, mono_id=55)
        self.post()
        sql, params = self.only_write_to(MONO)
        self.assertTrue(sql.startswith(f"UPDATE {MONO} SET "))
        self.assertTrue(sql.endswith("WHERE mono_id = %s"))
        self.assertEqual(params[-1], 55)
        self.assertNotIn("WHERE id = %s", sql)

    def test_an_unlinked_round_inserts_the_mono_row_and_links_it(self):
        self.existing_round(77, mono_id=0)
        self.post()
        sql, _ = self.only_write_to(MONO)
        self.assertTrue(sql.startswith(f"INSERT INTO {MONO} "))
        self.assertEqual(self.db.writes_to(HOTZONE)[1],
                         (f"UPDATE {HOTZONE} SET mono_id = %s WHERE id = %s", (101, 77)))

    def test_a_null_mono_id_counts_as_unlinked(self):
        self.existing_round(77, mono_id=None)
        self.post()
        self.assertTrue(self.only_write_to(MONO)[0].startswith(f"INSERT INTO {MONO} "))

    def test_no_zone_count_above_zero_leaves_the_mono_row_untouched(self):
        self.existing_round(77, mono_id=55)
        self.post(**NO_ZONES)
        self.assert_untouched(MONO)
        self.assertEqual(len(self.db.writes_to(HOTZONE)), 1)

    def test_the_whole_re_send_is_one_connection_and_one_commit(self):
        self.existing_round(77, mono_id=55)
        self.post()
        self.assertEqual(self.db.commits, 1)
        self.assertEqual(len(self.db.connects), 1)

    def test_several_existing_rows_update_the_first(self):
        self.db.rows[HOTZONE] = [{"id": 77, "mono_id": 0}, {"id": 78, "mono_id": 0}]
        self.assertEqual(self.post().json(), {"id": 77, "action": "updated"})


class TestUpdateDemoTier(HotzoneTestCase):
    """demo_tier on the update path: update it, insert it, or leave it alone."""

    def setUp(self):
        super().setUp()
        self.existing_round(77, mono_id=55)

    def test_an_existing_tier_row_is_updated_by_its_id(self):
        self.db.rows[TIER] = [{"id": 9}]
        self.post()
        sql, params = self.only_write_to(TIER)
        self.assertTrue(sql.startswith(f"UPDATE {TIER} SET "))
        columns = written_columns(sql, params)
        self.assertEqual(columns.pop("id"), 9)
        self.assertEqual(list(columns), TIER_COLUMNS)

    def test_a_missing_tier_row_is_inserted_with_the_demo_id_from_emp_demo(self):
        self.db.rows[DEMO] = [{"demo_id": 42}]
        self.post()
        sql, params = self.only_write_to(TIER)
        self.assertTrue(sql.startswith(f"INSERT INTO {TIER} "))
        self.assertEqual(written_columns(sql, params),
                         {"count_zone4_tier1": 1, "count_zone4_tier2": 1,
                          "count_zone4_tier3": 1, "hotzone_id": 77, "demo_id": 42})

    def test_no_emp_demo_row_means_no_tier_row_to_link_so_none_is_written(self):
        self.post()                       # neither emp_demo nor demo_tier canned
        self.assert_untouched(TIER)

    def test_no_mac_demos_leaves_the_tier_row_untouched(self):
        self.db.rows[TIER] = [{"id": 9}]
        self.post(count_zone4_mono=0)
        self.assert_untouched(TIER)


class TestTransaction(HotzoneTestCase):
    """One commit for the round, or none at all — a crash never half-writes it
    (spec User Story 9)."""

    def setUp(self):
        super().setUp()
        self.client = TestClient(appmod.app, raise_server_exceptions=False)

    def test_a_failure_part_way_rolls_back_and_commits_nothing(self):
        self.db.fail_on = f"INSERT INTO {DEMO}"       # the third write of the insert path
        resp = self.post()
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(self.db.commits, 0)
        self.assertEqual(self.db.rollbacks, 1)

    def test_a_failure_on_the_last_write_still_rolls_the_whole_round_back(self):
        self.db.fail_on = f"INSERT INTO {TIER}"
        self.assertEqual(self.post().status_code, 500)
        self.assertEqual((self.db.commits, self.db.rollbacks), (0, 1))

    def test_a_failed_update_rolls_back(self):
        self.existing_round(77, mono_id=55)
        self.db.fail_on = f"UPDATE {MONO}"
        self.assertEqual(self.post().status_code, 500)
        self.assertEqual((self.db.commits, self.db.rollbacks), (0, 1))

    def test_a_failed_lookup_writes_nothing(self):
        self.db.fail_on = "SELECT"
        self.assertEqual(self.post().status_code, 500)
        self.assert_zero_writes()


class TestValidation(HotzoneTestCase):
    """The header's three checks, shared with /traffic/rate — here only to prove the
    hotzone body runs them before it opens a connection."""

    def test_a_malformed_body_is_400_and_never_reaches_the_database(self):
        for bad in ({"branch": "123"}, {"timestamp": "18-08-2026 09:50"},
                    {"zone_type": "GADGET"}, {"count_zone4_mono": "many"}):
            with self.subTest(bad=bad):
                self.reset_db()
                self.assertEqual(self.post(**bad).status_code, 400)
                self.assertEqual(self.db.calls, [])
                self.assertEqual(self.db.connects, [])

    def test_a_missing_zone_field_is_rejected(self):
        payload = {"branch": "10000001"}
        self.assertEqual(self.client.post("/traffic/hotzone", json=payload).status_code, 400)


class TestPlannerIsPure(unittest.TestCase):
    """The planner with no database, no fake and no HTTP — the seam issue 04's parity
    verifier drives against rows globalapi wrote."""

    def request(self, **overrides):
        payload = {
            "branch": "10000001", "region_id": "N", "region_name": "ภาคเหนือ",
            "province_id": 50, "province_name": "เชียงใหม่",
            "timestamp": "2026-08-18 09:50:00", "zone_type": "MONO",
            "day_of_week": "Tuesday", "hour": "09",
            "count_in": 40, "count_apple": 12, "count_nb": 8,
            "nb_to_apple": 3, "apple_to_nb": 2, "count_active": 5, "count_not_active": 1,
            "count_learnhub": 4, "engagement_wait": 12.5,
            **{f"name_zone{i}": f"z{i}" for i in range(1, 6)},
            **{f"count_zone{i}": i for i in range(1, 6)},
            **{f"name_zone{i}_mono": f"z{i}" for i in range(1, 6)},
            **{f"count_zone{i}_mono": i for i in range(1, 6)},
            "name_zone_ce": "ce", "count_zone_ce": 5,
            "count_zone4_tier1": 1, "count_zone4_tier2": 1, "count_zone4_tier3": 1,
        }
        payload.update(overrides)
        return appmod.HotzoneRequest(**payload)

    def test_no_existing_row_plans_the_four_inserts_in_order(self):
        action, writes = appmod.plan_hotzone(self.request(), None)
        self.assertEqual(action, "inserted")
        self.assertEqual([(w.table, w.where is None) for w in writes],
                         [(HOTZONE, True), (MONO, True), (HOTZONE, False),
                          (DEMO, True), (TIER, True)])

    def test_an_existing_row_plans_updates_keyed_the_way_each_table_is_reached(self):
        action, writes = appmod.plan_hotzone(
            self.request(), {"id": 77, "mono_id": 55}, {"demo_id": 42, "tier_id": 9})
        self.assertEqual(action, "updated")
        self.assertEqual([(w.table, w.where) for w in writes],
                         [(HOTZONE, ("id", 77)), (DEMO, ("hotzone_id", 77)),
                          (MONO, ("mono_id", 55)), (TIER, ("id", 9))])

    def test_the_clock_can_be_pinned_so_a_plan_is_reproducible(self):
        frozen = datetime(2026, 8, 18, 10, 0, 30)
        first = appmod.plan_hotzone(self.request(), None, now=frozen)
        self.assertEqual(first[1][0].columns["insert_date_time"], "2026-08-18 10:00:30")
        self.assertEqual(first, appmod.plan_hotzone(self.request(), None, now=frozen))

    def test_the_planner_opens_no_connection(self):
        # No fake installed in this TestCase: a real mysql.connector import would fail here.
        appmod.plan_hotzone(self.request(), None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
