from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import zipfile
from typing import Dict, Optional

from data_service.core.services.pipeline_service import DUMMY_DATA_PATH
from data_service.core.exceptions import ValidationError

logger = logging.getLogger(__name__)

_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.\-]")


def _sanitize_name(value: str, field: str) -> str:
    cleaned = _SAFE_NAME.sub("_", value.strip()).strip("._")
    if not cleaned:
        raise ValidationError(f"{field} must contain at least one safe character", field)
    return cleaned


def _read_manifest_start_ts(zip_path: str) -> Optional[float]:
    """Read ``manifest.json`` inside a zip and return its ``start_ts``."""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            manifest = json.loads(zf.read("manifest.json"))
    except (OSError, zipfile.BadZipFile, json.JSONDecodeError, KeyError) as exc:
        logger.warning("cannot read manifest from %s: %s", zip_path, exc)
        return None
    try:
        return float(manifest["start_ts"])
    except (KeyError, TypeError, ValueError):
        return None


def _snapshot_filename(store_id: str, start_ts: Optional[float], existing: set) -> str:
    """Pick a non-colliding ``pipeline_<start_ts>.zip`` name for a store.

    Snapshots are never overwritten: the name is derived from the manifest
    ``start_ts`` (snapshot start), and when that name already exists a counter
    suffix is appended so each upload stays a distinct file for the queue.
    """
    base = "pipeline_%d" % int(start_ts) if start_ts is not None else "pipeline"
    candidate = f"{base}.zip"
    seq = 1
    while candidate in existing:
        candidate = f"{base}_{seq}.zip"
        seq += 1
    return candidate


def save_pipeline_zip(
    store_id: str,
    original_filename: str,
    file_obj,
) -> Dict[str, str]:
    """Validate and persist one raw pipeline zip under ``dummy_data/<store_id>/``.

    The zip is stored whole (never extracted). It is named ``pipeline_<start_ts>.zip``
    from the embedded manifest so snapshots are self-describing and never
    overwritten — each upload lands as a distinct file that the queue then
    processes. Metadata such as branch type is embedded by the client inside the
    zip; a later preprocess step can separate the data by branch type, by
    floorplan (``floorplan_id`` in the payloads), and by the people inside each
    floorplan.
    """
    if not store_id:
        raise ValidationError("store_id is required", "store_id")
    if not original_filename.lower().endswith(".zip"):
        raise ValidationError("file must be a .zip archive", "file")

    safe_store_id = _sanitize_name(store_id, "store_id")

    directory = os.path.join(DUMMY_DATA_PATH, safe_store_id)
    os.makedirs(directory, exist_ok=True)

    # Write to a temp path first so the filename (start_ts) can be read from the
    # manifest after the bytes land, then move it into place.
    fd, tmp_path = tempfile.mkstemp(suffix=".zip", dir=directory)
    os.close(fd)
    try:
        with open(tmp_path, "wb") as handle:
            shutil.copyfileobj(file_obj, handle)

        start_ts = _read_manifest_start_ts(tmp_path)
        existing = set(os.listdir(directory))
        filename = _snapshot_filename(safe_store_id, start_ts, existing)
        file_path = os.path.join(directory, filename)
        os.replace(tmp_path, file_path)
    except OSError as exc:
        logger.error("Failed to write pipeline zip for store %s: %s", store_id, exc)
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise

    logger.info(
        "Stored pipeline zip for store %s at %s (start_ts=%s)",
        safe_store_id, file_path, start_ts,
    )
    return {
        "message": "Pipeline zip uploaded successfully",
        "filename": filename,
        "path": file_path,
        "start_ts": start_ts,
    }