# frontend/blueprints/api_logs.py
"""Pull-logs API: list connected branches, run pull-all as a pollable
background job, and serve/delete a viewer session's pulled copy.

Nothing is kept permanently. An admin's browser mints a session_id and picks
a branch; pull-all starts a background job that sweeps every log file for
that branch into LOGS_BASE_PATH/<session_id>/<client_id>/<root>/<path>, the
browser polls GET /sessions/<session_id> until it finishes, and the admin
(or the next housekeeping sweep) deletes the session when done viewing.
"""
from __future__ import annotations

import io
import os

from flask import abort, current_app, jsonify, request, send_file

from blueprints import bp
from config import Config
from services import audit_log, log_unseal
from services.log_pull_service import (
    SESSION_ID_RE,
    LogPullError,
    delete_session,
    get_session_status,
    list_remote_logs,
    online_branches,
    pull_log,
    start_pull_all_job,
    touch_session,
)

# Cap on ?max_bytes= for /api/logs/content - keeps a single request from
# forcing the admin to read an unbounded slice of a (possibly huge) log.
_MAX_CONTENT_BYTES = 10 * 1024 * 1024  # 10485760

# ข้อความไทยของแต่ละ error code - ครอบคลุมทั้ง error ของเราเอง (bad_client,
# branch_offline, timeout, too_large, ...) และ error ที่สาขาส่งกลับมาตรงๆ
# ตาม log_read protocol (bad_root/bad_path/not_found/io_error)
_THAI_MESSAGES = {
    "bad_request": "คำขอไม่ถูกต้อง",
    "bad_client": "รหัสสาขาไม่ถูกต้อง",
    "bad_session": "รหัสรอบการดูไม่ถูกต้อง",
    "bad_root": "หมวดไฟล์บันทึกไม่ถูกต้อง",
    "bad_path": "พาธไฟล์ไม่ถูกต้องหรือไม่ปลอดภัย",
    "not_found": "ไม่พบไฟล์นี้บนสาขา",
    "io_error": "สาขาอ่านไฟล์นี้ไม่สำเร็จ",
    "file_changed": "ไฟล์บนสาขาเปลี่ยนระหว่างดึง (อาจถูกหมุนไฟล์) ลองดึงใหม่อีกครั้ง",
    "path_conflict": "บันทึกไม่ได้ เพราะชื่อไฟล์ชนกับโฟลเดอร์ที่ดึงมาก่อนหน้า",
    "branch_offline": "สาขานี้ไม่ได้เชื่อมต่ออยู่ในขณะนี้",
    "timeout": "สาขาไม่ตอบสนองภายในเวลาที่กำหนด",
    "too_large": "ไฟล์มีขนาดใหญ่เกินกำหนด (เกิน 50MB)",
    "total_limit": "เกินขนาดรวม 300 MB ของรอบนี้ ข้ามไฟล์ที่เหลือ",
    "busy": "กำลังดึงอยู่ รอสักครู่",
    "client_busy": "สาขานี้กำลังถูกดึง log อยู่ ลองใหม่อีกสักครู่",
    "disk_limit": "พื้นที่เก็บ log ชั่วคราวเต็ม ลองใหม่ภายหลัง",
    "no_session": "ไม่พบรอบการดูนี้",
    "local_io_error": "บันทึกไฟล์บนเครื่องแอดมินไม่สำเร็จ",
    "cancelled": "ยกเลิกการดึงแล้ว",
    "branch_error": "เกิดข้อผิดพลาดที่สาขาไม่ทราบสาเหตุ",
    "internal_error": "เกิดข้อผิดพลาดภายในระบบ",
    "key_not_configured": "เซิร์ฟเวอร์ยังไม่ได้ตั้งกุญแจถอดรหัส log (AICAM_LOG_SEAL_KEY)",
    "key_unavailable": "เซิร์ฟเวอร์เปิดกุญแจถอดรหัส log ไม่ได้",
    "key_unsafe": "ไฟล์กุญแจถอดรหัส log เปิดให้คนอื่นอ่านได้ เซิร์ฟเวอร์จึงไม่ใช้ (ต้องเป็นสิทธิ์ 600)",
}


def _thai_message(code: str) -> str:
    return _THAI_MESSAGES.get(code, f"เกิดข้อผิดพลาด ({code})")


def _error_response(exc: LogPullError):
    return jsonify({"error": exc.code, "message": _thai_message(exc.code)}), exc.http_status


def _bad_request(code: str = "bad_request"):
    return jsonify({"error": code, "message": _thai_message(code)}), 400


