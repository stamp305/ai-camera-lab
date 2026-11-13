"""Zone Hour feed tests: POST /traffic/zone-hour replaces one branch + Zone Type's hour of
zones in the local SQLite file (ADR 0010, .scratch/ai-camera-zone-hour issue 02).

Seams: the HTTP endpoint via TestClient, and a REAL SQLite file in a temp directory, read
back with sqlite3 as Alitech will. mysql.connector is still the fake from tests/fakedb.py,
only so a stray 209 call would show up in `self.db.connects`.

Run (from ai-camera-admin/backend/ai_camera_service/):
    ../.venv/bin/python -m unittest -v
"""
import json
import os
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime
from pathlib import Path

from starlette.testclient import TestClient

from tests.fakedb import FakeDBTestCase

import app as appmod

ZONE_FIELDS = ("zone_name", "dwell_sum_seconds", "dwell_count", "wait_sum_seconds",
               "wait_count", "long_wait_count", "unserved_count")


def zone(name="Macbook", **overrides):
    body = {"zone_name": name, "dwell_sum_seconds": 1920, "dwell_count": 6,
            "wait_sum_seconds": 376.97, "wait_count": 4, "long_wait_count": 1,
            "unserved_count": 0}
    body.update(overrides)
    return body


def zone_hour(**overrides):
    """The issue's example body."""
    body = {"branch": "51000017", "region_id": "C", "region_name": "กรุงเทพและปริมณฑล",
            "province_id": 1, "province_name": "กรุงเทพมหานคร",
            "timestamp": "2026-10-15 10:00:00", "zone_type": "IT", "day_of_week": "Thursday",
            "long_wait_seconds": 180, "hour": 10,
            "zones": [zone(), zone("iPhone", dwell_sum_seconds=45.5, unserved_count=2)]}
    body.update(overrides)
    return body


class ZoneHourTestCase(FakeDBTestCase):

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.path = os.path.join(self.tmp, "zone_hour.sqlite3")
        os.environ["ZONE_HOUR_DB_PATH"] = self.path   # the base's patch.dict restores it
        # Shadow mode is on by default and must not touch this feed (ADR 0010).
        os.environ.pop("TRAFFIC_FEEDS_SHADOW")

    def post(self, client=None, **overrides):
        return (client or self.client).post("/traffic/zone-hour", json=zone_hour(**overrides))

    def stored(self, **where):
        """Every row in the file, oldest first, as {column: value}; optionally filtered."""
        with closing(sqlite3.connect(self.path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = [dict(r) for r in conn.execute(
                f"SELECT * FROM {appmod.ZONE_HOUR_TABLE} ORDER BY rowid")]
        return [r for r in rows if all(r[k] == v for k, v in where.items())]


class TestInsertAndReplace(ZoneHourTestCase):

    def test_first_post_inserts_one_row_per_zone_with_every_field_as_sent(self):
        response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"action": "inserted", "zones": 2})

        rows = self.stored()
        self.assertEqual(len(rows), 2)
        body = zone_hour()
        for row, sent in zip(rows, body["zones"]):
            self.assertEqual({f: row[f] for f in ZONE_FIELDS}, sent)
            self.assertEqual(
                {k: row[k] for k in ("region_id", "region_name", "province_id",
                                     "province_name", "zone_type", "day_of_week", "hour",
                                     "long_wait_seconds")},
                {k: body[k] for k in ("region_id", "region_name", "province_id",
                                      "province_name", "zone_type", "day_of_week", "hour",
                                      "long_wait_seconds")})
            self.assertEqual(row["branch_cuscode"], body["branch"])
            self.assertEqual(row["timestamps"], body["timestamp"])
            datetime.strptime(row["insert_date_time"], appmod.DT_FMT)   # server-stamped
        self.assertEqual(self.db.connects, [])   # nothing on 209

    def test_a_resend_replaces_the_hour_and_a_dropped_zone_is_gone(self):
        self.post()
        response = self.post(zones=[zone(dwell_count=9)])
        self.assertEqual(response.json(), {"action": "replaced", "zones": 1})
        rows = self.stored()
        self.assertEqual([(r["zone_name"], r["dwell_count"]) for r in rows], [("Macbook", 9)])

    def test_a_resend_leaves_other_hours_zone_types_and_branches_alone(self):
        self.post()
        self.post(timestamp="2026-10-15 11:00:00", hour=11)
        self.post(zone_type="MONO")
        self.post(branch="51000018")
        self.post(zones=[zone()])
        self.assertEqual(len(self.stored()), 2 + 2 + 2 + 1)
        self.assertEqual(len(self.stored(timestamps="2026-10-15 10:00:00", zone_type="IT",
                                         branch_cuscode="51000017")), 1)

    def test_a_resend_spelling_the_timestamp_loosely_still_replaces_the_hour(self):
        self.post()
        response = self.post(timestamp="2026-10-15 10:0:0")
        self.assertEqual(response.json(), {"action": "replaced", "zones": 2})
        self.assertEqual([r["timestamps"] for r in self.stored()], ["2026-10-15 10:00:00"] * 2)

    def test_the_most_zones_a_body_may_carry_are_all_stored(self):
        zones = [zone(f"zone {i}") for i in range(appmod.MAX_ZONES)]
        self.assertEqual(self.post(zones=zones).json(),
                         {"action": "inserted", "zones": appmod.MAX_ZONES})
        self.assertEqual(len(self.stored()), appmod.MAX_ZONES)

    def test_a_zone_name_is_stored_as_sent_spaces_included(self):
        self.post(zones=[zone(" Macbook ")])
        self.assertEqual(self.stored()[0]["zone_name"], " Macbook ")

    def test_empty_zones_clears_the_hour(self):
        self.post()
        response = self.post(zones=[])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"action": "replaced", "zones": 0})
        self.assertEqual(self.stored(), [])

    def test_empty_zones_on_a_fresh_key_is_an_insert_of_nothing(self):
        self.assertEqual(self.post(zones=[]).json(), {"action": "inserted", "zones": 0})

    def test_hour_and_day_of_week_are_stored_as_sent_even_against_the_timestamp(self):
        self.assertEqual(self.post(hour=3, day_of_week="Funday").status_code, 200)
        row = self.stored()[0]
        self.assertEqual((row["timestamps"], row["hour"], row["day_of_week"]),
                         ("2026-10-15 10:00:00", 3, "Funday"))

    def test_shadow_mode_does_not_apply(self):
        for value in (None, "1"):   # unset (setUp), then explicitly on
            with self.subTest(shadow=value):
                if value is not None:
                    os.environ["TRAFFIC_FEEDS_SHADOW"] = value
                self.assertEqual(self.post().status_code, 200)
                self.assertEqual(len(self.stored()), 2)


