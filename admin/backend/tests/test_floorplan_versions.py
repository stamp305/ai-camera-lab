"""Floorplan picture history kept per branch type, and the repair of hours stored on
the other client's picture.

Run (from ai-camera-admin/backend/):
    venv/bin/python -m pytest -q tests/test_floorplan_versions.py
"""
import json
import os
import sqlite3
import sys
from contextlib import closing

import numpy as np

from data_service.core.services import pipeline_service as ps

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import repair_floorplan_pictures as repair  # noqa: E402

T0 = 1793070000.0   # 27 Oct 2026 10:00 Bangkok


def snapshot(*floorplans, calibrations=()):
    return {"floorplans": [{"floorplan_id": fp, "image_url": f"/images/{name}", "image_updated_at": at}
                           for fp, name, at in floorplans],
            "calibrations": [{"floorplan_id": fp, "image_url": f"/images/{name}"} for fp, name in calibrations]}


def history(root, kind):
    with open(ps._versions_path(str(root), kind), encoding="utf-8") as handle:
        return {int(k): [tuple(v) for v in vs] for k, vs in json.load(handle).items()}


def test_two_clients_of_one_store_keep_their_own_floorplan_1_picture(tmp_path):
    # MONO's client sends its Floorplan 1, then IT's client puts up its own Floorplan 1.
    mono = ps._floorplan_versions(snapshot((1, "mono.png", None)), str(tmp_path), T0, ["mono"])
    it = ps._floorplan_versions(snapshot((1, "it.png", T0 + 780)), str(tmp_path), T0 + 900, ["it"])
    mono = ps._floorplan_versions(snapshot((1, "mono.png", None)), str(tmp_path), T0 + 3600, ["mono"])
    assert mono["mono"] == {1: [(0.0, "mono.png")]}
    assert it["it"] == {1: [(0.0, "it.png")]}
    assert ps._version_at(mono["mono"], 1, T0 + 7200)[1] == "mono.png"


def test_one_client_with_it_and_mono_cameras_gives_both_the_same_history(tmp_path):
    out = ps._floorplan_versions(snapshot((4, "a.png", None)), str(tmp_path), T0, ["it", "mono"])
    out = ps._floorplan_versions(snapshot((4, "b.png", T0 + 60)), str(tmp_path), T0 + 600, ["it", "mono"])
    assert out["it"] == out["mono"] == {4: [(0.0, "a.png"), (T0 + 60, "b.png")]}


def test_the_old_shared_history_is_the_start_and_loses_the_other_clients_pictures(tmp_path):
    # As the shared versions.json was left for 51000025: MONO's picture, then IT's two.
    os.makedirs(tmp_path / "floorplans")
    shared = {"1": [[0.0, "3e71.png"], [T0 + 784, "c5da.png"], [T0 + 2335, "1fd5.png"]]}
    (tmp_path / "floorplans" / "versions.json").write_text(json.dumps(shared))
    mono = ps._floorplan_versions(snapshot((1, "3e71.png", None)), str(tmp_path), T0 + 9000, ["mono"])
    it = ps._floorplan_versions(snapshot((1, "1fd5.png", T0 + 2335)), str(tmp_path), T0 + 9000, ["it"])
    assert mono["mono"] == {1: [(0.0, "3e71.png")]}
    assert it["it"][1] == [(0.0, "3e71.png"), (T0 + 784, "c5da.png"), (T0 + 2335, "1fd5.png")]
    assert history(tmp_path, "mono") == mono["mono"]
    # versions.json itself is left as it was
    assert json.loads((tmp_path / "floorplans" / "versions.json").read_text()) == shared


def test_a_calibration_naming_an_older_picture_does_not_cut_the_history(tmp_path):
    ps._floorplan_versions(snapshot((2, "old.png", None)), str(tmp_path), T0, ["it"])
    ps._floorplan_versions(snapshot((2, "new.png", T0 + 60)), str(tmp_path), T0 + 600, ["it"])
    out = ps._floorplan_versions({"calibrations": [{"floorplan_id": 2, "image_url": "/i/old.png"}]},
                                 str(tmp_path), T0 + 1200, ["it"])
    assert [n for _vf, n in out["it"][2]] == ["old.png", "new.png"]


def _npz(path, value):
    grid = np.full((2, 2), float(value))
    np.savez_compressed(path, grid=grid, unique_grid=grid, sample_grid=grid,
                        bucket_start=T0, bucket_end=T0 + 3600, sample_count=1, unique_count=1)
    return str(path)


