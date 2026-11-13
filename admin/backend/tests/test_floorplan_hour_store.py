"""Preprocess payloads → floorplan_hour / floorplan_hour_zone in the Zone Hour SQLite file.

Run (from ai-camera-admin/backend/):
    venv/bin/python -m pytest -q tests/test_floorplan_hour_store.py
"""
import os
import sqlite3
from contextlib import closing

from data_service.core.services import pipeline_service as ps


def payload(**overrides):
    body = {
        "store_id": "51000001", "branch_type": "it", "start_ts": 1792130400.0,
        "floorplan_id": 2, "floorplan_valid_from": None,
        "avg_spent_seconds": 65.0, "median_spent_seconds": 14.6,
        "floorplan_file_path": "processed/51000001/floorplans/plan_96dde1de25d7.png",
        "heatmap_file_path": "processed/51000001/heatmaps/floorplan/20261016_1300_it_2.npz",
        "heatmap_manifest_path": "processed/51000001/heatmaps/20261016_it_2.manifest.json",
        "zones": [
            {"id": "zone-1", "name": "Notebook", "type": "hotzone", "subtype": None,
             "avg_spent_seconds": 65.1, "visit_count": 360},
            {"id": "rate-1", "name": "", "type": "rate", "subtype": "employee",
             "avg_spent_seconds": 51.9, "visit_count": 9},
        ],
    }
    body.update(overrides)
    return body


def rows(db, sql):
    with closing(sqlite3.connect(db)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql)]


def test_a_payload_is_one_hour_row_and_one_row_per_zone(tmp_path):
    db = str(tmp_path / "zone_hour.sqlite3")
    assert ps.store_payloads([payload()], db) == 1
    [hour] = rows(db, "SELECT * FROM floorplan_hour")
    assert (hour["store_id"], hour["branch_type"], hour["floorplan_id"], hour["floorplan_valid_from"],
            hour["start_ts"], hour["avg_spent_seconds"]) == ("51000001", "it", 2, 0.0, 1792130400.0, 65.0)
    assert os.path.isabs(hour["heatmap_file_path"])
    assert hour["heatmap_file_path"].endswith("processed/51000001/heatmaps/floorplan/20261016_1300_it_2.npz")
    zones = rows(db, "SELECT * FROM floorplan_hour_zone ORDER BY zone_id")
    assert [(z["zone_id"], z["zone_type"], z["visit_count"], z["floorplan_hour_id"]) for z in zones] == [
        ("rate-1", "rate", 9, hour["id"]), ("zone-1", "hotzone", 360, hour["id"])]


def test_a_later_snapshot_of_the_hour_replaces_it_and_its_zones(tmp_path):
    db = str(tmp_path / "zone_hour.sqlite3")
    ps.store_payloads([payload()], db)
    ps.store_payloads([payload(avg_spent_seconds=70.0, zones=[
        {"id": "zone-1", "name": "Notebook", "type": "hotzone", "subtype": None,
         "avg_spent_seconds": 70.0, "visit_count": 400}])], db)
    [hour] = rows(db, "SELECT * FROM floorplan_hour")
    assert hour["avg_spent_seconds"] == 70.0
    assert [(z["zone_id"], z["visit_count"]) for z in rows(db, "SELECT * FROM floorplan_hour_zone")] == [
        ("zone-1", 400)]


def test_each_picture_version_and_branch_type_is_its_own_row(tmp_path):
    db = str(tmp_path / "zone_hour.sqlite3")
    ps.store_payloads([payload(), payload(floorplan_valid_from=1792132200.0),
                       payload(branch_type="mono", floorplan_id=3)], db)
    assert len(rows(db, "SELECT id FROM floorplan_hour")) == 3


def test_no_db_path_stores_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("ZONE_HOUR_DB_PATH", raising=False)
    assert ps.store_payloads([payload()]) == 0


def test_a_floorplan_the_client_stops_listing_is_marked_deleted_at_that_list(tmp_path):
    db = str(tmp_path / "zone_hour.sqlite3")
    kept = "SELECT branch_type, floorplan_id, deleted_at FROM floorplan_client ORDER BY branch_type, floorplan_id"
    ps.store_payloads([payload(client_floorplans={"ids": [2, 5], "at": 100.0}),
                       payload(branch_type="mono", client_floorplans={"ids": [2, 5], "at": 100.0})], db)
    assert [tuple(r.values()) for r in rows(db, kept)] == [
        ("it", 2, None), ("it", 5, None), ("mono", 2, None), ("mono", 5, None)]
    # Floorplan 5 deleted on the IT client; then a list that could not be read changes nothing.
    ps.store_payloads([payload(client_floorplans={"ids": [2], "at": 200.0})], db)
    ps.store_payloads([payload(client_floorplans={"ids": [], "at": 300.0})], db)
    # A later list keeps the first deletion time.
    ps.store_payloads([payload(client_floorplans={"ids": [2], "at": 400.0})], db)
    assert [tuple(r.values()) for r in rows(db, kept)] == [
        ("it", 2, None), ("it", 5, 200.0), ("mono", 2, None), ("mono", 5, None)]


def test_a_late_older_list_does_not_bring_a_deleted_floorplan_back(tmp_path):
    db = str(tmp_path / "zone_hour.sqlite3")
    ps.store_payloads([payload(client_floorplans={"ids": [2], "at": 200.0})], db)
    ps.store_payloads([payload(client_floorplans={"ids": [2, 5], "at": 100.0})], db)
    assert rows(db, "SELECT floorplan_id FROM floorplan_client") == [{"floorplan_id": 2}]


def test_a_floorplan_with_hours_but_never_listed_is_deleted_at_the_first_list(tmp_path):
    db = str(tmp_path / "zone_hour.sqlite3")
    ps.store_payloads([payload(floorplan_id=7)], db)   # a client from before the list
    ps.store_payloads([payload(client_floorplans={"ids": [2], "at": 500.0})], db)
    assert [tuple(r.values()) for r in rows(
        db, "SELECT floorplan_id, deleted_at FROM floorplan_client ORDER BY floorplan_id")] == [(2, None), (7, 500.0)]


def test_the_forward_body_does_not_carry_the_floorplan_list(monkeypatch):
    sent = []

    class Response:
        status_code = 200
        text = ""

    monkeypatch.setattr(ps.requests, "post", lambda url, json, **_: sent.append(json) or Response())
    assert ps.forward_payloads("51000001", [payload(client_floorplans={"ids": [2], "at": 1.0})], url="http://receiver") == 1
    assert "client_floorplans" not in sent[0] and sent[0]["floorplan_id"] == 2
