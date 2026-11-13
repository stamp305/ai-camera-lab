# backend/build_farm_gateway.py
"""Build farm gateway — lets the remote Mac Mini builder talk to this backend.

Real-time trigger:
    Socket.IO namespace ``/build``. Builders authenticate with a shared key
    (``x_builder_key`` auth payload) and receive ``build_start
    {version, commit}`` events whenever a version is published to the farm.

One-time job REST API (all under ``/build-farm``):
    POST /build-farm/jobs                    -> create job for a version
         header x-builder-key, body {"version": "V.x.y", "commit": "<sha>"}
    POST /build-farm/jobs/{id}/result        -> signed artifact upload
         headers x-job-token / x-manifest / x-signature + raw gzip body

This backend never handles plaintext source. It names a commit; the builder
archives that commit out of its own clone of the client repo, stamps its own
public key into the payload's verifier, compiles and signs. Artifacts are
verified against the builder's ed25519 public key before they are extracted
into the version cache, which therefore only ever holds compiled trees.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shutil
import tarfile
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

import socketio
from fastapi import APIRouter, Header, HTTPException, Request

from config import (
    ADMIN_BUILDER_KEY,
    BACKEND_PUBLIC_URL,
    BUILD_JOBS_DIR,
    MANAGED_SERVICES_VERSIONS_CACHE,
    BUILDER_SIGNING_PUBKEY,
)

logger = logging.getLogger("build_farm_gateway")

# --------------------------------------------------------------------------------------
# Socket.IO server (/build namespace)
# --------------------------------------------------------------------------------------
sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")

_pending_notifications: list = []
_loop: Optional[asyncio.AbstractEventLoop] = None

# sids of builders currently on /build. A job can only make progress while at
# least one of them is connected, so this set decides whether a build request
# is real and whether in-flight jobs are still alive.
_connected_builders: set = set()

# Artifact base URL each connected builder announced for itself (sid -> url).
# The builder is the only party that knows its own public hostname, and behind
# a free-tier tunnel that hostname changes on every restart — so it tells us on
# connect rather than us keeping a copy that goes stale without saying so.
_builder_urls: Dict[str, str] = {}

# A pending job older than this is dead: the builder never came back for it.
_PENDING_TIMEOUT_SECS = int(os.getenv("AI_CAMERA_ADMIN_BUILD_TIMEOUT_SECS", "1800"))


def set_event_loop(loop: Optional[asyncio.AbstractEventLoop]) -> None:
    """Called once the ASGI loop is running so sync code can emit events."""
    global _loop
    _loop = loop


def _drain_pending() -> None:
    while _pending_notifications:
        data = _pending_notifications.pop(0)
        asyncio.ensure_future(sio.emit("build_start", data, namespace="/build"))


@sio.on("connect", namespace="/build")
async def _on_build_connect(sid, environ, auth=None):
    if not isinstance(auth, dict) or auth.get("x_builder_key") != ADMIN_BUILDER_KEY:
        logger.warning("[BUILD GATEWAY] Builder rejected (bad builder key): sid=%s", sid)
        raise socketio.exceptions.ConnectionError("unauthorized")
    _connected_builders.add(sid)
    logger.info("[BUILD GATEWAY] Builder connected: sid=%s (%d online)", sid, len(_connected_builders))
    _drain_pending()


@sio.on("disconnect", namespace="/build")
async def _on_build_disconnect(sid):
    _connected_builders.discard(sid)
    _builder_urls.pop(sid, None)
    logger.info("[BUILD GATEWAY] Builder disconnected: sid=%s (%d online)", sid, len(_connected_builders))
    if not _connected_builders:
        # Nothing can finish a pending job any more, so stop reporting them as
        # if they were still building.
        n = _fail_pending("Builder disconnected before the build finished.")
        if n:
            logger.warning("[BUILD GATEWAY] No builders left — failed %d in-flight job(s)", n)


@sio.on("builder_ready", namespace="/build")
async def _on_builder_ready(sid, data=None):
    logger.info("[BUILD GATEWAY] Builder ready: sid=%s data=%s", sid, data)
    url = str((data or {}).get("artifact_url", "")).rstrip("/")
    if url:
        _builder_urls[sid] = url
        logger.info("[BUILD GATEWAY] Builder %s serves artifacts at %s", sid, url)


def _artifact_base_url(sid: str = "") -> str:
    """Where to fetch this builder's artifacts from.

    What the builder announced wins: it is live truth from the machine holding
    the artifact. AI_CAMERA_ADMIN_FARM_TUNNEL_URL stays as the fallback so a
    worker too old to announce anything keeps working exactly as before — but
    it is a value someone typed once, and it goes on pointing at yesterday's
    tunnel long after that tunnel moved.
    """
    announced = _builder_urls.get(sid, "")
    if announced:
        return announced
    if len(_builder_urls) == 1 and len(_connected_builders) == 1:
        # Announcement and job_ready came from the same single builder even if
        # the sid rolled over between them — no ambiguity about whose URL it is.
        return next(iter(_builder_urls.values()))
    return os.getenv("AI_CAMERA_ADMIN_FARM_TUNNEL_URL", "http://127.0.0.1:8765").rstrip("/")


@sio.on("job_ready", namespace="/build")
async def _on_job_ready(sid, data=None):
    """Builder finished compiling — download artifact from builder artifact server."""
    data = data or {}
    job_id = data.get("job_id") or data.get("jobId") or data.get("id")
    if not job_id:
        logger.warning("[BUILD GATEWAY] job_ready with no job_id: sid=%s data=%s", sid, data)
        return
    logger.info("[BUILD GATEWAY] job_ready received for job %s — downloading artifact", job_id)

    _load_jobs()
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        logger.warning("[BUILD GATEWAY] job_ready for unknown job %s", job_id)
        return
    if job.get("status") != "pending":
        logger.info("[BUILD GATEWAY] job_ready for job %s — already %s, skipping", job_id, job.get("status"))
        return

    vid = job["version"]
    tunnel_url = _artifact_base_url(sid)
    builder_key = ADMIN_BUILDER_KEY
    logger.info("[BUILD GATEWAY] Job %s: fetching artifact from %s (%s)", job_id, tunnel_url,
                "announced by builder" if _builder_urls.get(sid) else "admin config")

    try:
        import requests as _req

        # Step 1: Get one-time download token
        _set_stage(job, "downloading")
        token_url = f"{tunnel_url}/jobs/{job_id}/token"
        token_resp = _req.get(token_url, headers={"x-builder-key": builder_key}, timeout=30)
        token_resp.raise_for_status()
        download_token = token_resp.json()["token"]
        logger.info("[BUILD GATEWAY] Got download token for job %s", job_id)

        # Step 2: Download artifact (response headers contain x-manifest + x-signature)
        artifact_url = f"{tunnel_url}/artifact/{job_id}?t={download_token}"
        art_resp = _req.get(artifact_url, headers={"x-builder-key": builder_key}, timeout=120, stream=True)
        art_resp.raise_for_status()
        artifact_bytes = art_resp.content
        manifest_header = art_resp.headers.get("x-manifest", "")
        signature = art_resp.headers.get("x-signature", "")
        logger.info("[BUILD GATEWAY] Downloaded artifact for job %s (%d bytes)", job_id, len(artifact_bytes))

        # Step 3: Verify signature + install
        _set_stage(job, "installing")
        if manifest_header and signature:
            manifest = _parse_manifest(manifest_header)
            if not verify_signature(manifest, signature):
                logger.warning("[BUILD GATEWAY] Job %s rejected: bad signature", job_id)
                with _jobs_lock:
                    job["status"] = "failed"
                    job["finished_at"] = time.time()
                _save_jobs()
                return
            file_count = len(manifest.get("files", {}))
        else:
            file_count = 0
            logger.warning("[BUILD GATEWAY] Job %s: no manifest/signature in response headers", job_id)

        installed = install_artifact(vid, artifact_bytes, job_id=job_id)
        with _jobs_lock:
            job["status"] = "done"
            job["stage"] = "done"
            job["token"] = None
            job["finished_at"] = time.time()
            job["manifest_files"] = file_count or installed
        _save_jobs()
        logger.info("[BUILD GATEWAY] Job %s done: version=%s files=%s", job_id, vid, file_count or installed)

        # Step 4: Confirm to builder (cleanup)
        try:
            confirm_url = f"{tunnel_url}/jobs/{job_id}/confirm"
            _req.post(confirm_url, headers={"x-builder-key": builder_key}, timeout=10)
            logger.info("[BUILD GATEWAY] Confirmed job %s to builder", job_id)
        except Exception as e:
            logger.warning("[BUILD GATEWAY] Could not confirm job %s to builder: %s", job_id, e)

    except Exception as e:
        logger.error("[BUILD GATEWAY] Job %s artifact download failed: %s", job_id, e)
        with _jobs_lock:
            job["status"] = "failed"
            job["finished_at"] = time.time()
        _save_jobs()


def notify_build_start(version_id: str, commit: str = "") -> int:
    """Emit build_start to every connected builder (thread-safe).

    ``commit`` is the sha the builder must archive out of its own clone of the
    client repo. This backend no longer materialises source at all — it names
    the commit and receives compiled .so files back.

    Returns how many builders the event reached. Zero means no build will
    happen — callers must surface that instead of reporting success.
    """
    data = {"version": version_id, "commit": commit}
    if _loop is None or not _loop.is_running():
        _pending_notifications.append(data)
        logger.info("[BUILD GATEWAY] build_start queued for '%s' (no loop yet)", version_id)
        return 0
    count = len(_connected_builders)
    if not count:
        logger.warning(
            "[BUILD GATEWAY] build_start for '%s' dropped — no builder connected", version_id
        )
        return 0
    asyncio.run_coroutine_threadsafe(
        sio.emit("build_start", data, namespace="/build"), _loop
    )
    logger.info("[BUILD GATEWAY] build_start emitted for '%s' to %d builder(s)", version_id, count)
    return count


def connected_builder_count() -> int:
    """How many builders are on /build right now."""
    return len(_connected_builders)


# --------------------------------------------------------------------------------------
# One-time build jobs
# --------------------------------------------------------------------------------------
_jobs_lock = threading.Lock()
_jobs: Dict[str, Dict[str, Any]] = {}
_jobs_file: Optional[Path] = None

# Per-version build stage tracker — derived from jobs.json (survives restarts).
# The get_build_stages() function reads from _jobs and maps status to stage.


def _jobs_path() -> Path:
    global _jobs_file
    if _jobs_file is None:
        root = Path(BUILD_JOBS_DIR)
        root.mkdir(parents=True, exist_ok=True)
        _jobs_file = root / "jobs.json"
    return _jobs_file


def _load_jobs() -> None:
    with _jobs_lock:
        if _jobs or not _jobs_path().exists():
            return
        try:
            raw = json.loads(_jobs_path().read_text())
            # Jobs never survive a restart as 'pending' — stale ones are dead.
            now = time.time()
            for jid, j in raw.items():
                if j.get("status") == "pending" and now - j.get("created_at", 0) > 3600:
                    j["status"] = "expired"
                _jobs[jid] = j
        except Exception as e:
            logger.warning("[BUILD GATEWAY] Could not load jobs file: %s", e)


def _save_jobs() -> None:
    with _jobs_lock:
        try:
            _jobs_path().write_text(json.dumps(_jobs, indent=1))
        except Exception as e:
            logger.warning("[BUILD GATEWAY] Could not save jobs file: %s", e)


def _require_builder_key(x_builder_key: str) -> None:
    if x_builder_key != ADMIN_BUILDER_KEY:
        raise HTTPException(status_code=401, detail="Invalid builder key")


# Where each admin-side stage sits on the job-wide bar. The builder owns
# everything up to 96 (git fetch / compile / sign / pack); these are what
# happens after the artifact exists, on this side of the wire.
_STAGE_PERCENT = {"downloading": 97, "installing": 99, "done": 100}


def _set_stage(job: Dict[str, Any], stage: str) -> None:
    """Update the build stage on a job dict and persist."""
    job["stage"] = stage
    pct = _STAGE_PERCENT.get(stage)
    if pct is not None:
        # Never let the bar walk backwards: the builder's last message may
        # already have claimed more than the stage floor.
        job["percent"] = max(int(job.get("percent") or 0), pct)
    _save_jobs()


def _fail_pending(reason: str, older_than: Optional[float] = None) -> int:
    """Mark pending jobs failed; with ``older_than``, only ones that old.

    ``_load_jobs`` applies its staleness rule once, at first load, so a job
    orphaned while the server keeps running would otherwise stay 'pending'
    forever and the UI would show it as still building.
    """
    _load_jobs()
    now = time.time()
    failed = 0
    with _jobs_lock:
        for job in _jobs.values():
            if job.get("status") != "pending":
                continue
            if older_than is not None and now - job.get("created_at", now) < older_than:
                continue
            job["status"] = "failed"
            job["message"] = reason
            job["finished_at"] = now
            failed += 1
    if failed:
        _save_jobs()
    return failed


def _latest_job_per_version() -> Dict[str, Dict[str, Any]]:
    """The newest job for each version.

    A version is rebuilt many times, so the store holds several jobs for it.
    Only the newest one describes what is happening now — reading them all and
    letting the last write win lets a finished job's message mask the running
    one's.
    """
    latest: Dict[str, Dict[str, Any]] = {}
    with _jobs_lock:
        for job in _jobs.values():
            vid = job.get("version", "")
            best = latest.get(vid)
            if best is None or job.get("created_at", 0) >= best.get("created_at", 0):
                latest[vid] = job
    return latest


def get_build_stages() -> Dict[str, str]:
    """Derive build stages from the persisted jobs store (survives restarts)."""
    _fail_pending(
        f"No result from the builder within {_PENDING_TIMEOUT_SECS}s.",
        older_than=_PENDING_TIMEOUT_SECS,
    )
    _load_jobs()
    stages: Dict[str, str] = {}
    for vid, job in _latest_job_per_version().items():
        status = job.get("status", "")
        stage = job.get("stage", "")
        if status == "done":
            stages[vid] = "done"
        elif status in ("expired", "failed", "cancelled"):
            stages[vid] = "failed"
        elif status == "pending" and stage:
            stages[vid] = stage
        elif status == "pending":
            stages[vid] = "building"
    return stages


def get_build_messages() -> Dict[str, str]:
    """Latest human-readable progress line reported by the builder, per version."""
    _load_jobs()
    messages: Dict[str, str] = {}
    for vid, job in _latest_job_per_version().items():
        # 'failed' included: the reason a build died is the one message the
        # operator actually needs.
        if job.get("status") in ("pending", "failed") and job.get("message"):
            messages[vid] = job["message"]
    return messages


def get_build_progress() -> Dict[str, Dict[str, Any]]:
    """Stage, message and percent per version in one read.

    The UI polls twice a second while a build runs; asking three endpoints for
    three views of the same job record made that three round trips that could
    disagree with each other mid-build.
    """
    stages = get_build_stages()
    messages = get_build_messages()
    out: Dict[str, Dict[str, Any]] = {}
    for vid, job in _latest_job_per_version().items():
        stage = stages.get(vid, "none")
        entry: Dict[str, Any] = {"stage": stage, "message": messages.get(vid, "")}
        if stage == "done":
            entry["percent"] = 100
        elif stage == "failed":
            entry["percent"] = None
        elif job.get("percent") is not None:
            entry["percent"] = int(job["percent"])
        else:
            entry["percent"] = None
        out[vid] = entry
    return out


def cancel_jobs_for_version(version_id: str) -> int:
    """Mark all pending jobs for a version as cancelled.

    Note: this only tells the server to stop waiting on / accepting the
    result for this job — the Mac Mini worker has no interrupt hook, so an
    in-flight Cython compile keeps running there until it finishes; its
    eventual upload will just be rejected since the job is no longer
    'pending' (see _get_job's status check).
    """
    _load_jobs()
    count = 0
    with _jobs_lock:
        for job in _jobs.values():
            if job.get("version") == version_id and job.get("status") == "pending":
                job["status"] = "cancelled"
                count += 1
    if count:
        _save_jobs()
    return count


def forget_jobs_for_version(version_id: str) -> int:
    """Drop every job record for a version that no longer exists.

    Without this a deleted version leaves its last job behind, so re-storing
    the same id shows that old job's stage and message until a new job is
    created — and if no builder is connected, no new job ever is, leaving the
    page reporting a failure that belongs to a version that was thrown away.
    """
    _load_jobs()
    with _jobs_lock:
        gone = [jid for jid, job in _jobs.items() if job.get("version") == version_id]
        for jid in gone:
            del _jobs[jid]
    if gone:
        _save_jobs()
        logger.info("[BUILD GATEWAY] Forgot %d job record(s) for deleted version '%s'",
                    len(gone), version_id)
    return len(gone)


def _get_job(job_id: str, token: str, expect_status: str = "pending") -> Dict[str, Any]:
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job or job.get("token") != token:
        raise HTTPException(status_code=403, detail="Unknown job, bad token, or token already consumed")
    if job.get("status") != expect_status:
        raise HTTPException(status_code=409, detail=f"Job is '{job['status']}', expected '{expect_status}'")
    return job


router = APIRouter(prefix="/build-farm", tags=["build-farm"])


@router.get("/build-stages")
def api_build_stages():
    """Return all current build stages keyed by version_id (for frontend polling)."""
    return get_build_stages()


@router.get("/build-messages")
def api_build_messages():
    """Return the latest progress message per version_id (for frontend polling)."""
    return get_build_messages()


@router.get("/build-progress")
def api_build_progress():
    """Stage + message + percent per version_id, in one poll."""
    return get_build_progress()


@router.post("/jobs/{job_id}/message")
def api_post_build_message(job_id: str, payload: dict, x_job_token: str = Header(default="")):
    """Builder reports a human-readable progress line for its in-flight job.

    An optional ``percent`` (0-100) fills the UI's progress bar. A builder too
    old to send one simply leaves it absent, and the bar stays indeterminate —
    which is what it always was.
    """
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job or job.get("token") != x_job_token:
            raise HTTPException(status_code=403, detail="Unknown job or bad token")
        if job.get("status") != "pending":
            return {"ok": False}
        job["message"] = str((payload or {}).get("message", ""))[:300]
        pct = (payload or {}).get("percent")
        if pct is not None:
            try:
                # Monotonic: two compile passes report their own 0..1 and the
                # admin's own stages land on top, so an out-of-order write must
                # not make the bar jump back.
                job["percent"] = max(int(job.get("percent") or 0),
                                     max(0, min(100, int(pct))))
            except (TypeError, ValueError):
                pass
    _save_jobs()
    return {"ok": True}


@router.post("/jobs/{job_id}/fail")
def api_report_job_failed(job_id: str, payload: dict = None, x_job_token: str = Header(default="")):
    """Builder reports that it could not complete/deliver this job."""
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job or job.get("token") != x_job_token:
            raise HTTPException(status_code=403, detail="Unknown job or bad token")
        if job.get("status") == "pending":
            job["status"] = "failed"
            job["message"] = str((payload or {}).get("reason", ""))[:300] or "Build failed"
    _save_jobs()
    logger.info("[BUILD GATEWAY] Job %s reported failed: %s", job_id, (payload or {}).get("reason", ""))
    return {"ok": True}


@router.post("/jobs/cancel-by-version/{version_id}")
def api_cancel_build(version_id: str):
    """Cancel pending build job(s) for a version (called from the admin UI)."""
    count = cancel_jobs_for_version(version_id)
    if not count:
        raise HTTPException(status_code=404, detail=f"No pending build job for '{version_id}'")
    return {"message": f"Cancelled {count} job(s) for '{version_id}'."}


@router.post("/jobs")
def create_job(payload: dict, x_builder_key: str = Header(default="")):
    """Create a one-time build job for a published version."""
    _require_builder_key(x_builder_key)
    vid = (payload or {}).get("version", "").strip()
    if not vid:
        raise HTTPException(status_code=400, detail="Missing 'version'")
    # The builder fetches the commit itself, so there is no source snapshot to
    # check for here any more. It echoes back the sha it was told to build so
    # the job record says exactly what was compiled.
    commit = str((payload or {}).get("commit", "")).strip()
    if not commit:
        raise HTTPException(status_code=400, detail=f"Missing 'commit' for '{vid}'")

    _load_jobs()
    job_id = secrets.token_hex(5)
    token = secrets.token_hex(16)
    job = {
        "job_id": job_id,
        "token": token,
        "version": vid,
        "commit": commit,
        "status": "pending",
        "stage": "building",
        "created_at": time.time(),
    }
    with _jobs_lock:
        _jobs[job_id] = job
    _save_jobs()
    logger.info("[BUILD GATEWAY] Job %s created for '%s' (commit %s)", job_id, vid, commit[:10])
    return {
        "job_id": job_id,
        "token": token,
        "version": vid,
        "commit": commit,
    }


@router.post("/jobs/{job_id}/result")
async def post_result(
    request: Request,
    job_id: str,
    x_job_token: str = Header(default=""),
    x_manifest: str = Header(default=""),
    x_signature: str = Header(default=""),
):
    """Accept a signed artifact, verify it, and install into the version cache."""
    _load_jobs()
    job = _get_job(job_id, x_job_token)

    manifest = _parse_manifest(x_manifest)
    if not verify_signature(manifest, x_signature):
        logger.warning("[BUILD GATEWAY] Job %s rejected: bad signature", job_id)
        raise HTTPException(status_code=403, detail="Signature verification failed")

    artifact = await request.body()
    if not artifact:
        raise HTTPException(status_code=400, detail="Empty artifact body")

    try:
        installed = install_artifact(job["version"], artifact, job_id=job_id)
    except Exception as e:
        logger.error("[BUILD GATEWAY] Job %s artifact install failed: %s", job_id, e)
        raise HTTPException(status_code=500, detail=f"Artifact install failed: {e}")

    with _jobs_lock:
        job["status"] = "done"
        job["token"] = None  # Invalidate token — one-time use
        job["finished_at"] = time.time()
        job["manifest_files"] = len(manifest.get("files", {}))
    _save_jobs()
    logger.info(
        "[BUILD GATEWAY] Job %s done: version=%s files=%s",
        job_id, job["version"], len(manifest.get("files", {})),
    )
    return {"status": "accepted", "version": job["version"], "installed_entries": installed}


def _parse_manifest(raw: str) -> Dict[str, Any]:
    try:
        manifest = json.loads(raw)
        if not isinstance(manifest, dict):
            raise ValueError("manifest must be a JSON object")
        return manifest
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid x-manifest header")


def verify_signature(manifest: Dict[str, Any], sig_b64: str) -> bool:
    """Verify the ed25519 signature over the canonical manifest bytes."""
    pub_path = Path(BUILDER_SIGNING_PUBKEY)
    if not pub_path.exists():
        logger.error("[BUILD GATEWAY] Builder public key missing: %s", pub_path)
        return False
    try:
        import base64
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
        with open(pub_path, "rb") as f:
            pub = serialization.load_pem_public_key(f.read())
        if isinstance(pub, Ed25519PublicKey):
            pub.verify(base64.b64decode(sig_b64), canonical)
            return True
        logger.error("[BUILD GATEWAY] Public key is not ed25519: %s", type(pub).__name__)
        return False
    except Exception as e:
        logger.warning("[BUILD GATEWAY] Signature check failed: %s", e)
        return False


# Written into a version folder the moment a signed artifact is installed.
# Its absence is what tells the update pusher "this tree was never built" —
# a freshly extracted tree is plaintext source and must never reach a branch.
BUILD_STATE_FILE = ".build_state.json"


def build_state(version_id: str) -> Optional[Dict[str, Any]]:
    """Return the build receipt for a version, or None if it has none."""
    path = Path(MANAGED_SERVICES_VERSIONS_CACHE) / version_id / BUILD_STATE_FILE
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def is_version_built(version_id: str) -> bool:
    """True when this version's folder came from a verified build artifact."""
    return build_state(version_id) is not None


# Lowest macOS a payload must load on. The Python the client app ships is
# itself built for 12.1, so no store that runs the app at all can be below it —
# building to this floor covers the whole fleet without surveying it.
MAX_PAYLOAD_MIN_MACOS = os.getenv("AI_CAMERA_ADMIN_MAX_PAYLOAD_MIN_MACOS", "12.1")

_MACHO64 = (0xFEEDFACF, 0xCFFAEDFE)
_LC_VERSION_MIN_MACOSX = 0x24
_LC_BUILD_VERSION = 0x32


def macho_min_macos(data: bytes) -> Optional[tuple]:
    """Lowest macOS this Mach-O will load on, or None if it does not say.

    Read straight out of the load commands because this server is Linux and has
    no otool — and because the number matters here more than anywhere else: a
    bundle built for a newer macOS than the store runs is refused by dyld at
    import, so the app does not start at all.
    """
    import struct
    if len(data) < 32:
        return None
    magic = struct.unpack("<I", data[:4])[0]
    if magic not in _MACHO64:
        return None  # fat binaries and non-Mach-O are not our concern here
    endian = "<" if magic == 0xFEEDFACF else ">"
    ncmds = struct.unpack(endian + "I", data[16:20])[0]
    off, best = 32, None
    for _ in range(ncmds):
        if off + 8 > len(data):
            break
        cmd, size = struct.unpack(endian + "II", data[off:off + 8])
        if size == 0:
            break
        if cmd in (_LC_BUILD_VERSION, _LC_VERSION_MIN_MACOSX):
            pos = off + 12 if cmd == _LC_BUILD_VERSION else off + 8
            if pos + 4 <= len(data):
                v = struct.unpack(endian + "I", data[pos:pos + 4])[0]
                got = (v >> 16, (v >> 8) & 0xFF, v & 0xFF)
                if best is None or got > best:
                    best = got
        off += size
    return best


def _check_payload_runs_on_fleet(tree: Path) -> None:
    """Reject a payload whose .so need a newer macOS than the fleet floor.

    The builder checks this too, from the deployment target of the Python doing
    the compiling. This second look is on the bytes that actually arrived, so a
    builder that is misconfigured, out of date, or simply not the one we expect
    cannot put a payload into the cache that bricks a store on import.
    """
    # Pad both to three parts before comparing: Python orders a longer tuple
    # above a shorter one with the same prefix, so a bare (12, 1) ceiling would
    # reject the (12, 1, 0) it is supposed to accept.
    def _triple(parts) -> tuple:
        v = tuple(int(x) for x in parts)[:3]
        return v + (0,) * (3 - len(v))

    ceiling = _triple(MAX_PAYLOAD_MIN_MACOS.split("."))
    offenders = []
    for so in tree.rglob("*.so"):
        try:
            got = macho_min_macos(so.read_bytes())
        except OSError:
            continue
        if got and _triple(got) > ceiling:
            offenders.append((str(so.relative_to(tree)), ".".join(map(str, got))))
    if offenders:
        shown = ", ".join(f"{n} (macOS {v}+)" for n, v in offenders[:3])
        raise RuntimeError(
            f"payload ต้องการ macOS ใหม่กว่าเพดาน {MAX_PAYLOAD_MIN_MACOS}: {shown}"
            + (f" และอีก {len(offenders) - 3} ไฟล์" if len(offenders) > 3 else "")
            + " — สาขาที่ macOS ต่ำกว่านี้จะ import ไม่ผ่านตั้งแต่แอพสตาร์ต"
              " ตรวจ FARM_CYTHON_PY ของเครื่อง build ว่าชี้ python ที่ deployment"
              " target ต่ำพอหรือยัง"
        )


def install_artifact(version_id: str, artifact: bytes, job_id: str = "") -> int:
    """Extract a compiled payload tarball into the version cache (atomic-ish)."""
    cache_dir = Path(MANAGED_SERVICES_VERSIONS_CACHE)
    target_dir = cache_dir / version_id
    tmp_dir = cache_dir / f".incoming-{version_id}-{secrets.token_hex(4)}"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True)
    count = 0
    tmp_tgz = tempfile.NamedTemporaryFile(delete=False, suffix=".tar.gz")
    try:
        tmp_tgz.write(artifact)
        tmp_tgz.close()
        with tarfile.open(tmp_tgz.name, mode="r:gz") as tar:
            members = [m for m in tar.getmembers()
                       if not os.path.isabs(m.name) and ".." not in Path(m.name).parts]
            tar.extractall(tmp_dir, members=members, filter="data")
        # Before anything is published: a payload that cannot load is worse than
        # no payload, because the store only finds out when its app stops
        # starting. tmp_dir is thrown away on the way out, so a rejection here
        # leaves the currently-serving version untouched.
        _check_payload_runs_on_fleet(tmp_dir)
        for _root, _dirs, files in os.walk(tmp_dir):
            count += len(files)
        (tmp_dir / BUILD_STATE_FILE).write_text(json.dumps({
            "version": version_id,
            "job_id": job_id,
            "installed_at": time.time(),
            "files": count,
        }, indent=1))
        count += 1
        if target_dir.exists():
            shutil.rmtree(target_dir)
        tmp_dir.rename(target_dir)
        return count
    finally:
        try:
            os.unlink(tmp_tgz.name)
        except Exception:
            pass
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
