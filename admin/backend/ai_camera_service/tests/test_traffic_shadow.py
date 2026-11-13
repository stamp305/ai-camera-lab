"""Shadow mode for the three old feeds (ADR 0010, .scratch/ai-camera-zone-hour issue 01).

While globalapi2 stays the writer, /traffic/rate, /in-pass and /hotzone only validate: a good
body answers 200 {"action": "shadow"} and one log line, and nothing reaches 209 or POS.

Seams: the HTTP endpoint via TestClient, mysql.connector replaced by tests/fakedb.py. The
bodies are the deploy README's smoke curls — already proven valid by test_deploy_readme.

Run (from ai-camera-admin/backend/ai_camera_service/):
    ../.venv/bin/python -m unittest -v
"""
import os

from tests.fakedb import FakeDBTestCase
from tests.test_deploy_readme import SMOKE_ENDPOINTS, curl_body

import app as appmod


class ShadowTestCase(FakeDBTestCase):
    """The variable unset — the default on a box whose `.env` never mentions it."""

    def setUp(self):
        super().setUp()
        os.environ.pop("TRAFFIC_FEEDS_SHADOW")   # the base's patch.dict restores it

    def assert_shadow_for_every_feed(self):
        for path, _ in SMOKE_ENDPOINTS:
            with self.subTest(path=path):
                self.reset_db()
                response = self.client.post(path, json=curl_body(path))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"action": "shadow"})
                self.assertEqual(self.db.connects, [])   # not 209, not POS
                self.assertEqual(self.db.calls, [])


class TestSoleSender(ShadowTestCase):
    """A body with `rollout_no_globalapi2` is stored; a client still sending to globalapi2 stays shadowed."""

    def test_it_is_stored_on_every_feed(self):
        for path, _ in SMOKE_ENDPOINTS:
            if path == "/traffic/zone-hour":
                continue
            with self.subTest(path=path):
                self.reset_db()
                self.db.rows["51000017.ar_invoice"] = [{
                    "total_sale_bill": 7, "total_bill": 9, "total_pos_bill": 6,
                    "total_web_bill": 2, "total_ordecenter_bill": 1}]
                response = self.client.post(path, json={**curl_body(path), "rollout_no_globalapi2": True})
                self.assertEqual(response.json()["action"], "inserted")
                self.assertTrue(self.db.writes())

    def test_without_it_nothing_is_touched(self):
        path = "/traffic/rate"
        response = self.client.post(path, json={**curl_body(path), "rollout_no_globalapi2": False})
        self.assertEqual(response.json(), {"action": "shadow"})
        self.assertEqual(self.db.calls, [])

    def test_zone_hour_has_no_such_field(self):
        self.assertNotIn("rollout_no_globalapi2", appmod.ZoneHourRequest.model_fields)


class TestShadowIsTheDefault(ShadowTestCase):
    def test_unset_variable_means_shadow(self):
        self.assert_shadow_for_every_feed()

    def test_any_value_but_zero_means_shadow(self):
        for value in ("1", "false", "", " 0", "off"):
            with self.subTest(value=value):
                os.environ["TRAFFIC_FEEDS_SHADOW"] = value
                self.assert_shadow_for_every_feed()

    def test_zero_turns_real_writes_back_on(self):
        os.environ["TRAFFIC_FEEDS_SHADOW"] = "0"
        response = self.client.post("/traffic/rate", json=curl_body("/traffic/rate"))
        self.assertEqual(response.json()["action"], "inserted")
        self.assertEqual(len(self.db.writes()), 1)


class TestShadowStillValidates(ShadowTestCase):
    def test_malformed_body_is_400_with_pydantic_detail(self):
        for path, _ in SMOKE_ENDPOINTS:
            with self.subTest(path=path):
                response = self.client.post(path, json=dict(curl_body(path), branch="123"))
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["detail"][0]["loc"], ["body", "branch"])
                self.assertEqual(self.db.calls, [])


class TestShadowLogLine(ShadowTestCase):
    def test_one_info_line_naming_feed_branch_zone_type_and_timestamp(self):
        for path, _ in SMOKE_ENDPOINTS:
            with self.subTest(path=path):
                body = curl_body(path)
                with self.assertLogs(appmod.log, "INFO") as logs:
                    self.client.post(path, json=body, headers={
                        "Authorization": "Bearer not-a-real-token",
                        "X-Device-Key": "not-a-real-device-key"})
                self.assertEqual(len(logs.records), 1)
                self.assertEqual(logs.records[0].levelname, "INFO")
                line = logs.output[0]
                for part in (f"feed={path.rsplit('/', 1)[1]}", f"branch={body['branch']}",
                             f"zone_type={body['zone_type']}", f"timestamp={body['timestamp']}"):
                    self.assertIn(part, line)
                for secret in ("Authorization", "Bearer", "not-a-real", "Device"):
                    self.assertNotIn(secret, line)


if __name__ == "__main__":
    import unittest
    unittest.main()