@bp.route("/api/logs/branches", methods=["GET"])
def api_logs_branches():
    """List branches currently connected over Socket.IO.

    Also runs the viewer-session housekeeping sweep, throttled to at most
    once per minute (see services.log_pull_service._maybe_sweep_expired_sessions) -
    this endpoint is polled far more often than pull-all is called.
    """
    return jsonify({"branches": online_branches()})


@bp.route("/api/logs/branches/<client_id>/remote", methods=["GET"])
def api_logs_branch_remote(client_id: str):
    """Ask a connected branch for its log file listing."""
    try:
        result = list_remote_logs(client_id)
        audit_log.record("log_list", client_id)
        return jsonify(result), 200
    except LogPullError as exc:
        audit_log.record("log_list", client_id, result=exc.code)
        return _error_response(exc)
    except Exception as exc:  # pragma: no cover - defensive fallback
        current_app.logger.error(f"[LOGS] Unexpected error listing remote logs for {client_id}: {exc}")
        return jsonify({"error": "internal_error", "message": _thai_message("internal_error")}), 500


@bp.route("/api/logs/branches/<client_id>/pull-all", methods=["POST"])
def api_logs_branch_pull_all(client_id: str):
    """Start a pull-all background job for every log file a branch
    currently reports, into the caller-supplied viewer session. Returns 202
    immediately; poll GET /api/logs/sessions/<session_id> for progress."""
    if not request.is_json:
        return _bad_request()
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _bad_request()

    session_id = data.get("session_id")
    if not isinstance(session_id, str) or not SESSION_ID_RE.match(session_id):
        return _bad_request("bad_session")

    try:
        result = start_pull_all_job(client_id, session_id)
        audit_log.record("log_pull_all", client_id)
        return jsonify(result), 202
    except LogPullError as exc:
        audit_log.record("log_pull_all", client_id, result=exc.code)
        return _error_response(exc)
    except OSError as exc:
        current_app.logger.error(f"[LOGS] Local I/O error starting pull-all for {client_id}: {exc}")
        return jsonify({"error": "local_io_error", "message": _thai_message("local_io_error")}), 500
    except Exception as exc:  # pragma: no cover - defensive fallback
        current_app.logger.error(f"[LOGS] Unexpected error starting pull-all for {client_id}: {exc}")
        return jsonify({"error": "internal_error", "message": _thai_message("internal_error")}), 500


@bp.route("/api/logs/sessions/<session_id>", methods=["GET"])
def api_logs_session_status(session_id: str):
    """Poll a pull-all job's progress/result.

    "idle" means the session dir survives but no job record does (e.g. the
    admin process restarted) - files come back as an empty list in that
    case, since which client_id(s) it holds is no longer tracked in memory.
    """
    status = get_session_status(session_id)
    if status is None:
        return jsonify({"error": "no_session", "message": _thai_message("no_session")}), 404
    error = status.get("error")
    if error is not None:
        status = dict(status)
        status["message"] = _thai_message(error)
    return jsonify(status), 200


@bp.route("/api/logs/branches/<client_id>/pull", methods=["POST"])
def api_logs_branch_pull(client_id: str):
    """Pull one log file from a connected branch into an existing viewer session."""
    if not request.is_json:
        return _bad_request()
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _bad_request()

    session_id = data.get("session_id")
    root = data.get("root")
    path = data.get("path")
    if not isinstance(session_id, str) or not isinstance(root, str) or not isinstance(path, str):
        return _bad_request()

    try:
        result = pull_log(client_id, root, path, session_id)
        audit_log.record("log_pull", client_id, f"{root}/{path}")
        return jsonify(result), 200
    except LogPullError as exc:
        audit_log.record("log_pull", client_id, f"{root}/{path}", result=exc.code)
        return _error_response(exc)
    except OSError as exc:
        current_app.logger.error(f"[LOGS] Local I/O error pulling {client_id}/{root}/{path}: {exc}")
        return jsonify({"error": "local_io_error", "message": _thai_message("local_io_error")}), 500
    except Exception as exc:  # pragma: no cover - defensive fallback
        current_app.logger.error(f"[LOGS] Unexpected error pulling {client_id}/{root}/{path}: {exc}")
        return jsonify({"error": "internal_error", "message": _thai_message("internal_error")}), 500


def _handle_delete_session(session_id: str):
    try:
        result = delete_session(session_id)
        return jsonify(result), 200
    except LogPullError as exc:
        return _error_response(exc)
    except Exception as exc:  # pragma: no cover - defensive fallback
        current_app.logger.error(f"[LOGS] Unexpected error deleting session {session_id}: {exc}")
        return jsonify({"error": "internal_error", "message": _thai_message("internal_error")}), 500


