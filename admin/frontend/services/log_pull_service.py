# frontend/services/log_pull_service.py
"""Pull log files from a connected branch over Socket.IO ack calls.

The branch side already implements two ack events that this module talks to
via `extensions.socketio.call()` (blocks the calling greenlet until the
branch acks or the timeout fires):

    log_list -> {"ok": true, "files": [{"root": "log"|"logs", "path": str,
                 "size": int, "mtime": float}], "truncated"?: true}
                or {"ok": false, "error": code}

    log_read -> {"ok": true, "data": base64, "size": total, "offset": int,
                 "eof": bool} or {"ok": false, "error": code}
                (codes: bad_request, bad_root, bad_path, not_found, io_error)

This module never touches raw sockets - all branch state comes from
services.dlc_service.update_clients (populated by socket_handlers.sio_identify).

Nothing pulled here is kept permanently: every pull (single-file or
pull-all) lands under a short-lived *viewer session* directory
(LOGS_BASE_PATH/<session_id>/<client_id>/<root>/<path>). The browser
generates the session_id up front (a fresh one per viewing round) and keeps
using it for the rest of that round; either it explicitly deletes the
session (delete_session) or it expires and gets swept up by the next
housekeeping pass.

pull-all is a *background job*, not a request/response call: starting one
(start_pull_all_job) validates the request, registers an in-memory job
record, wipes/recreates the session/client_id dir, and hands the actual
sequential per-file pulling off to `_run_in_background` (Flask-SocketIO's
`socketio.start_background_task` in production - an eventlet greenlet under
this app's async_mode - swapped for a synchronous/threaded runner in tests,
since sockeito.call needs no app context but the bare SocketIO() instance
used in tests has no server configured at all). The browser polls
get_session_status(session_id) (GET /api/logs/sessions/<id>) until the job
reaches a terminal state.
"""
from __future__ import annotations

import base64
import logging
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from socketio.exceptions import TimeoutError as SocketIOTimeoutError

from config import Config
from extensions import socketio
from services.dlc_service import update_clients, update_clients_lock

_logger = logging.getLogger(__name__)

# 256 KiB per log_read call - matches the branch-side cap on `length`.
CHUNK = 262144
# Refuse to keep pulling a single file past this many bytes so a runaway (or
# malicious) log can't fill up the admin's disk.
MAX_PULL_BYTES = 50 * 1024 * 1024

# How long an idle viewer session's on-disk copy survives before the
# housekeeping sweep deletes it (and drops its job record, if any is still
# hanging around finished). Refreshed (touch_session) on every pull, every
# successful /content read, and every status poll, so an admin actively
# viewing logs never loses them mid-session.
SESSION_TTL_SECONDS = 7200
# Cap on the total bytes a single pull-all will write for one client_id
# inside a session, so one bulk pull can't fill up the admin's disk either.
MAX_SESSION_BYTES = 300 * 1024 * 1024
# Cap on the combined size of everything under LOGS_BASE_PATH (every
# session, every client) - MAX_SESSION_BYTES only bounds one job; without
# this, enough concurrent/abandoned sessions could still fill the disk.
MAX_TOTAL_BYTES = 1024 * 1024 * 1024  # 1 GiB

# GET /api/logs/branches triggers the same TTL sweep pull-all does, but that
# endpoint is polled far more often than pull-all is invoked, so it is
# throttled to at most once per this many seconds (module-level timestamp,
# best-effort - a race just means an extra sweep runs, which is harmless).
SWEEP_THROTTLE_SECONDS = 60.0
_last_sweep_at = 0.0

_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_VALID_ROOTS = {"log", "logs"}

# 32 lowercase-hex chars == secrets.token_hex(16). Exported (no leading
# underscore) so blueprints/api_logs.py can reuse the exact same shape check
# for the /content/<local_path> first-segment guard and for the request-body
# session_id checks on pull-all/pull.
SESSION_ID_RE = re.compile(r"^[0-9a-f]{32}$")

