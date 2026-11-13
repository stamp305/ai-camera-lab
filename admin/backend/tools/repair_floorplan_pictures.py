"""Put floorplan_hour rows back on their own client's picture.

Before the picture history was kept per branch type, a store whose IT and MONO are two
clients shared one history (processed/<store>/floorplans/versions.json): when one client
put up a picture for its Floorplan 1, the other client's Floorplan 1 hours from then on
were stored on that picture. The raw zips are long gone, but each hour's grid is right:
only its picture version is wrong. A row whose version is not in its branch type's own
history (versions_<branch_type>.json, written by the preprocess from the next snapshot
of that client on) is re-pointed to the version that history gives from the row's start
on; a row already on one of its own versions is left alone. A row that lands on an hour that
already has a row of the right version (the hour the wrong picture came in) is added
into it: the grids are summed into that row's .npz, and the row itself is deleted.

A branch type with no history of its own yet is left as it is: wait for one upload of
its client after the deploy. Run from the admin backend's folder:

    cd ai-camera-admin/backend
    ZONE_HOUR_DB_PATH=/path/to/zone_hour.sqlite3 venv/bin/python tools/repair_floorplan_pictures.py --store 51000025 --dry-run
    ZONE_HOUR_DB_PATH=/path/to/zone_hour.sqlite3 venv/bin/python tools/repair_floorplan_pictures.py --store 51000025
"""
import argparse
import os
import sqlite3
import sys
from typing import Dict, List, Optional, Tuple

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data_service.core.services import pipeline_service as ps  # noqa: E402

_GRIDS = ("grid", "unique_grid", "sample_grid")
_COUNTS = ("sample_count", "unique_count")


def _add_npz(into: str, extra: str) -> bool:
    """Add the grids and counts of ``extra`` into ``into``; False when either cannot be read."""
    try:
        with np.load(into, allow_pickle=False) as a, np.load(extra, allow_pickle=False) as b:
            merged = {k: a[k] for k in a.files}
            for k in _GRIDS + _COUNTS:
                if k in a.files and k in b.files:
                    if k in _GRIDS and a[k].shape != b[k].shape:
                        return False
                    merged[k] = a[k] + b[k]
            for k in ("bucket_start",):
                if k in a.files and k in b.files:
                    merged[k] = np.minimum(a[k], b[k])
            for k in ("bucket_end",):
                if k in a.files and k in b.files:
                    merged[k] = np.maximum(a[k], b[k])
    except (OSError, KeyError, ValueError) as exc:
        print(f"    cannot add {extra} into {into}: {exc}")
        return False
    tmp = into + ".tmp.npz"
    np.savez_compressed(tmp, **merged)
    os.replace(tmp, into)
    return True


def _picture_path(processed_root: str, name: Optional[str]) -> Optional[str]:
    path = ps._floorplan_image({}, name, processed_root)
    return os.path.abspath(path) if path else None


def _is_own(entries: List[Tuple[float, str]], valid_from: float, picture: Optional[str]) -> bool:
    """Whether a row's version is one of its branch type's own: the stored picture
    ``<stem>_<sha12>.png`` is the client's ``<stem>.png`` that started at its valid_from."""
    if not picture:
        return False
    stem = os.path.basename(picture).rsplit("_", 1)[0]
    return any(vf == valid_from and os.path.splitext(name)[0] == stem for vf, name in entries)


def repair_store(store_id: str, db_path: str, dry_run: bool) -> Tuple[int, int, int]:
    """Returns (moved, added into another row, left: no history or picture file of its own)."""
    processed_root = os.path.join(ps.PROCESSED_DATA_PATH, store_id)
    histories: Dict[str, Optional[dict]] = {}
    moved = merged = left = 0
    conn = sqlite3.connect(db_path)
    try:
        rows: List[tuple] = conn.execute(
            "SELECT id, branch_type, floorplan_id, floorplan_valid_from, start_ts, floorplan_file_path,"
            " heatmap_file_path FROM floorplan_hour WHERE store_id = ? AND floorplan_id != 0 ORDER BY start_ts",
            (store_id,)).fetchall()
        with conn:
            for row_id, kind, fid, valid_from, start_ts, picture, heatmap in rows:
                if kind not in histories:
                    histories[kind] = ps._read_versions(ps._versions_path(processed_root, kind))
                history = histories[kind]
                if history is None or not history.get(fid):
                    left += 1
                    continue
                if _is_own(history[fid], valid_from, picture):
                    continue
                # The row covers its hour from the foreign picture's start: the client's own
                # picture at that moment, not at the hour's start (an hour the client itself
                # changed picture in is split into rows that are all right).
                want_from, want_name, _to = ps._version_at(history, fid, max(start_ts, valid_from))
                want_picture = _picture_path(processed_root, want_name)
                if want_picture is None:   # its file is not on disk: nothing to point the row at
                    left += 1
                    continue
                other = conn.execute(
                    "SELECT id, heatmap_file_path FROM floorplan_hour WHERE store_id = ? AND branch_type = ?"
                    " AND floorplan_id = ? AND floorplan_valid_from = ? AND start_ts = ? AND id != ?",
                    (store_id, kind, fid, want_from, start_ts, row_id)).fetchone()
                print(f"  {kind} floorplan {fid} hour {start_ts:.0f}: {os.path.basename(picture or '-')}"
                      f" -> {os.path.basename(want_picture or '-')}{' (added into its hour)' if other else ''}")
                if other is None:
                    moved += 1
                    if not dry_run:
                        conn.execute("UPDATE floorplan_hour SET floorplan_valid_from = ?, floorplan_file_path = ?"
                                     " WHERE id = ?", (want_from, want_picture, row_id))
                    continue
                merged += 1
                if not dry_run and _add_npz(other[1], heatmap):
                    conn.execute("DELETE FROM floorplan_hour_zone WHERE floorplan_hour_id = ?", (row_id,))
                    conn.execute("DELETE FROM floorplan_hour WHERE id = ?", (row_id,))
    finally:
        conn.close()
    return moved, merged, left


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--store", required=True, help="the store id")
    ap.add_argument("--dry-run", action="store_true", help="list what would change, write nothing")
    args = ap.parse_args()

    db_path = os.getenv("ZONE_HOUR_DB_PATH")
    if not db_path:
        print("ZONE_HOUR_DB_PATH is not set: point it at the same file the admin backend uses.")
        return 2
    if not os.path.isdir(ps.PROCESSED_DATA_PATH):
        print(f"No {ps.PROCESSED_DATA_PATH}/ here: run this from the admin backend's folder.")
        return 2
    print(f"{'DRY RUN — ' if args.dry_run else ''}DB {db_path}, store {args.store}")
    moved, merged, left = repair_store(args.store, db_path, args.dry_run)
    print(f"  {moved} hours moved to their own picture, {merged} added into an hour already there,"
          f" {left} left (no history of their own yet)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
