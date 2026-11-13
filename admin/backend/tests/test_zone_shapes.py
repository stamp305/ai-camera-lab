"""Hotzone polygons carried from the camera frame onto the floorplan, and stored with the hour.

Run (from ai-camera-admin/backend/):
    venv/bin/python -m pytest -q tests/test_zone_shapes.py
"""
import json
import sqlite3
from contextlib import closing

from data_service.core.services import pipeline_service as ps

# A real camera of the Mac mini (MONO, 192.168.2.240 ch 12): its matrix and calibrated floorplan points.
MATRIX = [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0], [0.0, 0.0, 1.0]]
DST = [[71, 19], [71, 35], [62, 36], [56, 46], [56, 36], [51, 36], [51, 18], [62, 18], [62, 3]]


def identity_scaled(sx=1.0, sy=1.0, tx=0.0, ty=0.0):
    """H that maps the camera frame percent (0-100) to floorplan percent: x*sx+tx, y*sy+ty."""
    return [[sx, 0.0, tx], [0.0, sy, ty], [0.0, 0.0, 1.0]]


def calibration(matrix=None):
    return {"calibrations": [{"camera_ip": "192.168.2.240", "camera_channel": 12, "floorplan_id": 4,
                              "matrix": matrix or identity_scaled(0.3, 0.4, 51, 18), "dst_points": DST}]}


def test_a_polygon_inside_the_calibrated_area_is_carried_to_floorplan_percent():
    model = ps._load_zone_projection(calibration())["192.168.2.240_12"]
    # camera frame (0.5, 0.5) -> (50, 50) -> x 50*0.3+51 = 66, y 50*0.4+18 = 38
    shape = ps._project_zone_points([(0.5, 0.5), (0.6, 0.5), (0.6, 0.6)], model)
    assert shape == [[66.0, 38.0], [69.0, 38.0], [69.0, 42.0]]


def test_a_polygon_far_outside_the_calibrated_area_is_dropped_not_drawn_wrong():
    model = ps._load_zone_projection(calibration(identity_scaled(1.0, 1.0, 0.0, -150.0)))["192.168.2.240_12"]
    assert ps._project_zone_points([(0.5, 0.5), (0.6, 0.5), (0.6, 0.6)], model) is None


def test_a_camera_without_a_usable_matrix_places_nothing():
    assert ps._load_zone_projection({"calibrations": [{"camera_ip": "1.1.1.1", "camera_channel": 1,
                                                       "matrix": None, "dst_points": DST}]}) == {}
    assert ps._project_zone_points([(0.1, 0.1), (0.2, 0.1), (0.2, 0.2)], None) is None


def test_dst_points_may_arrive_as_a_json_string():
    cal = calibration()
    cal["calibrations"][0]["dst_points"] = json.dumps(DST)
    assert "192.168.2.240_12" in ps._load_zone_projection(cal)


def test_zone_shape_is_stored_with_the_hour_and_survives_a_file_made_before_it(tmp_path):
    db = str(tmp_path / "zone_hour.sqlite3")
    # a file as the first release made it: no zone_color / zone_points
    with closing(sqlite3.connect(db)) as conn:
        conn.executescript(ps.FLOORPLAN_HOUR_DDL.replace(
            "    zone_color          TEXT,               -- zones[].color (hotzone drawn on the floorplan)\n", "").replace(
            "    zone_points         TEXT,               -- zones[].points as JSON [[x, y], ...], floorplan percent 0-100\n", ""))
    body = {"store_id": "51000001", "branch_type": "it", "start_ts": 1792130400.0, "floorplan_id": 2,
            "floorplan_valid_from": None, "avg_spent_seconds": 1.0, "median_spent_seconds": 1.0,
            "floorplan_file_path": None, "heatmap_file_path": "h.npz", "heatmap_manifest_path": None,
            "zones": [{"id": "z1", "name": "iPad", "type": "hotzone", "subtype": None,
                       "avg_spent_seconds": 5.0, "visit_count": 3, "color": "#f57c00",
                       "points": [[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]]},
                      {"id": "z2", "name": "Far", "type": "hotzone", "subtype": None,
                       "avg_spent_seconds": None, "visit_count": 0, "color": None, "points": None}]}
    assert ps.store_payloads([body], db) == 1
    with closing(sqlite3.connect(db)) as conn:
        got = conn.execute("SELECT zone_id, zone_color, zone_points FROM floorplan_hour_zone ORDER BY zone_id").fetchall()
    assert got == [("z1", "#f57c00", "[[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]]"), ("z2", None, None)]