# Root that camera snapshot stills live under on the branch side. Pulling this
# subtree through the generic log-pull path is blocked defence-in-depth (the
# UI already never offers it) so a crafted/typo'd request can't be used to
# bulk-drag camera stills off a branch through the "logs" feature.
_BLOCKED_SNAPSHOT_ROOT = "log"
_BLOCKED_SNAPSHOT_PREFIX = "monitor_snapshots"

# Branch-reported log_read/log_list error codes mapped to the HTTP status the
# blueprint should answer with; anything not listed here (e.g. io_error, or
# an unrecognized code) maps to 502 since it means the branch itself failed.
_BRANCH_ERROR_HTTP_STATUS = {
    "not_found": 404,
    "bad_path": 400,
    "bad_root": 400,
    "bad_request": 400,
    "file_changed": 409,
}


class LogPullError(Exception):
    """A known, user-facing failure talking to a branch or saving its logs.

    `code` is a short machine string (also reused for branch-reported codes
    like "not_found"/"io_error"); `http_status` is what the blueprint should
    answer with.
    """

    def __init__(self, code: str, http_status: int, detail: Optional[str] = None):
        self.code = code
        self.http_status = http_status
        super().__init__(detail or code)


class _JobCancelled(Exception):
    """Internal signal only: raised inside _pull_one_file's chunk loop when
    its `should_cancel` callable flips true mid-transfer. Always caught
    inside _pull_all_job - never an HTTP-facing error, and single-file
    pull_log never passes should_cancel so it can never see this."""


def online_branches() -> list[dict]:
    """List branches currently connected over Socket.IO, sorted by client_id."""
    _maybe_sweep_expired_sessions()
    with update_clients_lock:
        items = [
            {
                "client_id": cid,
                "branch_name": (meta.get("info") or {}).get("branchName"),
                "version": (meta.get("info") or {}).get("version"),
                "connected_at": meta.get("connected_at"),
            }
            for cid, meta in update_clients.items()
        ]
    return sorted(items, key=lambda item: item["client_id"])


def _lookup_sid(client_id: str) -> Optional[str]:
    """Look up a connected branch's Socket.IO sid under the lock.

    The ack call itself must happen outside the lock: it can block for
    seconds (up to the caller's timeout), and holding update_clients_lock
    that long would stall every other request that needs it.
    """
    with update_clients_lock:
        meta = update_clients.get(client_id)
        return meta.get("sid") if meta else None


def _call_branch(sid: str, event: str, data: dict, timeout: int) -> dict:
    """Call a branch ack event and normalize its failure modes to LogPullError."""
    try:
        result = socketio.call(event, data, to=sid, timeout=timeout)
    except SocketIOTimeoutError:
        raise LogPullError("timeout", 504, f"'{event}' timed out after {timeout}s (sid={sid})")
    except Exception as exc:  # broad-but-specific: dropped socket, bad namespace, etc.
        raise LogPullError("branch_error", 502, f"'{event}' communication failed: {exc}")

    if not isinstance(result, dict) or not result.get("ok"):
        code = (result.get("error") if isinstance(result, dict) else None) or "branch_error"
        http_status = _BRANCH_ERROR_HTTP_STATUS.get(code, 502)
        raise LogPullError(code, http_status, f"'{event}' returned error: {code}")
    return result


def list_remote_logs(client_id: str) -> dict:
    """Ask a connected branch for its log file listing (log_list ack)."""
    sid = _lookup_sid(client_id)
    if sid is None:
        raise LogPullError("branch_offline", 409, f"client '{client_id}' is not connected")
    return _call_branch(sid, "log_list", {}, timeout=15)


def _validate_client_id(client_id: str) -> None:
    if not isinstance(client_id, str) or not _CLIENT_ID_RE.match(client_id):
        raise LogPullError("bad_client", 400, f"invalid client_id: {client_id!r}")