def test_the_repair_puts_each_hour_back_on_its_own_clients_picture(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = "51000025"
    root = tmp_path / "processed" / store
    (root / "floorplans").mkdir(parents=True)
    (root / "heatmaps").mkdir()
    for name in ("3e71_aaaaaaaaaaaa.png", "c5da_bbbbbbbbbbbb.png"):
        (root / "floorplans" / name).write_bytes(b"png")
    (root / "floorplans" / "versions_mono.json").write_text(json.dumps({"1": [[0.0, "3e71.png"]]}))
    mine = os.path.abspath(root / "floorplans" / "3e71_aaaaaaaaaaaa.png")
    theirs = os.path.abspath(root / "floorplans" / "c5da_bbbbbbbbbbbb.png")
    db = str(tmp_path / "zone_hour.sqlite3")
    with closing(sqlite3.connect(db)) as conn:
        conn.executescript(ps.FLOORPLAN_HOUR_DDL)
        hour = [  # (valid_from, start_ts, picture, grid value)
            (0.0, T0 + 3600, mine, 1),               # before IT's picture came in
            (T0 + 784 + 3600, T0 + 3600, theirs, 2),  # the same hour, after it
            (T0 + 784 + 3600, T0 + 7200, theirs, 3),  # a later hour, all on IT's picture
        ]
        for i, (vf, ts, pic, value) in enumerate(hour):
            conn.execute(
                "INSERT INTO floorplan_hour (store_id, branch_type, start_ts, floorplan_id, floorplan_valid_from,"
                " floorplan_file_path, heatmap_file_path, created_at) VALUES (?, 'mono', ?, 1, ?, ?, ?, 'x')",
                (store, ts, vf, pic, _npz(root / "heatmaps" / f"h{i}.npz", value)))
        conn.commit()
    monkeypatch.setattr(ps, "PROCESSED_DATA_PATH", "processed")

    assert repair.repair_store(store, db, dry_run=True) == (1, 1, 0)
    assert repair.repair_store(store, db, dry_run=False) == (1, 1, 0)
    with closing(sqlite3.connect(db)) as conn:
        got = conn.execute("SELECT floorplan_valid_from, start_ts, floorplan_file_path, heatmap_file_path"
                           " FROM floorplan_hour ORDER BY start_ts").fetchall()
    assert [(vf, ts, pic) for vf, ts, pic, _h in got] == [(0.0, T0 + 3600, mine), (0.0, T0 + 7200, mine)]
    with np.load(got[0][3]) as z:
        assert z["unique_grid"].tolist() == [[3.0, 3.0], [3.0, 3.0]]   # 1 + 2: the hour's two parts added
        assert int(z["unique_count"]) == 2
    # run again: nothing left to move
    assert repair.repair_store(store, db, dry_run=False) == (0, 0, 0)


def test_the_repair_leaves_an_hour_the_client_itself_changed_picture_in(tmp_path, monkeypatch):
    # As 52000061 MONO: its own eed2 then 6f64 at 15:18 split the 15:00 hour in two rows,
    # both right; IT's cd26 at 16:13 then took its 17:00 hour.
    monkeypatch.chdir(tmp_path)
    store = "52000061"
    root = tmp_path / "processed" / store
    (root / "floorplans").mkdir(parents=True)
    (root / "heatmaps").mkdir()
    pics = {}
    for name in ("eed2", "6f64", "cd26"):
        (root / "floorplans" / f"{name}_aaaaaaaaaaaa.png").write_bytes(b"png")
        pics[name] = os.path.abspath(root / "floorplans" / f"{name}_aaaaaaaaaaaa.png")
    (root / "floorplans" / "versions_mono.json").write_text(
        json.dumps({"1": [[0.0, "eed2.png"], [T0 + 1100, "6f64.png"]]}))
    db = str(tmp_path / "zone_hour.sqlite3")
    with closing(sqlite3.connect(db)) as conn:
        conn.executescript(ps.FLOORPLAN_HOUR_DDL)
        for i, (vf, ts, pic) in enumerate([(0.0, T0, "eed2"), (T0 + 1100, T0, "6f64"),
                                           (T0 + 4400, T0 + 7200, "cd26")]):
            conn.execute(
                "INSERT INTO floorplan_hour (store_id, branch_type, start_ts, floorplan_id, floorplan_valid_from,"
                " floorplan_file_path, heatmap_file_path, created_at) VALUES (?, 'mono', ?, 1, ?, ?, ?, 'x')",
                (store, ts, vf, pics[pic], _npz(root / "heatmaps" / f"h{i}.npz", 1)))
        conn.commit()
    monkeypatch.setattr(ps, "PROCESSED_DATA_PATH", "processed")

    assert repair.repair_store(store, db, dry_run=False) == (1, 0, 0)
    with closing(sqlite3.connect(db)) as conn:
        got = conn.execute("SELECT floorplan_valid_from, start_ts, floorplan_file_path FROM floorplan_hour"
                           " ORDER BY start_ts, floorplan_valid_from").fetchall()
    assert got == [(0.0, T0, pics["eed2"]), (T0 + 1100, T0, pics["6f64"]), (T0 + 1100, T0 + 7200, pics["6f64"])]