class TestClientContractExample(ZoneHourTestCase):
    def test_the_documented_example_body_is_accepted_as_written(self):
        """docs/client-contract.md §4.4 promises the client team this exact body works."""
        doc = (Path(appmod.__file__).parent / "docs" / "client-contract.md").read_text(
            encoding="utf-8")
        body = json.loads(doc.split("```json", 1)[1].split("```", 1)[0])
        response = self.client.post("/traffic/zone-hour", json=body)
        self.assertEqual(response.json(), {"action": "inserted", "zones": 2})


class TestFile(ZoneHourTestCase):

    def test_the_file_is_created_on_first_write_in_wal_mode(self):
        self.assertFalse(os.path.exists(self.path))
        self.post()
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")


class TestRefused(ZoneHourTestCase):
    """400 with pydantic's detail, and nothing ran — not even the file is created."""

    def assert_400(self, response, loc):
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"][0]["loc"], loc)
        self.assertFalse(os.path.exists(self.path))

    def test_a_zone_name_sent_twice(self):
        self.assert_400(self.post(zones=[zone(), zone()]), ["body", "zones"])

    def test_a_negative_count_or_sum(self):
        for field in ("dwell_count", "wait_sum_seconds", "unserved_count"):
            with self.subTest(field=field):
                self.assert_400(self.post(zones=[zone(**{field: -1})]),
                                ["body", "zones", 0, field])

    def test_an_empty_or_blank_zone_name(self):
        for name in ("", "   ", "\t"):
            with self.subTest(name=name):
                self.assert_400(self.post(zones=[zone(name)]), ["body", "zones", 0, "zone_name"])

    def test_more_zones_than_a_body_may_carry(self):
        zones = [zone(f"zone {i}") for i in range(appmod.MAX_ZONES + 1)]
        self.assert_400(self.post(zones=zones), ["body", "zones"])

    def test_a_malformed_header(self):
        self.assert_400(self.post(branch="123"), ["body", "branch"])
        self.assert_400(self.post(timestamp="2026-10-15T10:00"), ["body", "timestamp"])
        self.assert_400(self.post(zone_type="AV"), ["body", "zone_type"])


class TestSqliteFailure(ZoneHourTestCase):

    def setUp(self):
        super().setUp()
        self.raw_client = TestClient(appmod.app, raise_server_exceptions=False)

    def test_a_path_sqlite_cannot_open_answers_500(self):
        os.environ["ZONE_HOUR_DB_PATH"] = self.tmp   # a directory, not a file
        self.assertEqual(self.post(self.raw_client).status_code, 500)

    def test_an_unset_path_answers_500(self):
        for value in (None, ""):
            with self.subTest(path=value):
                os.environ.pop("ZONE_HOUR_DB_PATH", None)
                if value is not None:
                    os.environ["ZONE_HOUR_DB_PATH"] = value
                self.assertEqual(self.post(self.raw_client).status_code, 500)

    def test_a_failed_insert_keeps_the_previous_send(self):
        self.post()
        with closing(sqlite3.connect(self.path)) as conn:   # make every INSERT fail
            conn.execute(f"CREATE TRIGGER refuse BEFORE INSERT ON {appmod.ZONE_HOUR_TABLE}"
                         " BEGIN SELECT RAISE(ABORT, 'refused'); END")
            conn.commit()
        self.assertEqual(self.post(self.raw_client, zones=[zone("iMac")]).status_code, 500)
        self.assertEqual([r["zone_name"] for r in self.stored()], ["Macbook", "iPhone"])


if __name__ == "__main__":
    import unittest
    unittest.main()
