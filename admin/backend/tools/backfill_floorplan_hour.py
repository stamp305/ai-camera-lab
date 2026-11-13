"""Fill floorplan_hour / floorplan_hour_zone from what the pipeline preprocess left on disk.

pipeline_service has written floorplan_hour only since that code was deployed. The hours
before it are still on disk, in two forms:

1. Raw zips still in dummy_data/<store>/ (kept PIPELINE_RAW_RETENTION_DAYS): preprocessed
   again, so they give the full payload, zones and time spent included. The preprocess is
   idempotent: it rewrites the same files under processed/.
2. The daily manifests in processed/<store>/heatmaps/*.manifest.json, which name every
   hour's heatmap and floorplan picture: for the hours no zip is left for. They hold no
   time spent and no zones, so those columns stay empty for them.

A row that is already there is never touched by step 2, and step 1 replaces a row only
with a payload of the same hour, as the live preprocess would. Run from the admin
backend's folder (where processed/ and dummy_data/ live), with the backend's environment:

    cd ai-camera-admin/backend
    ZONE_HOUR_DB_PATH=/path/to/zone_hour.sqlite3 venv/bin/python tools/backfill_floorplan_hour.py --dry-run
    ZONE_HOUR_DB_PATH=/path/to/zone_hour.sqlite3 venv/bin/python tools/backfill_floorplan_hour.py

--store 51000001 limits it to one store; --no-zips skips step 1.
"""
import argparse
import glob
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from typing import Iterator, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data_service.core.services import pipeline_service as ps  # noqa: E402

BANGKOK = timezone(timedelta(hours=7))


def stores(only: Optional[str]) -> List[str]:
    found = set()
    for root in (ps.PROCESSED_DATA_PATH, ps.DUMMY_DATA_PATH):
        if os.path.isdir(root):
            found.update(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d)))
    return sorted(s for s in found if only is None or s == only)


def reprocess_zips(store_id: str, dry_run: bool) -> int:
    """Step 1: every zip still kept for the store, preprocessed and stored. Returns payloads."""
    count = 0
    for path in sorted(glob.glob(os.path.join(ps.DUMMY_DATA_PATH, store_id, "*.zip"))):
        try:
            with open(path, "rb") as handle:
                payloads = ps.preprocess_zip_bytes(handle.read(), store_id,
                                                   os.path.join(ps.PROCESSED_DATA_PATH, store_id))
        except Exception as exc:  # noqa: BLE001 — one bad zip must not stop the rest
            print(f"  zip {os.path.basename(path)}: preprocess failed: {exc}")
            continue
        print(f"  zip {os.path.basename(path)}: {len(payloads)} hours")
        if not dry_run:
            ps.store_payloads(payloads)
        count += len(payloads)
    return count


def manifest_hours(store_id: str) -> Iterator[Tuple]:
    """(store, branch_type, floorplan_id, valid_from, start_ts, image, heatmap, manifest) per hour."""
    folder = os.path.join(ps.PROCESSED_DATA_PATH, store_id, "heatmaps")
    for path in sorted(glob.glob(os.path.join(folder, "*.manifest.json"))):
        try:
            with open(path, encoding="utf-8") as handle:
                manifest = json.load(handle)
        except (OSError, ValueError) as exc:
            print(f"  manifest {os.path.basename(path)}: unreadable: {exc}")
            continue
        # schema 2: one file per day, hours inside; schema 1 (old): one file per hour.
        hours = manifest.get("hours") if manifest.get("schema") == 2 else {"": manifest}
        for entry in (hours or {}).values():
            heatmap = entry.get("floorplan_heatmap_path")
            if not heatmap or entry.get("start_ts") is None:
                continue
            yield (str(manifest.get("store_id") or store_id), str(manifest.get("branch_type") or "").lower(),
                   int(manifest.get("floorplan_id") or 0), float(manifest.get("floorplan_valid_from") or 0.0),
                   float(entry["start_ts"]), entry.get("floorplan_image_path"), heatmap, path)


def _abs(path: Optional[str]) -> Optional[str]:
    return os.path.abspath(path) if path else None


def fill_from_manifests(store_id: str, db_path: str, dry_run: bool) -> Tuple[int, int, int]:
    """Step 2: hours with no row yet. Returns (added, already there, heatmap file missing)."""
    added = present = missing = 0
    now = datetime.now(BANGKOK).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(ps.FLOORPLAN_HOUR_DDL)
        with conn:
            for store, kind, fid, valid_from, start_ts, image, heatmap, manifest in manifest_hours(store_id):
                if not os.path.isfile(heatmap):
                    missing += 1
                    continue
                if conn.execute(
                        "SELECT 1 FROM floorplan_hour WHERE store_id = ? AND branch_type = ? AND floorplan_id = ?"
                        " AND floorplan_valid_from = ? AND start_ts = ?",
                        (store, kind, fid, valid_from, start_ts)).fetchone():
                    present += 1
                    continue
                added += 1
                if not dry_run:
                    conn.execute(
                        "INSERT INTO floorplan_hour (store_id, branch_type, floorplan_id, floorplan_valid_from,"
                        " start_ts, floorplan_file_path, heatmap_file_path, heatmap_manifest_path, created_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (store, kind, fid, valid_from, start_ts, _abs(image) if image and os.path.isfile(image) else None,
                         _abs(heatmap), _abs(manifest), now))
    finally:
        conn.close()
    return added, present, missing


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--store", help="only this store id")
    ap.add_argument("--no-zips", action="store_true", help="skip re-preprocessing the kept zips")
    ap.add_argument("--dry-run", action="store_true", help="count, write nothing")
    args = ap.parse_args()

    db_path = os.getenv("ZONE_HOUR_DB_PATH")
    if not db_path:
        print("ZONE_HOUR_DB_PATH is not set: point it at the same file the admin backend uses.")
        return 2
    if not os.path.isdir(ps.PROCESSED_DATA_PATH):
        print(f"No {ps.PROCESSED_DATA_PATH}/ here: run this from the admin backend's folder.")
        return 2
    print(f"{'DRY RUN — ' if args.dry_run else ''}DB {db_path}")
    for store_id in stores(args.store):
        print(f"store {store_id}")
        zipped = 0 if args.no_zips else reprocess_zips(store_id, args.dry_run)
        added, present, missing = fill_from_manifests(store_id, db_path, args.dry_run)
        print(f"  from zips: {zipped} hours · from manifests: {added} added, {present} already there,"
              f" {missing} heatmap files missing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
