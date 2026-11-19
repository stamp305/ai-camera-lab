# frontend/blueprints/api_live.py
"""Live / playback camera API for the Alitech portal (server to server) and MediaMTX.

    GET  /ai-camera/api/live/branches                 online branches that can relay video
    GET  /ai-camera/api/live/<client_id>/cameras      the channels that branch uses
    POST /ai-camera/api/live/<client_id>/start        {mode, items, start?, end?, viewer}
    POST /ai-camera/api/live/stop                     {session, viewer}
    POST /ai-camera/api/live/mtx-auth                 MediaMTX's auth hook (loopback only)

The portal calls with X-Live-Service-Key: only its server holds that key, never a
browser. It also names the viewer ("alitech:<user id>"), which goes into every audit
entry; the portal is the one that checked the viewer is an Alitech admin.
"""
from __future__ import annotations

import re

from flask import abort, jsonify, request

from blueprints import bp
from services import live_service
from services.live_service import LiveError

_CLIENT_RE = re.compile(r"^[A-Za-z0-9-]{4,40}$")
_VIEWER_RE = re.compile(r"^alitech:[0-9]{1,12}$")
_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
_SID_RE = re.compile(r"^[0-9a-f]{32}$")


def _service_only():
    if not live_service.service_key_ok(request.headers.get("X-Live-Service-Key", "")):
        abort(401)


def _client(client_id: str) -> str:
    if not _CLIENT_RE.match(client_id or ""):
        abort(400)
    return client_id


def _viewer(body: dict) -> str:
    viewer = str(body.get("viewer") or "")
    if not _VIEWER_RE.match(viewer):
        abort(400)
    return viewer


def _fail(exc: LiveError):
    resp = jsonify({"ok": False, "error": exc.code})
    resp.status_code = exc.status
    return resp


@bp.after_request
def _no_store_live(resp):
    if request.path.startswith("/ai-camera/api/live/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.route("/api/live/branches", methods=["GET"])
def live_branches():
    _service_only()
    return jsonify({"ok": True, "branches": [b for b in live_service.online_branches() if b["live"]]})


@bp.route("/api/live/<client_id>/cameras", methods=["GET"])
def live_cameras(client_id):
    _service_only()
    try:
        return jsonify({"ok": True, **live_service.cameras(_client(client_id))})
    except LiveError as exc:
        return _fail(exc)


@bp.route("/api/live/<client_id>/record-days", methods=["GET"])
def live_record_days(client_id):
    """Days of a month on which the branch's NVR holds video for one channel."""
    _service_only()
    month = request.args.get("month", "")
    if not re.match(r"^\d{4}-\d{2}$", month):
        abort(400)
    try:
        camera, channel = int(request.args.get("camera", "")), int(request.args.get("channel", ""))
        days = live_service.record_days(_client(client_id), camera, channel, month)
    except ValueError:
        abort(400)
    except LiveError as exc:
        return _fail(exc)
    return jsonify({"ok": True, "days": days})


@bp.route("/api/live/<client_id>/record-spans", methods=["GET"])
def live_record_spans(client_id):
    """Parts of one day (seconds) on which the branch's NVR holds video for one channel."""
    _service_only()
    day = request.args.get("day", "")
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
        abort(400)
    try:
        camera, channel = int(request.args.get("camera", "")), int(request.args.get("channel", ""))
        spans = live_service.record_spans(_client(client_id), camera, channel, day)
    except ValueError:
        abort(400)
    except LiveError as exc:
        return _fail(exc)
    return jsonify({"ok": True, "spans": spans})


@bp.route("/api/live/<client_id>/siren", methods=["POST"])
def live_siren(client_id):
    """The portal's siren setup page, passed to the branch: {method, path, body, viewer}."""
    _service_only()
    req = request.get_json(silent=True) or {}
    viewer = _viewer(req)
    method, path, body = str(req.get("method") or ""), str(req.get("path") or ""), req.get("body") or {}
    if not isinstance(body, dict) or len(str(body)) > 2000:
        abort(400)
    try:
        out = live_service.siren_call(_client(client_id), method, path, body, viewer)
    except LiveError as exc:
        return _fail(exc)
    return jsonify({"ok": True, **out})


@bp.route("/api/live/<client_id>/start", methods=["POST"])
def live_start(client_id):
    _service_only()
    body = request.get_json(silent=True) or {}
    viewer = _viewer(body)
    mode = body.get("mode")
    start_at = end_at = None
    if mode == "playback":
        start_at, end_at = str(body.get("start") or ""), str(body.get("end") or "")
        if not (_TIME_RE.match(start_at) and _TIME_RE.match(end_at)):
            abort(400)
    try:
        speed = int(body.get("speed", 1))
    except (TypeError, ValueError):
        abort(400)
    try:
        streams = live_service.start(_client(client_id), body.get("items"), mode, viewer, start_at, end_at,
                                     speed=speed, direction=str(body.get("direction") or "fwd"),
                                     quality=str(body.get("quality") or "full"))
    except LiveError as exc:
        return _fail(exc)
    return jsonify({"ok": True, "streams": streams})


@bp.route("/api/live/<client_id>/status", methods=["GET"])
def live_status(client_id):
    """What the branch is relaying now, with ffmpeg's last (scrubbed) error line."""
    _service_only()
    try:
        return jsonify(live_service._call(_client(client_id), "live_status", {}, timeout=5))
    except LiveError as exc:
        return _fail(exc)


@bp.route("/api/live/stop", methods=["POST"])
def live_stop():
    _service_only()
    body = request.get_json(silent=True) or {}
    sid = str(body.get("session") or "")
    if not _SID_RE.match(sid):
        abort(400)
    return jsonify({"ok": True, "stopped": live_service.stop(sid, "viewer_closed", viewer=_viewer(body))})


@bp.route("/api/live/mtx-auth", methods=["POST"])
def live_mtx_auth():
    # The real peer, not client_ip(): that one believes X-Forwarded-For, which any
    # LAN caller could set to 127.0.0.1. MediaMTX calls from loopback, unproxied.
    ip = request.remote_addr or ""
    if ip not in ("127.0.0.1", "::1") or request.headers.get("X-Forwarded-For"):
        abort(403)
    ok = live_service.mtx_auth(request.get_json(silent=True) or {}, ip)
    return ("", 200) if ok else ("", 401)