@bp.route("/api/logs/sessions/<session_id>", methods=["DELETE"])
def api_logs_session_delete(session_id: str):
    """Delete a viewer session, or cancel it first if a pull-all job is
    still running for it (the job removes the dir itself once cancelled)."""
    return _handle_delete_session(session_id)


@bp.route("/api/logs/sessions/<session_id>/delete", methods=["POST"])
def api_logs_session_delete_beacon(session_id: str):
    """Same as the DELETE route above, reachable via navigator.sendBeacon
    (which can only POST). Accepts any body, including none/empty - it is
    never read."""
    return _handle_delete_session(session_id)


@bp.route("/api/logs/content/<path:local_path>", methods=["GET"])
def api_logs_content(local_path: str):
    """Serve a pulled log file from within its viewer session: either the
    raw file (download=1) or its text content, optionally tailed to the
    last ?max_bytes= bytes."""
    segments = local_path.split("/")
    if any(seg in ("", ".", "..") for seg in segments):
        current_app.logger.warning(f"[LOGS] Blocked content request with unsafe segment: {local_path}")
        abort(404)

    session_id = segments[0]
    if not SESSION_ID_RE.match(session_id):
        current_app.logger.warning(f"[LOGS] Blocked content request with non-session prefix: {local_path}")
        abort(404)

    base_real = os.path.realpath(Config.LOGS_BASE_PATH)
    full = os.path.normpath(os.path.join(Config.LOGS_BASE_PATH, local_path))
    full_real = os.path.realpath(full)

    try:
        inside = os.path.commonpath([base_real, full_real]) == base_real
    except ValueError:
        inside = False  # e.g. different drives - definitely not inside

    if not inside or os.path.islink(full) or not os.path.isfile(full_real):
        current_app.logger.warning(f"[LOGS] Blocked or missing content request: {local_path}")
        abort(404)

    # <session_id>/<client_id>/<root>/<path>: who and what, for the audit entry.
    client_id = segments[1] if len(segments) > 1 else None
    target = "/".join(segments[2:]) or None
    download = request.args.get("download") == "1"
    action = "log_download" if download else "log_view"

    max_bytes_raw = request.args.get("max_bytes")
    max_bytes = None
    if max_bytes_raw is not None:
        try:
            max_bytes = int(max_bytes_raw)
        except (TypeError, ValueError):
            max_bytes = None
        if max_bytes is None or not (1 <= max_bytes <= _MAX_CONTENT_BYTES):
            return _bad_request()

    # The whole file is read: a sealed record needs its key line, which can be
    # anywhere above it, so a tail is cut from the opened text, not the file.
    # Pulled files are capped at 50 MB each (log_pull_service).
    try:
        with open(full_real, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        current_app.logger.error(f"[LOGS] Failed reading {full_real}: {exc}")
        abort(404)

    sealed = log_unseal.is_sealed(raw[:16])
    if sealed:
        # Opened in memory for this response only; the copy on disk stays sealed.
        try:
            raw = log_unseal.unseal(raw)
        except log_unseal.UnsealError as exc:
            current_app.logger.error(f"[LOGS] Cannot open sealed logs: {exc}")
            audit_log.record(action, client_id, target, result=exc.code)
            return jsonify({"error": exc.code, "message": _thai_message(exc.code)}), 503
    # Recorded before anything is sent: an access that cannot be logged fails.
    audit_log.record(action, client_id, target, sealed=sealed)

    if download:
        resp = send_file(
            io.BytesIO(raw),
            mimetype="text/plain",
            as_attachment=True,
            download_name=os.path.basename(full_real),
        )
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Cache-Control"] = "no-store"
        touch_session(session_id)
        return resp

    total_size = len(raw)
    truncated = max_bytes is not None and total_size > max_bytes
    if truncated:
        raw = raw[-max_bytes:]

    if truncated:
        # Drop the (likely partial) first line so the view starts clean on a
        # whole-line boundary; if there is no newline in this tail at all,
        # fall back to showing it as-is.
        newline_at = raw.find(b"\n")
        if newline_at != -1:
            raw = raw[newline_at + 1:]

    resp = current_app.response_class(
        response=raw.decode("utf-8", errors="replace"), status=200, mimetype="text/plain",
    )
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Log-Size"] = str(total_size)
    resp.headers["X-Log-Truncated"] = "1" if truncated else "0"
    touch_session(session_id)
    return resp
