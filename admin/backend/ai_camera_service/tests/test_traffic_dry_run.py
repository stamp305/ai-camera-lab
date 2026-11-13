"""The X-Traffic-Dry-Run header on the three feeds that write to 209: every read, the plan,
no write.

Run (from ai-camera-admin/backend/ai_camera_service/):
    ../.venv/bin/python -m unittest -v
"""
import os
import unittest
from unittest import mock

from tests.fakedb import FakeDBTestCase
from tests.test_traffic_hotzone import HotzoneTestCase

import app as appmod

HEADER = {"branch": "10000001", "region_id": "N", "region_name": "ภาคเหนือ", "province_id": 50,
          "province_name": "เชียงใหม่", "timestamp": "2026-08-18 09:00:00", "zone_type": "MONO"}
DRY = {"X-Traffic-Dry-Run": "1"}
POS_ROW = {"total_sale_bill": 7, "total_bill": 9, "total_pos_bill": 6, "total_web_bill": 2,
           "total_ordecenter_bill": 1}


class DryRunTestCase(FakeDBTestCase):
    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {"POS_DB_HOST": "10.10.10.70", "POS_DB_USER": "u",
                                           "POS_DB_PASS": "p"})
        env.start()
        self.addCleanup(env.stop)
        self.db.rows["10000001.ar_invoice"] = [dict(POS_ROW)]

    def in_pass(self, headers=DRY, **overrides):
        return self.client.post("/traffic/in-pass", headers=headers, json={
            **HEADER, "day_of_week": "Tuesday", "hour": 9, "walk_pass": 120, "walk_in": 30,
            "rate": 0.25, **overrides})

    def rate(self):
        return self.client.post("/traffic/rate", headers=DRY, json={
            **HEADER, "hour": "09", "avg_emp": 2.5, "avg_cus": 5.0, "rate": 0.5})


class TestInPass(DryRunTestCase):
    def test_counts_the_bills_and_answers_the_columns_without_writing(self):
        body = self.in_pass().json()
        self.assertEqual((body["action"], body["would"], body["id"]), ("dry_run", "inserted", None))
        self.assertEqual(body["columns"]["count_bill"], 9)
        self.assertEqual(body["columns"]["count_oc"], 1)
        self.assert_zero_writes()

    def test_names_the_row_an_update_would_hit(self):
        self.db.rows[appmod.ANALYTIC_TABLE] = [{"id": 77}]
        body = self.in_pass().json()
        self.assertEqual((body["would"], body["id"]), ("updated", 77))
        self.assert_zero_writes()

    def test_runs_under_shadow_mode_too(self):
        with mock.patch.dict(os.environ, {"TRAFFIC_FEEDS_SHADOW": "1"}):
            body = self.in_pass().json()
        self.assertEqual(body["action"], "dry_run")
        self.assertEqual(body["columns"]["count_sale"], 7)
        self.assert_zero_writes()

    def test_a_pos_failure_still_answers_500(self):
        self.client = appmod_client_without_raise()
        self.db.fail_on = "total_sale_bill"
        self.assertEqual(self.in_pass().status_code, 500)

    def test_a_bad_body_still_answers_400_before_any_sql(self):
        self.assertEqual(self.in_pass(branch="`x`").status_code, 400)
        self.assertEqual(self.db.calls, [])

    def test_without_the_header_shadow_touches_nothing(self):
        with mock.patch.dict(os.environ, {"TRAFFIC_FEEDS_SHADOW": "1"}):
            body = self.in_pass(headers={}).json()
        self.assertEqual(body, {"action": "shadow"})
        self.assertEqual(self.db.calls, [])

    def test_a_dry_run_field_in_the_body_is_ignored(self):
        # The body is the one thing a Branch controls through the frontend.
        with mock.patch.dict(os.environ, {"TRAFFIC_FEEDS_SHADOW": "1"}):
            body = self.in_pass(headers={}, dry_run=True).json()
        self.assertEqual(body, {"action": "shadow"})
        self.assertEqual(self.db.calls, [])


class TestRate(DryRunTestCase):
    def test_answers_the_columns_without_writing(self):
        self.db.rows[appmod.AVG_TABLE] = [{"id": 5}]
        body = self.rate().json()
        self.assertEqual((body["action"], body["would"], body["id"]), ("dry_run", "updated", 5))
        self.assertEqual(body["columns"]["avg_emp"], 2.5)
        self.assert_zero_writes()


class TestHotzone(HotzoneTestCase):
    def test_answers_the_writes_without_making_them(self):
        self.client.headers.update(DRY)
        resp = self.post()
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual((body["action"], body["would"]), ("dry_run", "inserted"))
        self.assertEqual(body["writes"][0]["table"], appmod.HOTZONE_TABLE)
        self.assertEqual(self.db.writes(), [])


def appmod_client_without_raise():
    from starlette.testclient import TestClient
    return TestClient(appmod.app, raise_server_exceptions=False)


if __name__ == "__main__":
    unittest.main()