def _validate_root(root: str) -> None:
    if root not in _VALID_ROOTS:
        raise LogPullError("bad_root", 400, f"invalid root: {root!r}")


def _validate_rel_path(path: str) -> None:
    """Reject anything that is not a clean, relative, forward-slash path."""
    if not isinstance(path, str) or not path:
        raise LogPullError("bad_path", 400, "empty path")
    if path.startswith("/") or path.startswith("\\"):
        raise LogPullError("bad_path", 400, f"absolute path not allowed: {path!r}")
    if "\\" in path:
        raise LogPullError("bad_path", 400, f"backslash not allowed: {path!r}")
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise LogPullError("bad_path", 400, f"path traversal not allowed: {path!r}")


def _check_snapshot_guard(root: str, path: str) -> None:
    if root == _BLOCKED_SNAPSHOT_ROOT and path.split("/", 1)[0].casefold() == _BLOCKED_SNAPSHOT_PREFIX:
        raise LogPullError("bad_path", 400, f"pulling camera snapshots via root {root!r} is not allowed: {path!r}")


# ---------------------------------------------------------------------------
# Viewer sessions: id validation, TTL sweep, on-disk helpers.
# ---------------------------------------------------------------------------

def new_session_id() -> str:
    """Generate a fresh viewer-session id: 32 lowercase-hex chars.

    Session ids are now generated by the browser (kept here as the one
    canonical way to produce a validly-shaped one - handy for tests and for
    anything server-side that still wants to mint one)."""
    return secrets.token_hex(16)


def _validate_session_id(session_id: str) -> None:
    if not isinstance(session_id, str) or not SESSION_ID_RE.match(session_id):
        raise LogPullError("bad_session", 400, f"invalid session_id: {session_id!r}")


def _session_dir_path(session_id: str) -> str:
    # session_id is always validated against SESSION_ID_RE (32 lowercase hex
    # chars - no slashes, no dots) before this is ever called, so this join
    # can never resolve outside LOGS_BASE_PATH.
    return os.path.join(Config.LOGS_BASE_PATH, session_id)


def touch_session(session_id: str) -> None:
    """Bump a session dir's mtime so the sweep treats it as alive.

    Called after every successful pull (single or per pull-all file), every
    status poll, and every successful /content read. Best-effort: a session
    dir that doesn't exist (or a race with someone else deleting it) is
    silently ignored.
    """
    try:
        os.utime(_session_dir_path(session_id), None)
    except OSError:
        pass


def _remove_dir_safely(path: str) -> None:
    """rmtree(path) iff it exists, is a real directory, and stays inside
    LOGS_BASE_PATH. Never follows a symlink: a symlinked entry at `path`
    itself is unlinked rather than recursed into (shutil.rmtree already
    refuses to descend into symlinked subdirectories on its own)."""
    base_real = os.path.realpath(Config.LOGS_BASE_PATH)
    path_real = os.path.realpath(path)
    try:
        if os.path.commonpath([base_real, path_real]) != base_real:
            return
    except ValueError:
        return  # e.g. different drives - definitely not inside

    if os.path.islink(path):
        try:
            os.remove(path)
        except OSError:
            pass
        return
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)


def _total_bytes_under_logs_base() -> int:
    """Total bytes of every regular file under LOGS_BASE_PATH.

    Walks the tree without following symlinks: a symlinked file or directory
    never counts against MAX_TOTAL_BYTES (and is never descended into), same
    spirit as _remove_dir_safely. Missing/unreadable entries are skipped
    rather than raising - this only ever feeds a soft, best-effort cap.
    """
    base = Config.LOGS_BASE_PATH
    total = 0
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
        for name in filenames:
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                continue
            try:
                total += os.path.getsize(full)
            except OSError:
                continue
    return total


