# data_service/core/services/pipeline_service.py
"""Pipeline snapshot preprocess + forward + background queue, in one module.

The admin receives whole-branch pipeline zips under ``dummy_data/<store_id>/``.
This module owns the whole downstream pipeline:

  * preprocess: unzip a snapshot and compute per (branch_type, floorplan_id,
    date, hour) analytics — per-zone spent time, avg/median spent seconds,
    person count, a content-addressed floorplan image, and a rendered overall
    heatmap. It also renders one CCTV-frame heatmap per (camera, date, hour)
    from raw camera-space foot points (uncalibrated cameras included), and
    writes one manifest JSON per day listing every heatmap path.
    Processed artifacts live under ``processed/<store_id>/``.
  * forward: POST each payload as JSON to the globalAPI preprocess route.
    Images stay in the admin's processed store; payloads carry the store
    ``processed/...`` image paths. No image bytes are uploaded.
  * queue: a background loop (started by the backend) that scans ``dummy_data/``
    every ``PIPELINE_QUEUE_INTERVAL_SECONDS``, processes + forwards zips not
    yet handled (tracked in ``processed/<store_id>/queue_state.json`` by
    ``mtime:size``), then deletes raw zips older than
    ``PIPELINE_RAW_RETENTION_DAYS``.

Config is hardcoded below — no environment variables are read.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import sqlite3
import threading
import time
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np
import requests

logger = logging.getLogger(__name__)
_BANGKOK = timezone(timedelta(hours=7))

# -------------------------------------------------------------------------------------
# Config (hardcoded — not read from the environment)
# -------------------------------------------------------------------------------------
DUMMY_DATA_PATH = "dummy_data"

# Processed artifacts (floorplans/heatmaps/queue_state) live outside the raw
# store dirs: ``processed/<store_id>/...``. Raw zips get pruned by retention;
# processed outputs are kept because forwarded payloads reference them.
PROCESSED_DATA_PATH = "processed"

# globalAPI preprocess route. Empty string disables forwarding (preprocess and
# artifact storage still run).
PIPELINE_FORWARD_URL = ""

# Preprocess the latest snapshot right after a zip upload.
PIPELINE_PREPROCESS_ON_UPLOAD = True

# Background queue loop.
PIPELINE_QUEUE_ENABLED = True
PIPELINE_QUEUE_INTERVAL_SECONDS = 60

# Raw snapshot retention: pipeline zips older than this many days are deleted
# after being processed. Processed artifacts (floorplans/heatmaps) are kept.
PIPELINE_RAW_RETENTION_DAYS = 1

GRID_W = 192
GRID_H = 108
_MIN_DWELL_SECONDS = 10.0
_PNG_EXT = ".png"
_NPZ_EXT = ".npz"

Point = Tuple[float, float]


class PreprocessError(RuntimeError):
    """Raised when a pipeline snapshot cannot be processed."""


# -------------------------------------------------------------------------------------
# geometry — the same point-in-polygon rule the client uses (zone_geometry.py)
# -------------------------------------------------------------------------------------
def _point_in_polygon(point: Point, polygon: List[Point]) -> bool:
    px, py = point
    inside = False
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        if (y1 > py) != (y2 > py):
            x_cross = (x2 - x1) * (py - y1) / (y2 - y1) + x1
            if px < x_cross:
                inside = not inside
    return inside


def _cam_to_camera_key(cam: str) -> str:
    """``cam_192_168_2_240_1`` -> ``192.168.2.240_1`` (manifest cameras key)."""
    parts = str(cam).split("_")
    if len(parts) < 6 or parts[0] != "cam":
        return str(cam)
    try:
        return f"{'.'.join(parts[1:5])}_{int(parts[5])}"
    except ValueError:
        return str(cam)


# -------------------------------------------------------------------------------------
# snapshot reading
# -------------------------------------------------------------------------------------
_REQUIRED = object()


def _read_json(zf: zipfile.ZipFile, name: str, default=_REQUIRED):
    """A JSON member of the zip; `default` when given and the member is absent."""
    try:
        with zf.open(name) as handle:
            return json.loads(handle.read().decode("utf-8"))
    except KeyError:
        if default is not _REQUIRED:
            return default
        raise PreprocessError(f"zip missing {name}")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PreprocessError(f"zip {name} is not valid JSON: {exc}")


def _iter_text(zf: zipfile.ZipFile, prefix: str):
    for name in zf.namelist():
        if not name.startswith(prefix) or not name.endswith(".jsonl"):
            continue
        with zf.open(name) as handle:
            for raw in handle:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rec["_name"] = name
                yield rec


def _hour_stamp_from_name(name: str) -> Optional[Tuple[str, int]]:
    """``observations_20260918_150000.jsonl`` -> (``2026-09-18``, 15)."""
    base = os.path.basename(name)
    if not base.startswith("observations_") or not base.endswith(".jsonl"):
        return None
    parts = base[:-6].split("_")
    if len(parts) < 3:
        return None
    try:
        day = datetime.strptime(parts[-2], "%Y%m%d").strftime("%Y-%m-%d")
        hour = int(parts[-1][:2])
    except ValueError:
        return None
    return day, hour


def _load_zones(zone_json: dict) -> List[dict]:
    """Normalise zone.json into (camera_key, kind, subtype, id, name, points)."""
    zones: List[dict] = []
    for entry in zone_json.get("zones", []):
        camera_key = f"{entry.get('camera_ip')}_{int(entry.get('camera_channel', 0))}"
        for kind in ("hotzone", "rate", "attendance", "in_pass"):
            for z in entry.get(kind) or []:
                points = [(float(p[0]), float(p[1])) for p in z.get("points", [])]
                if len(points) < 3:
                    continue
                zones.append({
                    "camera_key": camera_key,
                    "kind": kind,
                    "subtype": z.get("type") or z.get("direction") or None,
                    "id": str(z.get("id") or ""),
                    "name": str(z.get("name") or ""),
                    "points": points,
                    "duration": z.get("duration"),
                    "color": z.get("color"),
                })
    return zones


def _load_calibration(calibration_json: dict) -> Dict[str, int]:
    """camera_key -> floorplan_id (from calibration.json calibrations)."""
    out: Dict[str, int] = {}
    for c in calibration_json.get("calibrations", []):
        key = f"{c.get('camera_ip')}_{int(c.get('camera_channel', 0))}"
        out[key] = int(c.get("floorplan_id", 0))
    return out


# A hotzone polygon is drawn on the camera frame (0-1). Alitech draws it on the floorplan picture, so
# it is carried through the camera's calibration matrix into floorplan percent (0-100). The matrix
# only holds the homography fitted on the points the installer placed, and it runs away from
# them: a zone outside that area lands off the plan or across the store. Such a zone is dropped,
# not drawn wrong. "Outside" = further than this many percent of the plan from the hull of the
# camera's calibrated floorplan points.
_ZONE_SHAPE_MARGIN = 6.0


def _as_point_list(value) -> Optional[list]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return None
    return value if isinstance(value, list) else None


def _convex_hull(points: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """Counter-clockwise hull (monotone chain); fewer than 3 distinct points give []."""
    pts = sorted(set(points))
    if len(pts) < 3:
        return []

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: List[Tuple[float, float]] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: List[Tuple[float, float]] = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    hull = lower[:-1] + upper[:-1]
    return hull if len(hull) >= 3 else []


def _near_hull(point: Tuple[float, float], hull: List[Tuple[float, float]], margin: float) -> bool:
    """True when the point is inside the counter-clockwise hull or within margin of every edge's inside."""
    for i, a in enumerate(hull):
        b = hull[(i + 1) % len(hull)]
        length = ((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5
        if length == 0:
            continue
        side = ((b[0] - a[0]) * (point[1] - a[1]) - (b[1] - a[1]) * (point[0] - a[0])) / length
        if side < -margin:
            return False
    return True


def _load_zone_projection(calibration_json: dict) -> Dict[str, dict]:
    """camera_key -> {"H": 3x3 matrix, "hull": hull of the calibrated floorplan points}."""
    out: Dict[str, dict] = {}
    for c in calibration_json.get("calibrations", []):
        try:
            key = f"{c.get('camera_ip')}_{int(c.get('camera_channel', 0))}"
            matrix = np.asarray(c.get("matrix"), dtype=float)
            dst = _as_point_list(c.get("dst_points"))
            hull = _convex_hull([(float(p[0]), float(p[1])) for p in dst or []])
        except (TypeError, ValueError, IndexError):
            continue
        if matrix.shape == (3, 3) and np.all(np.isfinite(matrix)) and hull:
            out[key] = {"H": matrix, "hull": hull}
    return out


def _project_zone_points(points: List[Tuple[float, float]], model: Optional[dict]) -> Optional[List[List[float]]]:
    """A camera-frame polygon (0-1) as floorplan percent (0-100), or None when it cannot be placed."""
    if not model or len(points) < 3:
        return None
    out: List[List[float]] = []
    for x, y in points:
        vec = model["H"] @ np.array([x * 100.0, y * 100.0, 1.0])
        if not np.isfinite(vec).all() or vec[2] <= 1e-9:
            return None
        px, py = float(vec[0] / vec[2]), float(vec[1] / vec[2])
        if not _near_hull((px, py), model["hull"], _ZONE_SHAPE_MARGIN):
            return None
        out.append([round(min(max(px, 0.0), 100.0), 2), round(min(max(py, 0.0), 100.0), 2)])
    return out


def _coerce_group(value) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _load_floorplan_groups(calibration_json: dict) -> Dict[str, Optional[int]]:
    """camera_key -> explicit floorplan group (None = not configured).

    Accepts ``group`` / ``group_id`` / ``floorplan_group``. Callers fall back
    to per-floorplan isolation (group = floorplan_id) so old snapshots
    without the DB ``group`` field keep the strict behavior.
    """
    out: Dict[str, Optional[int]] = {}
    for c in (calibration_json or {}).get("calibrations", []):
        try:
            key = f"{c.get('camera_ip')}_{int(c.get('camera_channel', 0))}"
        except (TypeError, ValueError):
            continue
        group = None
        for alias in ("group", "group_id", "floorplan_group"):
            try:
                if c.get(alias) is not None:
                    group = c.get(alias)
                    break
            except AttributeError:
                continue
        out[key] = _coerce_group(group)
    return out


def _versions_path(processed_root: str, branch_type: Optional[str] = None) -> str:
    """``floorplans/versions_<branch_type>.json``; no branch type: the one shared
    ``versions.json`` written before the history was kept per branch type."""
    name = "versions.json" if branch_type is None else f"versions_{_safe_part(branch_type)}.json"
    return os.path.join(processed_root, "floorplans", name)


def _safe_part(text: str) -> str:
    return "".join(ch if ch.isalnum() or ch in " _-" else "_" for ch in str(text))


def _read_versions(path: str) -> Optional[Dict[int, List[Tuple[float, str]]]]:
    """A picture history file; None when there is none (or it cannot be read)."""
    try:
        with open(path, encoding="utf-8") as handle:
            return {int(k): [(float(v[0]), str(v[1])) for v in vs]
                    for k, vs in json.load(handle).items()}
    except (OSError, ValueError, TypeError, IndexError):
        return None


def _floorplan_versions(calibration_json: dict, processed_root: str, snapshot_ts: Optional[float],
                        branch_types) -> Dict[str, Dict[int, List[Tuple[float, str]]]]:
    """branch_type -> floorplan_id -> [(valid_from, image basename), ...] oldest first.

    Each branch type keeps its own picture history in
    ``<processed_root>/floorplans/versions_<branch_type>.json``, fed only by the
    snapshots of the client that has that branch type's cameras: a store whose IT
    and MONO are two clients has a Floorplan 1 on each, and one shared history gave
    each one's hours the other's picture. A file not there yet starts from the old
    shared ``versions.json``, so a store with one client keeps its versions.

    A history grows from what each snapshot reports: ``floorplans`` (the client's
    current picture per floorplan and ``image_updated_at``, set only when the
    picture is replaced, never on recalibration), else the calibrations' image
    for clients that do not send it, and ``floorplan_versions`` (the client's
    picture history, so a picture replaced again before the upload is not
    missed). A picture never seen before starts a new
    version at its ``image_updated_at`` (the snapshot start when unknown); the
    first picture known for a floorplan covers all earlier data (0). A picture
    after the one ``floorplans`` says the client has now was never this client's
    (the old shared history took it from the other one), so it is dropped.
    """
    # (floorplan_id, picture, valid_from or None) in the order they were shown:
    # the client's picture history first (every picture since the last upload,
    # with its exact start), then the current picture per floorplan.
    reported: List[Tuple[int, str, Optional[float]]] = []
    for v in (calibration_json or {}).get("floorplan_versions", []):
        try:
            reported.append((int(v["floorplan_id"]), os.path.basename(str(v["image_url"])),
                             float(v.get("valid_from") or 0.0) or None))
        except (KeyError, TypeError, ValueError):
            continue
    reported.sort(key=lambda r: r[2] or 0.0)

    current: Dict[int, Tuple[str, Optional[float]]] = {}
    for c in (calibration_json or {}).get("calibrations", []):
        try:
            fp = int(c.get("floorplan_id", 0))
        except (TypeError, ValueError):
            continue
        if fp and c.get("image_url"):
            current[fp] = (os.path.basename(str(c["image_url"])), None)
    # Only ``floorplans`` says for sure which picture is on now: a calibration can
    # still name the picture it was made on.
    now_on: Dict[int, str] = {}
    for f in (calibration_json or {}).get("floorplans", []):
        try:
            fp = int(f["floorplan_id"])
            updated = f.get("image_updated_at")
            updated = float(updated) if updated is not None else None
        except (KeyError, TypeError, ValueError):
            continue
        if f.get("image_url"):
            current[fp] = (os.path.basename(str(f["image_url"])), updated)
            now_on[fp] = current[fp][0]

    reported += [(fp, name, updated) for fp, (name, updated) in current.items()]

    shared = _read_versions(_versions_path(processed_root))
    out: Dict[str, Dict[int, List[Tuple[float, str]]]] = {}
    for branch_type in sorted({str(b).lower() for b in branch_types if b}):
        path = _versions_path(processed_root, branch_type)
        history = _read_versions(path)
        changed = history is None
        if history is None:
            history = {k: list(v) for k, v in (shared or {}).items()}
        for fp, name, updated in reported:
            entries = history.setdefault(fp, [])
            if any(n == name for _vf, n in entries):
                continue
            if not entries:
                valid_from = 0.0
            else:
                valid_from = updated or snapshot_ts or time.time()
                valid_from = max(valid_from, entries[-1][0] + 1.0)
            entries.append((valid_from, name))
            entries.sort()
            changed = True
        for fp, name in now_on.items():
            entries = history[fp]
            at = next(i for i, (_vf, n) in enumerate(entries) if n == name)
            if at + 1 < len(entries):
                del entries[at + 1:]
                changed = True
        if changed:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump({str(k): v for k, v in history.items()}, handle, indent=2)
            os.replace(tmp, path)
        out[branch_type] = history
    return out


def _client_floorplans(calibration_json: dict, manifest: dict) -> dict:
    """The floorplans the client has now (``floorplans`` of its calibration.json) and when
    it listed them, so Alitech hides a floorplan from the time it was deleted there.
    ``ids`` [] = not reported: an old client, or one whose floorplan DB could not be
    read, which is not the same as having none."""
    ids = set()
    for f in (calibration_json or {}).get("floorplans") or []:
        try:
            ids.add(int(f["floorplan_id"]))
        except (KeyError, TypeError, ValueError):
            continue
    at = (calibration_json or {}).get("generated_ts") or manifest.get("uploaded_at") or time.time()
    return {"ids": sorted(ids), "at": float(at)}


def _version_at(versions: Dict[int, List[Tuple[float, str]]], floorplan_id: int,
                ts: float) -> Tuple[float, Optional[str], float]:
    """``(valid_from, image basename, valid_to)`` of the picture shown at ``ts``.

    Data older than a floorplan's first version goes to that first version.
    """
    entries = versions.get(floorplan_id) or []
    if not entries:
        return 0.0, None, float("inf")
    index = 0
    for i, (valid_from, _name) in enumerate(entries):
        if valid_from <= ts:
            index = i
    valid_to = entries[index + 1][0] if index + 1 < len(entries) else float("inf")
    return entries[index][0], entries[index][1], valid_to


def _floorplan_label(floorplan_id: int, valid_from: float) -> str:
    """File-name part for a floorplan version: ``<id>`` for the version that has
    no known start (old outputs keep their names), else ``<id>_v<YYYYmmddHHMMSS>``
    in Bangkok time."""
    if not valid_from:
        return str(floorplan_id)
    return f"{floorplan_id}_v{datetime.fromtimestamp(valid_from, _BANGKOK):%Y%m%d%H%M%S}"


def _floorplan_image(image_paths: Dict[str, str], name: Optional[str],
                     processed_root: str) -> Optional[str]:
    """Stored path of one version's picture: from this snapshot, else the copy an
    earlier snapshot left under ``processed/<store>/floorplans``."""
    if not name:
        return None
    if name in image_paths:
        return image_paths[name]
    stem = os.path.splitext(name)[0]
    folder = os.path.join(processed_root, "floorplans")
    try:
        found = sorted(f for f in os.listdir(folder) if f.startswith(stem + "_"))
    except OSError:
        return None
    return os.path.join(folder, found[-1]) if found else None


# -------------------------------------------------------------------------------------
# image helpers
# -------------------------------------------------------------------------------------
def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_content_addressed(root: str, filename: str, data: bytes) -> str:
    """Store bytes under ``<root>/<filename_stem>_<sha12>.png``; returns the path."""
    os.makedirs(root, exist_ok=True)
    digest = _sha256_bytes(data)
    stem = os.path.splitext(os.path.basename(filename))[0]
    path = os.path.join(root, f"{stem}_{digest[:12]}{_PNG_EXT}")
    if not os.path.exists(path):
        with open(path, "wb") as handle:
            handle.write(data)
    return path


def _render_heatmap_npz(
    grid: np.ndarray,
    bucket_start: float,
    bucket_end: float,
    sample_count: int,
    unique_count: int,
    out_dir: str,
    stamp: str,
    branch_type: str,
    floorplan_id: int,
    hour: int,
) -> str:
    """Persist an aggregated grid as a bucket-style .npz and return its path.

    The file keeps the same keys the client's buckets use (``grid``,
    ``unique_grid``, ``sample_grid``, ``bucket_start``, ``bucket_end``,
    counts). The name carries the hour so different hour slots never collide:
    ``<stamp>_<HH>00_<branch_type>_<floorplan>.npz``.
    """
    os.makedirs(out_dir, exist_ok=True)
    name = f"{stamp}_{hour:02d}00_{branch_type}_{floorplan_id}{_NPZ_EXT}"
    path = os.path.join(out_dir, name)
    np.savez_compressed(
        path,
        grid=grid,
        unique_grid=grid,
        sample_grid=grid,
        bucket_start=bucket_start,
        bucket_end=bucket_end,
        sample_count=sample_count,
        unique_count=unique_count,
    )
    return path


def _render_cctv_heatmap_npz(
    sample_grid: np.ndarray,
    unique_grid: np.ndarray,
    bucket_start: float,
    bucket_end: float,
    sample_count: int,
    unique_count: int,
    out_dir: str,
    stamp: str,
    cam: str,
    hour: int,
) -> str:
    """Persist one camera's CCTV-frame heatmap as a bucket-style .npz.

    The grid lives in the camera's own frame (``camera_x``/``camera_y``
    percent space), so uncalibrated cameras still produce heatmaps. The file
    keeps the same keys the client buckets use (``grid``, ``unique_grid``,
    ``sample_grid``, ``bucket_start``, ``bucket_end``, counts); the name is
    ``<stamp>_<HH>00_<cam>.npz``.
    """
    os.makedirs(out_dir, exist_ok=True)
    name = f"{stamp}_{hour:02d}00_{cam}{_NPZ_EXT}"
    path = os.path.join(out_dir, name)
    np.savez_compressed(
        path,
        grid=unique_grid,
        unique_grid=unique_grid,
        sample_grid=sample_grid,
        bucket_start=bucket_start,
        bucket_end=bucket_end,
        sample_count=sample_count,
        unique_count=unique_count,
    )
    return path


DAILY_MANIFEST_SCHEMA = 2


def _daily_manifest_name(stamp: str, branch_type: str, floorplan_id) -> str:
    """``<stamp>_<branch_type>_<floorplan>.manifest.json`` (no hour)."""
    return f"{stamp}_{branch_type}_{floorplan_id}.manifest.json"


def _upsert_daily_heatmap_manifest(
    store_id: str,
    branch_type: str,
    floorplan_id: int,
    day: str,
    hour: int,
    start_ts: float,
    floorplan_image_path: Optional[str],
    heatmap_path: str,
    cctv_entries: List[dict],
    out_dir: str,
    valid_from: float = 0.0,
) -> str:
    """Merge one hour slot into the day manifest; returns its path.

    One manifest per floorplan version (``valid_from``, see
    ``_floorplan_label``): a day in which the picture changed has two.

    The day manifest keeps hourly detail under ``hours`` (``{"17": {...}}``)
    so per-day file count drops 24 -> 1 without losing hour breakdowns.
    Reprocessing the same hour replaces that hour's entry (idempotent —
    the file never bloats). ``cctv_entries`` must already be filtered to this
    slot's branch_type; cameras without a calibration are kept under every
    manifest of their own branch_type (floorplan-agnostic).
    """
    os.makedirs(out_dir, exist_ok=True)
    stamp = day.replace("-", "")
    path = os.path.join(out_dir, _daily_manifest_name(
        stamp, branch_type, _floorplan_label(floorplan_id, valid_from)))
    try:
        with open(path, encoding="utf-8") as handle:
            manifest = json.load(handle)
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema") != DAILY_MANIFEST_SCHEMA
            or not isinstance(manifest.get("hours"), dict)
        ):
            raise ValueError("legacy manifest")
    except (OSError, ValueError, json.JSONDecodeError):
        manifest = {
            "schema": DAILY_MANIFEST_SCHEMA,
            "store_id": store_id,
            "branch_type": branch_type,
            "floorplan_id": floorplan_id,
            "floorplan_valid_from": valid_from or None,
            "date": day,
            "hours": {},
        }
    manifest["hours"][str(hour)] = {
        "start_ts": start_ts,
        "floorplan_image_path": _store_rel(floorplan_image_path, store_id)
        if floorplan_image_path else None,
        "floorplan_heatmap_path": _store_rel(heatmap_path, store_id),
        "cctv_heatmaps": cctv_entries,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


class _CctvAccumulator:
    """Per (camera, day, hour) CCTV-frame grids from raw camera-space points.

    Mirrors the client's ``GridAccumulator`` dedup rule (one sample per person
    per cell) but in the camera's own frame, so uncalibrated cameras still
    produce heatmaps. Every observation counts — customers, staff, cal:false.
    """

    def __init__(self):
        self._samples: Dict[Tuple[str, str, int], np.ndarray] = {}
        self._uniques: Dict[Tuple[str, str, int], np.ndarray] = {}
        self._seen: Dict[Tuple[str, str, int], set] = {}
        self._sample_count: Dict[Tuple[str, str, int], int] = {}
        self._unique_count: Dict[Tuple[str, str, int], int] = {}

    def add(self, cam: str, day: str, hour: int, camera_x: float,
            camera_y: float, unique_key: str) -> None:
        gx = int((camera_x / 100.0) * GRID_W)
        gy = int((camera_y / 100.0) * GRID_H)
        if not (0 <= gx < GRID_W and 0 <= gy < GRID_H):
            return
        key = (cam, day, hour)
        samples = self._samples.get(key)
        if samples is None:
            samples = np.zeros((GRID_H, GRID_W), dtype=np.float32)
            uniques = np.zeros((GRID_H, GRID_W), dtype=np.float32)
            seen: set = set()
            self._samples[key] = samples
            self._uniques[key] = uniques
            self._seen[key] = seen
            self._sample_count[key] = 0
            self._unique_count[key] = 0
        else:
            uniques = self._uniques[key]
            seen = self._seen[key]
        samples[gy, gx] += 1.0
        self._sample_count[key] += 1
        cell_key = (unique_key, gx, gy)
        if cell_key not in seen:
            seen.add(cell_key)
            uniques[gy, gx] += 1.0
            self._unique_count[key] += 1

    def keys(self) -> List[Tuple[str, str, int]]:
        return sorted(self._samples)

    def write_all(self, processed_root: str, store_id: str,
                  cameras: Dict[str, str]) -> Dict[Tuple[str, int], List[dict]]:
        """Render one npz per (camera, day, hour); group paths by (day, hour)."""
        by_hour: Dict[Tuple[str, int], List[dict]] = defaultdict(list)
        for key in self.keys():
            cam, day, hour = key
            stamp = day.replace("-", "")
            start_ts = _slot_start_ts(day, hour)
            path = _render_cctv_heatmap_npz(
                self._samples[key], self._uniques[key],
                start_ts, start_ts + 3600.0,
                self._sample_count[key], self._unique_count[key],
                os.path.join(processed_root, "heatmaps", "camera"),
                stamp, cam, hour,
            )
            camera_key = _cam_to_camera_key(cam)
            by_hour[(day, hour)].append({
                "camera_key": camera_key,
                "cam": cam,
                "branch_type": cameras.get(camera_key),
                "file_path": _store_rel(path, store_id),
            })
        return by_hour


def _store_rel(path: Optional[str], store_id: str) -> Optional[str]:
    """``processed/<store_id>/x.png`` -> ``processed/<store_id>/x.png``.

    Anchored at the backend working dir (where ``processed/`` lives) so the
    forwarded payload path is directly fetchable from the admin root.
    """
    if not path:
        return None
    try:
        return os.path.relpath(path, os.curdir).replace(os.sep, "/")
    except ValueError:
        return path.replace(os.sep, "/")


# -------------------------------------------------------------------------------------
# preprocess
# -------------------------------------------------------------------------------------
_IMAGE_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")


def _extract_images(zf: zipfile.ZipFile) -> Dict[str, bytes]:
    """Floorplan images shipped by the client under ``floorplan/images/``.

    Only PNG/JPEG bytes count: a zip packed by hand on a Mac also carries
    ``.DS_Store`` and ``__MACOSX/._*`` files, and one of those was once taken
    for the floorplan image.
    """
    images: Dict[str, bytes] = {}
    for name in zf.namelist():
        if "/images/" not in name or not name.startswith("floorplan/"):
            continue
        base = os.path.basename(name)
        if not base or base.startswith("."):
            continue
        data = zf.read(name)
        if data.startswith(_IMAGE_MAGIC):
            images[base] = data
    return images


def _extract_buckets(zf: zipfile.ZipFile) -> Dict[str, List[Tuple[float, bytes]]]:
    """Per-camera .npz heatmap buckets from ``heatmap/buckets/camera/<ip>_<ch>/*.npz``.

    Returns ``{cam_dir: [(bucket_start_ts, npz_bytes), ...]}`` keyed by the
    ``cam_<ip>_<channel>`` directory name (matching ``slot_cams`` below). The
    sibling ``heatmap/buckets/floorplan/<id>/`` rollup (the edge's own
    per-floorplan sum, redundant with per-camera here) is naturally excluded
    by the prefix — per branch_type heatmaps are re-aggregated from the
    per-camera buckets so it/mono never bleed into each other.
    """
    buckets: Dict[str, List[Tuple[float, bytes]]] = defaultdict(list)
    prefix = "heatmap/buckets/camera/"
    for name in zf.namelist():
        if not name.startswith(prefix) or not name.endswith(".npz"):
            continue
        rel = name[len(prefix):]
        parts = rel.split("/")
        if len(parts) < 2:
            continue
        ip_ch, filename = parts[0], parts[1]
        cam_dir = "cam_" + ip_ch.replace(".", "_")
        try:
            ts = datetime.strptime(filename[:-4], "%Y%m%d_%H%M%S").replace(
                tzinfo=_BANGKOK
            ).timestamp()
        except ValueError:
            continue
        buckets[cam_dir].append((ts, zf.read(name)))
    for entries in buckets.values():
        entries.sort(key=lambda item: item[0])
    return buckets


def _cam_dir_to_key(cam_dir: str) -> Optional[str]:
    """``cam_192_168_2_240_1`` -> ``192.168.2.240_1`` (manifest cameras key)."""
    parts = str(cam_dir).split("_")
    if len(parts) < 6 or parts[0] != "cam":
        return None
    try:
        return f"{'.'.join(parts[1:5])}_{int(parts[5])}"
    except ValueError:
        return None


def _decode_buckets(
    buckets: Dict[str, List[Tuple[float, bytes]]],
) -> Dict[str, List[Tuple[float, "np.ndarray", int]]]:
    """Decode each bucket .npz once per snapshot.

    The old per-slot aggregation re-decoded the same bucket bytes for every
    hour slot; with 24 slots that is 24x wasted CPU. Decoding once here and
    summing from memory is identical output for a fraction of the cost.
    """
    decoded: Dict[str, List[Tuple[float, "np.ndarray", int]]] = {}
    for cam_dir, entries in buckets.items():
        out: List[Tuple[float, "np.ndarray", int]] = []
        for ts, data in entries:
            try:
                with np.load(io.BytesIO(data), allow_pickle=False) as d:
                    out.append((
                        ts,
                        d["unique_grid"].astype(np.float32),
                        int(d.get("unique_count", 0)),
                    ))
            except (KeyError, OSError, ValueError):
                continue
        out.sort(key=lambda item: item[0])
        decoded[cam_dir] = out
    return decoded


def _aggregate_buckets(
    decoded: Dict[str, List[Tuple[float, "np.ndarray", int]]],
    cam_dirs: List[str],
    start_ts: float,
    valid_from: float = 0.0,
    valid_to: float = float("inf"),
) -> Tuple[np.ndarray, int]:
    """Sum pre-decoded ``unique_grid`` of the given cameras within [start_ts, start_ts+1h),
    limited to buckets that started while one floorplan version was shown."""
    grid = np.zeros((GRID_H, GRID_W), dtype=np.float32)
    end_ts = min(start_ts + 3600.0, valid_to)
    start_ts = max(start_ts, valid_from)
    count = 0
    for cam_dir in cam_dirs:
        for ts, bucket_grid, bucket_count in decoded.get(cam_dir, []):
            if start_ts <= ts < end_ts:
                grid += bucket_grid
                count += bucket_count
    return grid, count


def _load_reid_assignments(zf: zipfile.ZipFile) -> Dict[Tuple[str, int], dict]:
    """Identity assignments from ``assignments/reid_*.jsonl``.

    Keys are ``(camera_key, track_id)`` matching the observation ``cam``/``t``
    pair, e.g. ``("cam_192_168_2_240_1", 2215)``. Only the newest assignment
    per (camera, track) is kept.
    """
    out: Dict[Tuple[str, int], dict] = {}
    for rec in _iter_text(zf, "assignments/"):
        if rec.get("type") != "identity.assignment":
            continue
        try:
            key = (str(rec["camera_key"]), int(rec["track_id"]))
        except (KeyError, TypeError, ValueError):
            continue
        if not rec.get("identity_key") and rec.get("global_id") is None:
            continue
        prev = out.get(key)
        if prev is None or float(rec.get("assigned_ts") or 0) >= float(prev.get("assigned_ts") or 0):
            out[key] = rec
    return out


def _backfill_identity(rec: dict, assignments: Dict[Tuple[str, int], dict], stale_seconds: float) -> None:
    """Fill null identity on an observation from the reid assignment stream.

    Mirrors the client's ``PersonLookup.lookup`` rule (DLC events/person_lookup.py):
    the assignment is valid for ``source_ts``..``source_ts + stale``; observations outside that
    window are left untouched (ByteTrack may have reused the number).
    """
    if rec.get("identity_key") or rec.get("global_id") is not None:
        return
    try:
        key = (str(rec["cam"]), int(rec["t"]))
        ts = float(rec["ts"])
    except (KeyError, TypeError, ValueError):
        return
    assign = assignments.get(key)
    if assign is None:
        return
    # Mirror the client's PersonLookup.lookup: the assignment stays
    # valid until stale_seconds after its source_ts (ByteTrack may have
    # reused the number). Observations earlier than source_ts are the same
    # track's pre-match period and are still backfilled.
    source_ts = float(assign.get("source_ts") or 0.0)
    if ts > source_ts + stale_seconds:
        return
    rec["identity_key"] = assign.get("identity_key")
    rec["global_id"] = assign.get("global_id")
    # Carry the assignment's group only when the observation has none:
    # the calibration binding (ip/cam -> group) is authoritative.
    if rec.get("group") is None:
        for alias in ("group", "group_id", "floorplan_group"):
            try:
                if assign.get(alias) is not None:
                    rec["group"] = _coerce_group(assign.get(alias))
                    break
            except AttributeError:
                continue


def preprocess_zip_bytes(zip_bytes: bytes, store_id: str, processed_root: str) -> List[dict]:
    """Run one snapshot through the whole pipeline and return forward payloads."""
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    manifest = _read_json(zf, "manifest.json")
    # Both are written by floorplan_service, which a branch may not run (one on the
    # recorder/analyzer pipeline has none). Without them every camera is simply
    # uncalibrated: the CCTV-frame heatmaps below still come out. Requiring them
    # failed the whole snapshot, and the same zip was retried and failed every pass.
    calibration = _read_json(zf, "floorplan/calibration.json", {})
    zone_json = _read_json(zf, "floorplan/zone.json", {})

    floorplan_by_cam = _load_calibration(calibration)
    projection_by_cam = _load_zone_projection(calibration)
    group_by_cam = _load_floorplan_groups(calibration)
    cameras = {str(k): str(v).lower() for k, v in (manifest.get("cameras") or {}).items()}
    versions = _floorplan_versions(calibration, processed_root, manifest.get("start_ts"), cameras.values())
    client_floorplans = _client_floorplans(calibration, manifest)
    zones = _load_zones(zone_json)
    images = _extract_images(zf)
    buckets = _extract_buckets(zf)
    assignments = _load_reid_assignments(zf)

    # content-address the floorplan images once per snapshot
    image_paths: Dict[str, str] = {}
    for filename, data in images.items():
        image_paths[filename] = _write_content_addressed(
            os.path.join(processed_root, "floorplans"), filename, data,
        )

    # collect per (branch_type, floorplan_id, floorplan version, date, hour)
    # observations, backfilling any identity lost to the client-side lookup race.
    # An hour in which the floorplan got a new picture is split in two: rows
    # before the change stay with the old picture.
    by_slot: Dict[Tuple[str, int, float, str, int], List[dict]] = defaultdict(list)
    # CCTV-frame heatmaps accumulate alongside, from raw camera-space foot
    # points, so cameras without a floorplan calibration still produce
    # heatmaps (customers + staff + cal:false all count).
    cctv = _CctvAccumulator()
    for rec in _iter_text(zf, "heatmap/observations/"):
        slot = _hour_stamp_from_name(rec.pop("_name", ""))
        if slot is None:
            continue
        day, hour = slot
        cam = str(rec.get("cam") or "")
        if cam:
            try:
                camera_x = float(rec.get("camera_x"))
                camera_y = float(rec.get("camera_y"))
            except (TypeError, ValueError):
                camera_x = camera_y = None
            if (camera_x is not None and camera_y is not None
                    and np.isfinite(camera_x) and np.isfinite(camera_y)):
                unique_key = rec.get("identity_key") or f"track:{cam}:{rec.get('t')}"
                cctv.add(cam, day, hour, camera_x, camera_y, str(unique_key))
        cam_key = _cam_to_camera_key(cam)
        branch_type = cameras.get(cam_key)
        floorplan_id = floorplan_by_cam.get(cam_key, 0)
        if not branch_type:
            continue
        _backfill_identity(rec, assignments, stale_seconds=10.0)
        # Effective shared-GID namespace: explicit group wins, else the
        # floorplan itself (per-floorplan isolation fallback for old
        # snapshots without the DB group field).
        _explicit = group_by_cam.get(cam_key)
        if rec.get("group") is None:
            rec["group"] = _explicit if _explicit is not None else floorplan_id
        try:
            rec_ts = float(rec["ts"])
        except (KeyError, TypeError, ValueError):
            rec_ts = _slot_start_ts(day, hour)
        valid_from = _version_at(versions.get(branch_type, {}), floorplan_id, rec_ts)[0]
        by_slot[(branch_type, floorplan_id, valid_from, day, hour)].append(rec)

    cctv_heatmap_files = cctv.write_all(processed_root, store_id, cameras)

    # zones belonging to each (branch_type, floorplan_id) group via their camera
    slot_zones: Dict[Tuple[str, int], List[dict]] = defaultdict(list)
    slot_cams: Dict[Tuple[str, int], List[str]] = defaultdict(list)
    for cam_key, branch_type in cameras.items():
        floorplan_id = floorplan_by_cam.get(cam_key)
        if not floorplan_id:
            continue
        cam_dir = "cam_" + cam_key.replace(".", "_")
        slot_cams[(branch_type, floorplan_id)].append(cam_dir)
    for zone in zones:
        cam_key = zone["camera_key"]
        branch_type = cameras.get(cam_key)
        floorplan_id = floorplan_by_cam.get(cam_key)
        if branch_type and floorplan_id is not None:
            if zone["kind"] == "hotzone":
                zone["shape"] = _project_zone_points(zone["points"], projection_by_cam.get(cam_key))
            slot_zones[(branch_type, floorplan_id)].append(zone)

    payloads: List[dict] = []
    decoded_buckets = _decode_buckets(buckets)
    for (branch_type, floorplan_id, valid_from, day, hour), rows in sorted(by_slot.items()):
        stamp = day.replace("-", "")
        start_ts = _slot_start_ts(day, hour)
        _vf, image_name, valid_to = _version_at(versions.get(branch_type, {}), floorplan_id, valid_from)
        image_path = _floorplan_image(image_paths, image_name, processed_root)
        label = _floorplan_label(floorplan_id, valid_from)
        heatmap_dir = os.path.join(processed_root, "heatmaps", "floorplan")
        grid, count = _aggregate_buckets(
            decoded_buckets, slot_cams.get((branch_type, floorplan_id), []), start_ts,
            valid_from, valid_to,
        )
        heatmap_path = _render_heatmap_npz(
            grid, start_ts, start_ts + 3600.0, count, count,
            heatmap_dir, stamp, branch_type, label, hour,
        )
        payload = _aggregate_slot(
            store_id, branch_type, floorplan_id, start_ts, rows,
            slot_zones.get((branch_type, floorplan_id), []),
            image_path, heatmap_path,
        )
        payload["floorplan_id"] = floorplan_id
        payload["floorplan_valid_from"] = valid_from or None
        payload["client_floorplans"] = client_floorplans
        # CCTV entries are grouped per (day, hour) across every camera, so
        # filter to this slot's branch_type — otherwise each manifest lists
        # every camera of the hour and downstream double-counts them.
        hour_cctv = [
            e for e in cctv_heatmap_files.get((day, hour), [])
            if e.get("branch_type") == branch_type
        ]
        manifest_path = _upsert_daily_heatmap_manifest(
            store_id, branch_type, floorplan_id, day, hour, start_ts,
            image_path,
            heatmap_path,
            hour_cctv,
            os.path.join(processed_root, "heatmaps"),
            valid_from,
        )
        payload["heatmap_manifest_path"] = _store_rel(manifest_path, store_id)
        payloads.append(payload)
    zf.close()
    return payloads


def _slot_start_ts(day: str, hour: int) -> float:
    """Unix epoch at the start of the (day, hour) slot in Bangkok time."""
    return datetime.strptime(f"{day} {hour:02d}", "%Y-%m-%d %H").replace(
        tzinfo=_BANGKOK
    ).timestamp()


def _aggregate_slot(
    store_id: str,
    branch_type: str,
    floorplan_id: int,
    start_ts: float,
    rows: List[dict],
    zones: List[dict],
    floorplan_path: Optional[str],
    heatmap_path: str,
) -> dict:
    # filter usable observations: customers only, and only rows that carry an
    # identity. `cus_emp is None` cannot be distinguished from an employee, so
    # it is excluded too — the label is baked into the observation at write
    # time and never recovered later.
    usable = [
        r for r in rows
        if r.get("cal") and r.get("cus_emp") == "cus"
        and (r.get("identity_key") or r.get("global_id"))
    ]

    # person key: global_id is stable across ReID boots, identity_key is not
    # (``<boot_id>[:<group>]:<global_id>``). Grouping by (group, global_id)
    # means the same person re-appearing under a new boot_id is still counted
    # once, while the same number in different groups stays separate people.
    # Missing group falls back to this slot's floorplan (per-floorplan
    # isolation, matching the by_slot split).
    def _person_key(r: dict) -> str:
        if r.get("global_id") is not None:
            _g = r.get("group")
            try:
                _g = int(_g) if _g is not None else int(floorplan_id)
            except (TypeError, ValueError):
                _g = floorplan_id
            return f"g:{_g}:{r['global_id']}"
        return f"i:{r['identity_key']}"

    # person dwell (person spent time = max_ts - min_ts)
    person_range: Dict[str, List[float]] = defaultdict(lambda: [float("inf"), float("-inf")])
    person_points: Dict[str, List[dict]] = defaultdict(list)
    for r in usable:
        try:
            ts = float(r["ts"])
            x = float(r.get("x", 0.0)) / 100.0
            y = float(r.get("y", 0.0)) / 100.0
        except (KeyError, TypeError, ValueError):
            continue
        key = _person_key(r)
        rng = person_range[key]
        rng[0] = min(rng[0], ts)
        rng[1] = max(rng[1], ts)
        person_points[key].append({"ts": ts, "x": x, "y": y})

    dwells = []
    for rng in person_range.values():
        if rng[1] >= rng[0]:
            dwells.append(rng[1] - rng[0])
    avg_spent = round(sum(dwells) / len(dwells), 1) if dwells else None
    median_spent = None
    if dwells:
        ordered = sorted(dwells)
        median_spent = round(ordered[len(ordered) // 2], 1)

    # per-zone dwell (avg time per person inside the zone)
    zone_rows = []
    for zone in zones:
        zone_avg, visits = _zone_dwell(zone, person_points)
        zone_rows.append({
            "id": zone["id"],
            "name": zone["name"],
            "type": zone["kind"],
            "subtype": zone["subtype"],
            "avg_spent_seconds": round(zone_avg, 1) if zone_avg is not None else None,
            "visit_count": visits,
            # hotzone only, and only when the polygon could be placed on the floorplan
            "color": zone.get("color") if zone.get("shape") else None,
            "points": zone.get("shape"),
        })

    return {
        "store_id": store_id,
        "branch_type": branch_type,
        "start_ts": start_ts,
        "avg_spent_seconds": avg_spent,
        "median_spent_seconds": median_spent,
        "zones": zone_rows,
        "floorplan_file_path": _store_rel(floorplan_path, store_id),
        "heatmap_file_path": _store_rel(heatmap_path, store_id),
    }


def _zone_dwell(zone: dict, person_points: Dict[str, List[dict]]) -> Tuple[Optional[float], int]:
    """Average time people spent inside a zone polygon (mean dwell per person).

    Returns ``(avg_spent_seconds, visit_count)``. ``avg_spent_seconds`` is the
    mean dwell of every person who entered the zone; ``visit_count`` counts
    only the people whose dwell reached ``_MIN_DWELL_SECONDS``.

    A bounding-box precheck skips people with no points near the zone before
    the (costly) point-in-polygon test — identical results, less CPU.
    """
    dwells: List[float] = []
    polygon = zone["points"]
    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    for pts in person_points.values():
        inside = [p for p in pts
                  if min_x <= p["x"] <= max_x and min_y <= p["y"] <= max_y
                  and _point_in_polygon((p["x"], p["y"]), polygon)]
        if not inside:
            continue
        ts = sorted(p["ts"] for p in inside)
        dwells.append(ts[-1] - ts[0])
    avg = round(sum(dwells) / len(dwells), 1) if dwells else None
    visits = sum(1 for d in dwells if d >= _MIN_DWELL_SECONDS)
    return avg, visits


def _store_zips(store_id: str) -> List[str]:
    store_dir = os.path.join(DUMMY_DATA_PATH, store_id)
    try:
        names = os.listdir(store_dir)
    except OSError:
        return []
    return [os.path.join(store_dir, n) for n in names if n.endswith(".zip")]


def _latest_zip(store_id: str) -> Optional[str]:
    """The most recently uploaded pipeline zip for a store (by start_ts name)."""
    zips = _store_zips(store_id)
    if not zips:
        return None
    return max(zips, key=lambda p: os.path.getmtime(p))


def preprocess_store(store_id: str) -> List[dict]:
    """Locate the latest pipeline zip for a store and preprocess it.

    The processed zip is marked in the queue state, so the background loop
    does not process it a second time; when it was already handled (e.g. by
    the loop) this returns [] instead of duplicating work and forwards.
    One store is never preprocessed concurrently (upload thread vs loop).
    """
    path = _latest_zip(store_id)
    if path is None:
        raise PreprocessError(f"no pipeline zip for store {store_id}")
    with _store_lock(store_id):
        fingerprint = _zip_fingerprint(path)
        if (
            fingerprint is not None
            and _load_queue_state(store_id).get(os.path.basename(path)) == fingerprint
        ):
            logger.info(
                "store %s: %s already processed, skipping",
                store_id, os.path.basename(path),
            )
            return []
        with open(path, "rb") as handle:
            zip_bytes = handle.read()
        processed_root = os.path.join(PROCESSED_DATA_PATH, store_id)
        payloads = preprocess_zip_bytes(zip_bytes, store_id, processed_root)
        _mark_zip_done(store_id, path)
        return payloads


# -------------------------------------------------------------------------------------
# forward
# -------------------------------------------------------------------------------------
def forward_payloads(
    store_id: str,
    payloads: List[dict],
    url: Optional[str] = None,
    timeout: float = 60.0,
) -> int:
    """Forward all payloads for a store; returns how many were sent.

    Each payload already carries ``floorplan_file_path``/``heatmap_file_path``
    and a ``heatmap_manifest_path`` (``processed/<store_id>/...`` paths)
    computed during preprocess and is POSTed as-is. ``url`` defaults to the
    module-level forward URL.
    """
    if not payloads:
        return 0
    if url is None:
        url = PIPELINE_FORWARD_URL
    if not url:
        return 0
    headers = {
        "Accept": "application/json",
        "User-Agent": "avi-admin-preprocess",
    }
    sent = 0
    for payload in payloads:
        # The client's floorplan list is for store_payloads; the forward body stays as it was.
        payload = {k: v for k, v in payload.items() if k != "client_floorplans"}
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            logger.error("store %s: forward request failed: %s", store_id, exc)
            continue
        if response.status_code >= 400:
            logger.error(
                "store %s: forward status=%s body=%s",
                store_id, response.status_code, response.text[:300],
            )
            continue
        sent += 1
    return sent


# -------------------------------------------------------------------------------------
# store: the payloads go into the Zone Hour SQLite file, where Alitech reads them
# -------------------------------------------------------------------------------------
# Same file as ai_camera_service's zone_hour (ZONE_HOUR_DB_PATH), made on first write.
# One floorplan_hour row per payload: branch_type × floorplan × picture version × hour.
# The heatmap and the floorplan picture stay files under processed/; the rows keep their
# absolute paths, so a reader on this box opens them as they are.
FLOORPLAN_HOUR_DDL = """
CREATE TABLE IF NOT EXISTS floorplan_hour (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id              TEXT    NOT NULL,
    branch_type           TEXT    NOT NULL,   -- it / mono
    start_ts              REAL    NOT NULL,   -- start of the hour (unix)
    floorplan_id          INTEGER NOT NULL,
    floorplan_valid_from  REAL    NOT NULL DEFAULT 0,  -- payload null -> 0 (first version)
    avg_spent_seconds     REAL,
    median_spent_seconds  REAL,
    floorplan_file_path   TEXT,               -- this version's floorplan picture
    heatmap_file_path     TEXT    NOT NULL,
    heatmap_manifest_path TEXT,
    created_at            TEXT    NOT NULL,   -- insert time (Thai local)
    UNIQUE (store_id, branch_type, floorplan_id, floorplan_valid_from, start_ts)
);
CREATE TABLE IF NOT EXISTS floorplan_hour_zone (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    floorplan_hour_id   INTEGER NOT NULL REFERENCES floorplan_hour(id) ON DELETE CASCADE,
    zone_id             TEXT    NOT NULL,   -- payload zones[].id
    zone_name           TEXT    NOT NULL,   -- zones[].name
    zone_type           TEXT    NOT NULL,   -- zones[].type: hotzone / rate / in_pass / attendance
    zone_subtype        TEXT,               -- zones[].subtype
    avg_spent_seconds   REAL,               -- zones[].avg_spent_seconds
    visit_count         INTEGER NOT NULL,   -- zones[].visit_count
    zone_color          TEXT,               -- zones[].color (hotzone drawn on the floorplan)
    zone_points         TEXT,               -- zones[].points as JSON [[x, y], ...], floorplan percent 0-100
    UNIQUE (floorplan_hour_id, zone_type, zone_id)
);
-- Each floorplan a client has listed (payload client_floorplans), per store and branch type,
-- and when it left the list (deleted on the client). Alitech hides a floorplan's hours from
-- deleted_at on; a floorplan with no row (a client that does not list them) is not hidden.
CREATE TABLE IF NOT EXISTS floorplan_client (
    store_id      TEXT    NOT NULL,
    branch_type   TEXT    NOT NULL,
    floorplan_id  INTEGER NOT NULL,
    listed_at     REAL    NOT NULL,   -- the client list this row last came from (unix)
    deleted_at    REAL,               -- NULL = the client still has it
    PRIMARY KEY (store_id, branch_type, floorplan_id)
);
"""
# Columns added after the first release; a file made before them gets them on its next write.
FLOORPLAN_HOUR_ZONE_EXTRA_COLUMNS = (("zone_color", "TEXT"), ("zone_points", "TEXT"))


def _abs_path(path: Optional[str]) -> Optional[str]:
    return os.path.abspath(path) if path else None


def store_payloads(payloads: List[dict], db_path: Optional[str] = None) -> int:
    """Upsert payloads into floorplan_hour / floorplan_hour_zone; returns how many.

    A payload already stored (same store, branch_type, floorplan, version and hour) is
    replaced, its zones too: a later snapshot of an hour wins. The client's floorplan list
    (client_floorplans) marks in floorplan_client which floorplans it still has and when
    the others were deleted. All in one transaction.
    ZONE_HOUR_DB_PATH unset stores nothing (and says so); a sqlite3 error is raised to the
    caller, which logs it and keeps the zip for its next pass.
    """
    path = db_path or os.getenv("ZONE_HOUR_DB_PATH")
    if not path:
        logger.warning("floorplan_hour: ZONE_HOUR_DB_PATH is not set, %d payloads not stored", len(payloads))
        return 0
    if not payloads:
        return 0
    now = datetime.now(_BANGKOK).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(FLOORPLAN_HOUR_DDL)
        have = {r[1] for r in conn.execute("PRAGMA table_info(floorplan_hour_zone)")}
        for column, kind in FLOORPLAN_HOUR_ZONE_EXTRA_COLUMNS:
            if column not in have:
                conn.execute(f"ALTER TABLE floorplan_hour_zone ADD COLUMN {column} {kind}")
        with conn:
            for p in payloads:
                key = (str(p["store_id"]), str(p["branch_type"]).lower(), int(p.get("floorplan_id") or 0),
                       float(p.get("floorplan_valid_from") or 0.0), float(p["start_ts"]))
                values = (p.get("avg_spent_seconds"), p.get("median_spent_seconds"),
                          _abs_path(p.get("floorplan_file_path")), _abs_path(p["heatmap_file_path"]),
                          _abs_path(p.get("heatmap_manifest_path")), now)
                row = conn.execute(
                    "SELECT id FROM floorplan_hour WHERE store_id = ? AND branch_type = ?"
                    " AND floorplan_id = ? AND floorplan_valid_from = ? AND start_ts = ?", key).fetchone()
                if row:
                    hour_id = row[0]
                    conn.execute(
                        "UPDATE floorplan_hour SET avg_spent_seconds = ?, median_spent_seconds = ?,"
                        " floorplan_file_path = ?, heatmap_file_path = ?, heatmap_manifest_path = ?,"
                        " created_at = ? WHERE id = ?", values + (hour_id,))
                    conn.execute("DELETE FROM floorplan_hour_zone WHERE floorplan_hour_id = ?", (hour_id,))
                else:
                    hour_id = conn.execute(
                        "INSERT INTO floorplan_hour (store_id, branch_type, floorplan_id,"
                        " floorplan_valid_from, start_ts, avg_spent_seconds, median_spent_seconds,"
                        " floorplan_file_path, heatmap_file_path, heatmap_manifest_path, created_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", key + values).lastrowid
                conn.executemany(
                    "INSERT OR REPLACE INTO floorplan_hour_zone (floorplan_hour_id, zone_id, zone_name,"
                    " zone_type, zone_subtype, avg_spent_seconds, visit_count, zone_color, zone_points)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [(hour_id, str(z.get("id") or ""), str(z.get("name") or ""), str(z.get("type") or ""),
                      z.get("subtype"), z.get("avg_spent_seconds"), int(z.get("visit_count") or 0),
                      z.get("color") if z.get("points") else None,
                      json.dumps(z["points"]) if z.get("points") else None)
                     for z in p.get("zones") or []])
            for (store, branch_type), listed in _latest_client_lists(payloads).items():
                _store_client_list(conn, store, branch_type, listed)
    finally:
        conn.close()
    return len(payloads)


def _latest_client_lists(payloads: List[dict]) -> Dict[Tuple[str, str], dict]:
    """(store, branch_type) -> the newest client floorplan list among the payloads;
    an empty one (not reported) is skipped."""
    lists: Dict[Tuple[str, str], dict] = {}
    for p in payloads:
        listed = p.get("client_floorplans") or {}
        if not listed.get("ids"):
            continue
        key = (str(p["store_id"]), str(p["branch_type"]).lower())
        if key not in lists or listed["at"] >= lists[key]["at"]:
            lists[key] = listed
    return lists


def _store_client_list(conn: sqlite3.Connection, store: str, branch_type: str, listed: dict) -> None:
    """Apply one client floorplan list: a listed floorplan is in use, one known before
    (listed earlier, or with hours stored) but not listed now was deleted at ``at``.
    A list older than the one already applied (a zip retried late) changes nothing."""
    at, ids = float(listed["at"]), {int(i) for i in listed["ids"]}
    newest = conn.execute("SELECT MAX(listed_at) FROM floorplan_client WHERE store_id = ? AND branch_type = ?",
                          (store, branch_type)).fetchone()[0]
    if newest is not None and at < newest:
        return
    known = {r[0] for r in conn.execute(
        "SELECT floorplan_id FROM floorplan_client WHERE store_id = ? AND branch_type = ?"
        " UNION SELECT floorplan_id FROM floorplan_hour WHERE store_id = ? AND branch_type = ?",
        (store, branch_type, store, branch_type))}
    conn.executemany(
        "INSERT INTO floorplan_client (store_id, branch_type, floorplan_id, listed_at, deleted_at)"
        " VALUES (?, ?, ?, ?, NULL) ON CONFLICT (store_id, branch_type, floorplan_id)"
        " DO UPDATE SET listed_at = excluded.listed_at, deleted_at = NULL",
        [(store, branch_type, i, at) for i in sorted(ids)])
    conn.executemany(
        "INSERT INTO floorplan_client (store_id, branch_type, floorplan_id, listed_at, deleted_at)"
        " VALUES (?, ?, ?, ?, ?) ON CONFLICT (store_id, branch_type, floorplan_id)"
        " DO UPDATE SET listed_at = excluded.listed_at, deleted_at = COALESCE(deleted_at, excluded.deleted_at)",
        [(store, branch_type, i, at, at) for i in sorted(known - ids)])


def _store_logged(store_id: str, payloads: List[dict]) -> int:
    """store_payloads, with a failure logged instead of raised (forwarding still runs)."""
    try:
        stored = store_payloads(payloads)
        if stored:
            logger.info("store %s: %d payloads stored in floorplan_hour", store_id, stored)
        return stored
    except sqlite3.Error as exc:
        logger.error("store %s: storing %d payloads failed: %s", store_id, len(payloads), exc)
        return 0


def preprocess_and_forward(store_id: str) -> dict:
    """Preprocess the latest snapshot for a store and forward it if configured."""
    payloads = preprocess_store(store_id)
    result = {
        "store_id": store_id,
        "preprocessed": len(payloads),
        "forwarded": 0,
        "stored": 0,
        "payloads": payloads,
    }
    if not payloads:
        return result
    result["stored"] = _store_logged(store_id, payloads)
    if not PIPELINE_FORWARD_URL:
        logger.info("store %s: %d payloads preprocessed, forward URL not configured", store_id, len(payloads))
        return result
    result["forwarded"] = forward_payloads(store_id, payloads)
    return result


# -------------------------------------------------------------------------------------
# queue (background loop)
# -------------------------------------------------------------------------------------
_store_locks: Dict[str, threading.Lock] = {}
_store_locks_guard = threading.Lock()


def _store_lock(store_id: str) -> threading.Lock:
    """One lock per store so the upload thread and the queue never preprocess
    the same store concurrently (with hundreds of stores, different stores
    still run in parallel)."""
    with _store_locks_guard:
        lock = _store_locks.get(store_id)
        if lock is None:
            lock = threading.Lock()
            _store_locks[store_id] = lock
        return lock


def _mark_zip_done(store_id: str, path: str) -> None:
    """Record a zip fingerprint as processed (shared by upload + queue paths)."""
    fingerprint = _zip_fingerprint(path)
    if fingerprint is None:
        return
    state = _load_queue_state(store_id)
    state[os.path.basename(path)] = fingerprint
    _save_queue_state(store_id, state)


def _queue_state_path(store_id: str) -> str:
    return os.path.join(PROCESSED_DATA_PATH, store_id, "queue_state.json")


def _load_queue_state(store_id: str) -> Dict[str, str]:
    try:
        with open(_queue_state_path(store_id), encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_queue_state(store_id: str, state: Dict[str, str]) -> None:
    path = _queue_state_path(store_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)


def _zip_fingerprint(path: str) -> Optional[str]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return f"{int(st.st_mtime)}:{int(st.st_size)}"


def _cleanup_raw(store_id: str) -> int:
    """Delete raw pipeline zips older than the retention window."""
    cutoff = time.time() - PIPELINE_RAW_RETENTION_DAYS * 86400
    removed = 0
    for path in _store_zips(store_id):
        try:
            if os.stat(path).st_mtime < cutoff:
                os.remove(path)
                removed += 1
        except OSError:
            continue
    return removed


# A zip that failed is tried again only after this long (or once it changes): the
# queue passes every minute, and the same broken zip used to log a full traceback
# each time. Kept in memory, so a restart gives every zip one fresh try.
RETRY_FAILED_AFTER_SECONDS = 6 * 3600
_failed_zips: Dict[Tuple[str, str], Tuple[str, float]] = {}


def process_store(store_id: str) -> dict:
    """Preprocess + forward every new zip for a store; prune old raw zips."""
    with _store_lock(store_id):
        state = _load_queue_state(store_id)
        result = {
            "store_id": store_id,
            "processed": 0,
            "forwarded": 0,
            "stored": 0,
            "cleaned": 0,
            "skipped": 0,
        }

        for path in sorted(_store_zips(store_id)):
            fingerprint = _zip_fingerprint(path)
            if fingerprint is None:
                continue
            if state.get(os.path.basename(path)) == fingerprint:
                result["skipped"] += 1
                continue
            failed = _failed_zips.get((store_id, os.path.basename(path)))
            if failed and failed[0] == fingerprint and time.time() - failed[1] < RETRY_FAILED_AFTER_SECONDS:
                result["skipped"] += 1
                continue
            try:
                with open(path, "rb") as handle:
                    zip_bytes = handle.read()
                processed_root = os.path.join(PROCESSED_DATA_PATH, store_id)
                payloads = preprocess_zip_bytes(zip_bytes, store_id, processed_root)
                result["processed"] += 1
            except Exception as exc:
                _failed_zips[(store_id, os.path.basename(path))] = (fingerprint, time.time())
                # A zip we cannot read is one short line; anything else is a bug and keeps
                # its traceback. Either way once per RETRY_FAILED_AFTER_SECONDS, not per pass.
                logger.error("store %s: preprocess %s failed: %s (next try in %d h)",
                             store_id, os.path.basename(path), exc, RETRY_FAILED_AFTER_SECONDS // 3600,
                             exc_info=not isinstance(exc, PreprocessError))
                continue

            result["stored"] += _store_logged(store_id, payloads)
            result["forwarded"] += forward_payloads(store_id, payloads)

            state[os.path.basename(path)] = fingerprint
            _save_queue_state(store_id, state)

        result["cleaned"] = _cleanup_raw(store_id)
        return result


def run_once() -> List[dict]:
    """One queue pass over every store; returns per-store results."""
    try:
        store_ids = sorted(os.listdir(DUMMY_DATA_PATH))
    except OSError:
        return []
    results = []
    for store_id in store_ids:
        if not os.path.isdir(os.path.join(DUMMY_DATA_PATH, store_id)):
            continue
        if not _store_zips(store_id):
            continue
        try:
            results.append(process_store(store_id))
        except Exception as exc:
            logger.error("store %s: queue pass failed: %s", store_id, exc, exc_info=True)
    return results


_loop_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()


def start_loop() -> None:
    """Start the background queue loop (no-op when disabled or already running)."""
    global _loop_thread
    if not PIPELINE_QUEUE_ENABLED:
        logger.info("pipeline queue disabled")
        return
    if _loop_thread is not None and _loop_thread.is_alive():
        return
    _stop_event.clear()

    def _loop():
        logger.info("pipeline queue loop started (interval=%ss)", PIPELINE_QUEUE_INTERVAL_SECONDS)
        while not _stop_event.is_set():
            try:
                results = run_once()
                for r in results:
                    if not (r["processed"] or r["forwarded"] or r["cleaned"]):
                        continue  # nothing new for this store; one line a minute each said nothing
                    logger.info(
                        "queue %s: processed=%d forwarded=%d cleaned=%d skipped=%d",
                        r["store_id"], r["processed"], r["forwarded"],
                        r["cleaned"], r["skipped"],
                    )
            except Exception:
                logger.exception("pipeline queue pass failed")
            _stop_event.wait(PIPELINE_QUEUE_INTERVAL_SECONDS)

    _loop_thread = threading.Thread(target=_loop, name="pipeline-queue", daemon=True)
    _loop_thread.start()


def stop_loop() -> None:
    _stop_event.set()