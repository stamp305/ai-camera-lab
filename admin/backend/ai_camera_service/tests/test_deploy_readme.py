"""The deploy README's smoke checks must stay runnable (issue 05).

Everything in deploy/README.md §2c–2e is copy-pasted by a person onto a prod box, where a
field renamed six months from now shows up as a 400 and a puzzled operator. These tests read
the README itself, so the doc cannot drift from the code without one of them going red.

Seams (the doc is the thing under test here, so both sides of every check are read, never mocked):
  - deploy/README.md and .env.ai_camera_service.example, as text
  - app.py's request models, SQL constants and table names, imported directly

Run (from ai-camera-admin/backend/ai_camera_service/):
    ../.venv/bin/python -m unittest -v
"""
import json
import re
import unittest
from datetime import datetime
from pathlib import Path

import app as appmod

SERVICE_DIR = Path(__file__).resolve().parent.parent
README = (SERVICE_DIR / "deploy" / "README.md").read_text(encoding="utf-8")
ENV_EXAMPLE = (SERVICE_DIR / ".env.ai_camera_service.example").read_text(encoding="utf-8")

# The three smoke curls, each paired with the model that has to accept its body.
SMOKE_ENDPOINTS = [("/traffic/rate", appmod.StaffingRatioRequest),
                   ("/traffic/in-pass", appmod.TrafficRowRequest),
                   ("/traffic/hotzone", appmod.HotzoneRequest)]


def curl_body(path: str) -> dict:
    """The JSON the README tells the operator to POST to `path`."""
    block = README.split("127.0.0.1:8011/ai-camera-service" + path)[1].split("```")[0]
    return json.loads(block.split("-d '", 1)[1].rsplit("'", 1)[0])


def flatten(sql: str) -> str:
    """One line, lower case, every bound or literal window date as `?` — so a retyped query
    can be compared with app.py's without the layout or the sample dates getting in the way."""
    sql = re.sub(r"(%s|'\d{4}-\d\d-\d\d \d\d:\d\d:\d\d')", "?", sql)
    return re.sub(r"\s+", " ", sql).strip().lower()


class SmokeBodiesTestCase(unittest.TestCase):
    def test_each_smoke_curl_carries_exactly_the_fields_its_model_declares(self):
        for path, model in SMOKE_ENDPOINTS:
            with self.subTest(path=path):
                self.assertEqual(set(curl_body(path)), set(model.model_fields) - {"rollout_no_globalapi2"})

    def test_each_smoke_curl_is_accepted_by_its_model(self):
        """A body the model rejects would answer 400 on the box, not write the row."""
        for path, model in SMOKE_ENDPOINTS:
            with self.subTest(path=path):
                model(**curl_body(path))   # ValidationError = the README is wrong


class PosQueriesTestCase(unittest.TestCase):
    """§2d prints the two POS queries for the operator to run by hand and diff the five bill
    columns against. A retyped copy that drifted from app.py would 'prove' a difference that
    is only the doc's."""

    def test_the_hand_run_queries_are_the_ones_app_sends(self):
        args = {"branch": "51000017", "mono": appmod.ZONE_TYPE_FILTER["IT"]}
        for name in ("SALE_BILL_SQL", "BILL_BREAKDOWN_SQL"):
            with self.subTest(sql=name):
                self.assertIn(flatten(getattr(appmod, name).format(**args)), flatten(README))

    def test_the_hand_run_window_is_the_inclusive_hour_the_service_uses(self):
        start, end = (datetime.strptime(t, appmod.DT_FMT)
                      for t in ("2026-09-01 14:00:00", "2026-09-01 15:00:00"))
        self.assertEqual(end - start, appmod.TRAFFIC_ROW_HOUR)
        self.assertIn("abb_date >= '2026-09-01 14:00:00' AND abb_date <= '2026-09-01 15:00:00'",
                      README)


class GrantsTestCase(unittest.TestCase):
    def test_every_table_the_feeds_write_has_a_grant_line(self):
        """§0 step 2 is the DB team's whole ticket — a table missing from it is a 500 on the
        box, found only when a feed first touches that table."""
        for table in (appmod.AVG_TABLE, appmod.ANALYTIC_TABLE, appmod.HOTZONE_TABLE,
                      appmod.MONO_TABLE, appmod.DEMO_TABLE, appmod.TIER_TABLE):
            with self.subTest(table=table):
                self.assertIn(f"GRANT SELECT, INSERT, UPDATE ON api_data.{table} ", README)


class EnvVariablesTestCase(unittest.TestCase):
    """The ticket's third line: the env example and the README must agree — and with app.py."""

    def test_both_documents_name_every_variable_app_reads(self):
        source = (SERVICE_DIR / "app.py").read_text(encoding="utf-8")
        read_by_app = set(re.findall(r"os\.getenv\(\"([A-Z_]+)\"", source))
        self.assertTrue(read_by_app, "no os.getenv calls found — the regex has gone stale")
        for name in read_by_app:
            with self.subTest(variable=name):
                self.assertIn(name, ENV_EXAMPLE)
                self.assertIn(name, README)


if __name__ == "__main__":
    unittest.main()