def _sweep_expired_sessions() -> None:
    """Delete session dirs directly under LOGS_BASE_PATH whose name matches
    SESSION_ID_RE and whose mtime is older than SESSION_TTL_SECONDS, and drop
    their job records (if any). Also independently drops job records for
    already-finished jobs older than SESSION_TTL_SECONDS, even if their dir
    is still fresh (touch_session on a status poll would otherwise keep a
    long-dead job's record alive in memory forever).

    A session dir with a *running* job is never touched here, regardless of
    its mtime - the job itself owns that dir's lifecycle while it runs.

    Anything that doesn't match the session-id shape - legacy client_id
    dirs from before sessions existed, .gitignore, stray files - is left
    completely untouched.
    """
    now = time.time()

    with _jobs_lock:
        running_sessions = set()
        stale_job_sessions = []
        for sid, job in _jobs.items():
            if job.state == "running":
                running_sessions.add(sid)
            elif job.finished_at is not None and (now - job.finished_at) > SESSION_TTL_SECONDS:
                stale_job_sessions.append(sid)
        for sid in stale_job_sessions:
            del _jobs[sid]

    base_abs = Config.LOGS_BASE_PATH
    try:
        names = os.listdir(base_abs)
    except OSError:
        return

    removed_sessions = []
    for name in names:
        if not SESSION_ID_RE.match(name) or name in running_sessions:
            continue
        full = os.path.join(base_abs, name)
        if os.path.islink(full) or not os.path.isdir(full):
            continue
        try:
            age = now - os.stat(full).st_mtime
        except OSError:
            continue
        if age > SESSION_TTL_SECONDS:
            shutil.rmtree(full, ignore_errors=True)
            removed_sessions.append(name)

    if removed_sessions:
        with _jobs_lock:
            for sid in removed_sessions:
                _jobs.pop(sid, None)


def _maybe_sweep_expired_sessions() -> None:
    """Run _sweep_expired_sessions, but at most once every
    SWEEP_THROTTLE_SECONDS. Used by GET /branches, which is polled far more
    often than pull-all is called (pull-all still sweeps unconditionally on
    every call, same as before)."""
    global _last_sweep_at
    now = time.time()
    if now - _last_sweep_at < SWEEP_THROTTLE_SECONDS:
        return
    _last_sweep_at = now
    _sweep_expired_sessions()


def delete_session(session_id: str) -> dict:
    """Delete (or start cancelling) a viewer session.

    Idempotent and never raises "busy": a running job only gets its cancel
    flag set (the job itself removes the dir and finishes as "cancelled" -
    see _pull_all_job), reported back as {"deleted": true, "cancelling":
    true}. Otherwise the dir is removed immediately (no-op if it never
    existed or was already removed) and any finished job's record for this
    session is dropped, reported back as {"deleted": true}.
    """
    _validate_session_id(session_id)

    with _jobs_lock:
        job = _jobs.get(session_id)
        if job is not None and job.state == "running":
            job.cancel_requested = True
            return {"deleted": True, "cancelling": True}
        _jobs.pop(session_id, None)

    _remove_dir_safely(_session_dir_path(session_id))
    return {"deleted": True}


def _resolve_dest_path(base_dir: str, client_id: str, root: str, path: str) -> str:
    """Build base_dir/<client_id>/<root>/<path> and guarantee containment
    within base_dir (the caller always passes a session dir here, which is
    itself always inside LOGS_BASE_PATH by construction)."""
    base_abs = os.path.abspath(base_dir)
    dest_path = os.path.normpath(os.path.join(base_abs, client_id, root, *path.split("/")))

    # A prior pull can leave a plain file where this pull now needs a
    # directory (or vice versa) - e.g. pulling "a" then "a/b". Surface that
    # as a distinct, actionable error instead of a raw OSError/500.
    if os.path.isdir(dest_path):
        raise LogPullError("path_conflict", 409, f"destination is an existing directory: {dest_path}")
    try:
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    except OSError as exc:
        raise LogPullError(
            "path_conflict", 409,
            f"a path component of {dest_path} already exists as a file: {exc}",
        )

    base_real = os.path.realpath(base_abs)
    dest_real = os.path.realpath(dest_path)
    if os.path.commonpath([base_real, dest_real]) != base_real:
        raise LogPullError("bad_path", 400, f"resolved path escapes session dir: {dest_path}")
    return dest_path


def _pull_one_file(
    dest_base: str,
    client_id: str,
    root: str,
    path: str,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> int:
    """Pull one file from a branch into dest_base/<client_id>/<root>/<path>.

    Shared chunk-pull core for both the single-file endpoint and the
    pull-all job. Reads in CHUNK-sized steps starting at offset 0 until the
    branch reports eof. Logs can grow while being read; that is fine as long
    as the total stays under MAX_PULL_BYTES. Written to a temp file first and
    swapped in with os.replace so a failed/aborted/cancelled pull never
    leaves a half file at the real path. Returns the number of bytes
    actually written.

    `should_cancel`, when given (only the pull-all job passes one - a plain
    single-file pull_log is never cancellable), is checked once per loop
    iteration *before* the next chunk request and raises _JobCancelled if it
    returns true, so a cancel is noticed between chunks, not just between
    files.

    Looks up the branch's sid itself (rather than taking one from a caller
    who fetched it earlier) so that a branch going offline mid pull-all is
    reported as "branch_offline" for whichever file it happens on.
    """
    sid = _lookup_sid(client_id)
    if sid is None:
        raise LogPullError("branch_offline", 409, f"client '{client_id}' is not connected")

    dest_path = _resolve_dest_path(dest_base, client_id, root, path)
    dest_dir = os.path.dirname(dest_path)

    offset = 0
    total_written = 0
    tmp_path: Optional[str] = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix=".pull-", suffix=".tmp", dir=dest_dir)
        with os.fdopen(fd, "wb") as fh:
            while True:
                if should_cancel is not None and should_cancel():
                    raise _JobCancelled()

                resp = _call_branch(
                    sid, "log_read",
                    {"root": root, "path": path, "offset": offset, "length": CHUNK},
                    timeout=30,
                )
                size = resp.get("size")
                if isinstance(size, (int, float)) and size > MAX_PULL_BYTES:
                    raise LogPullError("too_large", 413, f"'{path}' reports size {size} > {MAX_PULL_BYTES}")

                try:
                    chunk = base64.b64decode(resp.get("data") or "")
                except Exception as exc:
                    raise LogPullError("branch_error", 502, f"bad base64 chunk from branch: {exc}")

                if chunk:
                    fh.write(chunk)
                    offset += len(chunk)
                    total_written += len(chunk)

                if total_written > MAX_PULL_BYTES:
                    raise LogPullError("too_large", 413, f"'{path}' exceeded {MAX_PULL_BYTES} bytes while pulling")

                if resp.get("eof"):
                    break
                if not chunk:
                    # ไม่มี eof แต่ก็ไม่มีข้อมูลเพิ่มมาให้ - กันลูปค้าง
                    raise LogPullError("branch_error", 502, f"'{path}': branch sent an empty chunk without eof")

        os.replace(tmp_path, dest_path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    return total_written


def pull_log(client_id: str, root: str, path: str, session_id: str) -> dict:
    """Pull one file from a branch into an existing viewer session
    (LOGS_BASE_PATH/<session_id>/<client_id>/<root>/<path>).

    Raises "busy" if a pull-all job is currently running for this session,
    and "no_session" if the session dir doesn't exist yet (a single pull can
    no longer conjure a session into existence on its own - it must have
    been created by a pull-all job first)."""
    _validate_session_id(session_id)
    _validate_client_id(client_id)
    _validate_root(root)
    _validate_rel_path(path)
    _check_snapshot_guard(root, path)

    with _jobs_lock:
        job = _jobs.get(session_id)
        job_running = job is not None and job.state == "running"
    if job_running:
        raise LogPullError("busy", 409, f"pull-all already running for session {session_id!r}")

    session_dir = _session_dir_path(session_id)
    if not os.path.isdir(session_dir):
        raise LogPullError("no_session", 404, f"session {session_id!r} does not exist")

    total_written = _pull_one_file(session_dir, client_id, root, path)
    touch_session(session_id)

    return {
        "client_id": client_id,
        "root": root,
        "path": path,
        "size": total_written,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "local_path": f"{session_id}/{client_id}/{root}/{path}",
    }


# ---------------------------------------------------------------------------
# pull-all as a background job: registry, starter, worker body.
# ---------------------------------------------------------------------------

class _Job:
    """In-memory record for one pull-all run, keyed by session_id in _jobs.

    Every read/mutation of a _Job's fields (both from the background worker
    and from GET/DELETE handlers) happens under _jobs_lock, so a poller
    never observes a half-updated snapshot (e.g. done_files bumped but the
    matching files[] entry not appended yet).
    """

    __slots__ = (
        "session_id", "client_id", "state", "error",
        "total_files", "done_files", "total_bytes",
        "list_truncated", "incomplete", "files",
        "cancel_requested", "finished_at",
    )

    def __init__(self, session_id: str, client_id: str):
        self.session_id = session_id
        self.client_id = client_id
        self.state = "running"  # running | done | failed | cancelled
        self.error: Optional[str] = None
        self.total_files: Optional[int] = None  # None until list_remote_logs succeeds
        self.done_files = 0
        self.total_bytes = 0
        self.list_truncated = False
        self.incomplete = False
        self.files: list[dict] = []
        self.cancel_requested = False
        self.finished_at: Optional[float] = None

    def snapshot(self) -> dict:
        """Caller must hold _jobs_lock."""
        d = {
            "session_id": self.session_id,
            "client_id": self.client_id,
            "state": self.state,
            "done_files": self.done_files,
            "total_files": self.total_files,
            "list_truncated": self.list_truncated,
            "incomplete": self.incomplete,
            "total_bytes": self.total_bytes,
            "files": list(self.files),
        }
        if self.error is not None:
            d["error"] = self.error
        return d


# The one lock guarding every read/write of _jobs and of any _Job inside it.
# Under eventlet's monkey patching this is a cooperative ("green") lock.
# Nothing that can block (socketio.call, disk I/O, sleeps) ever runs while
# holding it - only dict/attribute reads and writes.
_jobs: dict[str, "_Job"] = {}
_jobs_lock = threading.Lock()


def _default_run_in_background(fn, *args, **kwargs):
    """Real runtime: hand the job off to Flask-SocketIO's background-task
    runner (an eventlet greenlet under this app's async_mode=eventlet).

    Tests replace `_run_in_background` (module attribute - reassign
    `services.log_pull_service._run_in_background`) with a synchronous or
    thread-based runner instead: the bare `SocketIO()` instance used there
    (no init_app) has `server is None`, so the real
    `socketio.start_background_task` would raise.
    """
    return socketio.start_background_task(fn, *args, **kwargs)


_run_in_background: Callable = _default_run_in_background


def start_pull_all_job(client_id: str, session_id: str) -> dict:
    """Validate a pull-all request, register it as a running job, wipe and
    recreate its on-disk dir, and hand the actual pulling off to
    `_run_in_background`.

    Raises LogPullError (bad_client / bad_session / busy / client_busy /
    disk_limit) for anything that stops the job from being started at all.
    On success the job is already registered as "running" - and the
    <session>/<client_id> dir already wiped+recreated - before this
    returns, so a poller hitting GET /sessions/<session_id> right after the
    202 always finds a live record, never a gap.
    """
    _validate_session_id(session_id)
    _validate_client_id(client_id)
    _sweep_expired_sessions()

    def _check_busy_locked() -> None:
        existing = _jobs.get(session_id)
        if existing is not None and existing.state == "running":
            raise LogPullError("busy", 409, f"pull-all already running for session {session_id!r}")
        for job in _jobs.values():
            if job.state == "running" and job.client_id == client_id:
                raise LogPullError(
                    "client_busy", 409,
                    f"pull-all already running for client {client_id!r} (session {job.session_id!r})",
                )

    with _jobs_lock:
        _check_busy_locked()

    # Disk-cap check: a full walk of LOGS_BASE_PATH. Deliberately done
    # outside _jobs_lock (a filesystem walk is not the kind of quick,
    # non-blocking op that lock is meant to guard).
    total = _total_bytes_under_logs_base()
    if total >= MAX_TOTAL_BYTES:
        raise LogPullError("disk_limit", 507, f"LOGS_BASE_PATH at {total} bytes >= cap {MAX_TOTAL_BYTES}")

    with _jobs_lock:
        _check_busy_locked()  # re-check: the disk walk above released the lock briefly
        job = _Job(session_id, client_id)
        _jobs[session_id] = job

    # From here on the job is already registered (and visible to pollers) as
    # "running" - any failure in this block must undo that registration (and
    # best-effort clean up any half-created dir) before propagating, so a
    # blown os.makedirs/etc never leaves a phantom job stuck "running"
    # forever, nor makes this client look permanently client_busy.
    session_dir = _session_dir_path(session_id)
    try:
        client_dir = os.path.join(session_dir, client_id)
        # Fresh pull: this client's previous copy inside the session (if any) is
        # wiped before we start, so a re-run never mixes stale files with new
        # ones. Other clients' data already in this session dir is untouched.
        _remove_dir_safely(client_dir)
        os.makedirs(client_dir, exist_ok=True)
        touch_session(session_id)

        _run_in_background(_pull_all_job, session_id, client_id)
    except Exception:
        with _jobs_lock:
            if _jobs.get(session_id) is job:
                del _jobs[session_id]
        _remove_dir_safely(session_dir)
        raise

    return {"session_id": session_id, "client_id": client_id, "state": "running"}


def _finalize_pull_all_job(
    job: _Job, session_id: str, session_dir: str, stop_error_code: Optional[str],
) -> None:
    """Decide and set a pull-all job's terminal state once its file loop has
    finished normally (the in-loop / immediately-post-loop cancel checks
    already handle a cancel that was visible by then): "cancelled" if
    job.cancel_requested is (or, within this very call, just became) set,
    "done" otherwise.

    The cancel_requested re-check has to happen *inside* the same
    _jobs_lock acquisition that sets the terminal state - not as a separate
    check-then-lock pair - because delete_session only removes a running
    job's dir itself when it finds the job already non-running; a "running"
    job is trusted to notice cancel_requested and remove its own dir.
    Without one atomic check-and-set here, a cancel_requested flip landing
    in the gap between the caller's last cancellation check and this call
    would be missed entirely: the job would finish "done", nobody would
    ever remove its dir, and the client that asked to cancel would have
    been told {"cancelling": true} for nothing.
    """
    with _jobs_lock:
        if job.cancel_requested:
            job.state = "cancelled"
            job.error = "cancelled"
            job.finished_at = time.time()
            cancelled = True
        else:
            job.incomplete = stop_error_code is not None
            job.state = "done"
            job.finished_at = time.time()
            cancelled = False

    if cancelled:
        _remove_dir_safely(session_dir)
        return
    touch_session(session_id)


def _pull_all_job(session_id: str, client_id: str) -> None:
    """Background task body for one pull-all run - the target handed to
    `_run_in_background` by start_pull_all_job.

    Never raises: every failure mode (including a totally unexpected bug)
    updates the job record's state instead, so a job can never be "lost"
    (stuck on "running" forever with nobody left to report a traceback to).
    """
    with _jobs_lock:
        job = _jobs.get(session_id)
    if job is None or job.client_id != client_id:
        return  # pragma: no cover - defensive: start_pull_all_job always sets this up first

    session_dir = _session_dir_path(session_id)

    def _cancelled() -> bool:
        with _jobs_lock:
            return job.cancel_requested

    try:
        try:
            listing = list_remote_logs(client_id)
        except LogPullError as exc:
            with _jobs_lock:
                job.state = "failed"
                job.error = exc.code
                job.finished_at = time.time()
            _remove_dir_safely(session_dir)
            return

        files_meta = listing.get("files") or []
        with _jobs_lock:
            job.total_files = len(files_meta)
            job.list_truncated = bool(listing.get("truncated"))

        stop_error_code: Optional[str] = None
        budget_exceeded = False

        for meta in files_meta:
            if _cancelled():
                break

            root = meta.get("root")
            path = meta.get("path")
            size = meta.get("size")
            entry: dict = {"root": root, "path": path, "size": size, "mtime": meta.get("mtime")}
            written = 0

            if stop_error_code is not None:
                # A branch_offline/timeout already happened earlier in this
                # loop - everything after it is unreachable too.
                entry["status"] = "error"
                entry["error"] = stop_error_code
            elif budget_exceeded:
                entry["status"] = "skipped"
                entry["error"] = "total_limit"
            elif isinstance(size, (int, float)) and size > MAX_PULL_BYTES:
                entry["status"] = "skipped"
                entry["error"] = "too_large"
            elif isinstance(size, (int, float)) and job.total_bytes + size > MAX_SESSION_BYTES:
                budget_exceeded = True
                entry["status"] = "skipped"
                entry["error"] = "total_limit"
            else:
                try:
                    _validate_root(root)
                    _validate_rel_path(path)
                    _check_snapshot_guard(root, path)
                    written = _pull_one_file(session_dir, client_id, root, path, should_cancel=_cancelled)
                except _JobCancelled:
                    break  # skips the files.append below for this in-flight file
                except LogPullError as exc:
                    entry["status"] = "error"
                    entry["error"] = exc.code
                    if exc.code in ("branch_offline", "timeout"):
                        stop_error_code = exc.code
                except OSError:
                    entry["status"] = "error"
                    entry["error"] = "local_io_error"
                else:
                    entry["status"] = "ok"
                    entry["local_path"] = f"{session_id}/{client_id}/{root}/{path}"

            with _jobs_lock:
                job.files.append(entry)
                job.done_files += 1
                if entry.get("status") == "ok":
                    job.total_bytes += written
            touch_session(session_id)

        if _cancelled():
            with _jobs_lock:
                job.state = "cancelled"
                job.error = "cancelled"
                job.finished_at = time.time()
            _remove_dir_safely(session_dir)
            return

        _finalize_pull_all_job(job, session_id, session_dir, stop_error_code)

    except Exception:
        _logger.exception(
            "[LOGS] pull-all job crashed for session=%r client=%r", session_id, client_id,
        )
        with _jobs_lock:
            job.state = "failed"
            job.error = "internal_error"
            job.finished_at = time.time()


def get_session_status(session_id: str) -> Optional[dict]:
    """Snapshot of a pull-all job for GET /api/logs/sessions/<session_id>.

    Returns None (-> the blueprint answers 404 no_session) when there is
    neither a job record nor an on-disk dir for this id. Returns an "idle"
    snapshot (client_id: None, total_files: None, files: []) when the dir
    survives but no job record does - e.g. after an admin process restart,
    which wipes every in-memory job but leaves already-pulled files on disk.
    """
    if not isinstance(session_id, str) or not SESSION_ID_RE.match(session_id):
        return None

    with _jobs_lock:
        job = _jobs.get(session_id)
        snapshot = job.snapshot() if job is not None else None

    if snapshot is None:
        session_dir = _session_dir_path(session_id)
        if os.path.islink(session_dir) or not os.path.isdir(session_dir):
            return None
        snapshot = {
            "session_id": session_id,
            "client_id": None,
            "state": "idle",
            "done_files": 0,
            "total_files": None,
            "list_truncated": False,
            "incomplete": False,
            "total_bytes": 0,
            "files": [],
        }

    touch_session(session_id)
    return snapshot
