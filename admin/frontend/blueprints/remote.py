# frontend/blueprints/remote.py
"""Remote browser — open a branch's web UI from the admin.

    admin page   /ai-camera/remote                       (this file)
    open         POST /ai-camera/api/remote/<client_id>/open
                 -> emit "open_remote" to the branch over its Socket.IO link
                 -> branch says hello with the token on that same link (TLS)
                 -> answer: a one-use enter URL on the remote origin
    branch UI    <remote origin>/remote/<client_id>/...  (remote_origin.py)

The branch's pages never load from this origin: see remote_origin.py for why.
Every open, close and failure goes to the audit log (services/audit_log).
"""
from __future__ import annotations

import time
from urllib.parse import urlsplit

from flask import jsonify, render_template, request

from blueprints import bp
from config import Config
from extensions import socketio
from services import audit_log, remote_tunnel
from services.dlc_service import update_clients, update_clients_lock

# How long to wait for the branch to say hello after "open_remote".
_CONNECT_WAIT = 8.0


def _remote_origin() -> str:
    if Config.REMOTE_ORIGIN_URL:
        return Config.REMOTE_ORIGIN_URL
    host = urlsplit(request.host_url).hostname or "localhost"
    return f"{request.scheme}://{host}:{Config.REMOTE_ORIGIN_PORT}"


def _enter_url(client_id: str) -> str:
    ticket = remote_tunnel.mint_enter_ticket(client_id)
    return f"{_remote_origin()}/remote/{client_id}/__enter?t={ticket}"


@bp.route("/remote")
def remote_page():
    """Pick an online branch and work in its web UI on this page."""
    return render_template("remote.html")


@bp.route("/api/remote/sessions")
def remote_sessions():
    """Branches with a remote session open (the red dot in the branch list)."""
    return jsonify(remote_tunnel.active_sessions())


@bp.route("/api/remote/capable")
def remote_capable():
    """Branches whose version can be opened remotely."""
    with update_clients_lock:
        return jsonify([cid for cid, entry in update_clients.items()
                        if entry.get("remote_capable")])


@bp.route("/api/remote/<client_id>/open", methods=["POST"])
def remote_open(client_id: str):
    with update_clients_lock:
        entry = update_clients.get(client_id)
    if not entry:
        audit_log.record("remote_open", client_id, result="offline")
        return jsonify({"status": False, "message": "เครื่องนี้ไม่ได้ออนไลน์อยู่"}), 404
    if not entry.get("remote_capable"):
        audit_log.record("remote_open", client_id, result="version")
        return jsonify({"status": False,
                        "message": "สาขานี้ยังใช้เวอร์ชันที่ยังไม่รองรับรีโมต ต้องอัปเดตก่อน"}), 409

    # A live session on the branch's current connection is reused: opening anew
    # makes the branch drop the old tunnel, cutting a camera stream mid-view.
    # (A session bound to an older sid is dead; that one is opened anew.)
    current = remote_tunnel.session_status(client_id)
    if current.get("active") and remote_tunnel.session_sid(client_id) == entry["sid"]:
        audit_log.record("remote_open", client_id, result="reused")
        return jsonify({"status": True, "url": _enter_url(client_id),
                        "expires_in": current.get("expires_in"), "reused": True})

    try:
        payload = remote_tunnel.mint_token(client_id)
    except remote_tunnel.SessionLimitReached as exc:
        audit_log.record("remote_open", client_id, result="limit")
        return jsonify({"status": False, "message": str(exc)}), 429

    socketio.emit("open_remote", payload, to=entry["sid"])

    deadline = time.time() + _CONNECT_WAIT
    while time.time() < deadline:
        status = remote_tunnel.session_status(client_id)
        if status.get("active"):
            audit_log.record("remote_open", client_id,
                             branch=(entry.get("info") or {}).get("branchName"))
            return jsonify({"status": True, "url": _enter_url(client_id),
                            "expires_in": status.get("expires_in")})
        time.sleep(0.3)

    audit_log.record("remote_open", client_id, result="timeout")
    return jsonify({"status": False, "message": "สาขาไม่ได้เชื่อมต่อกลับมาในเวลาที่กำหนด"}), 504


@bp.route("/api/remote/<client_id>/close", methods=["POST"])
def remote_close(client_id: str):
    """Tell both sides, in case one of them is stuck."""
    with update_clients_lock:
        entry = update_clients.get(client_id)
    if entry:
        socketio.emit("close_remote", {}, to=entry["sid"])
    remote_tunnel.close_session(client_id)
    audit_log.record("remote_close", client_id)
    return jsonify({"status": True})


@bp.route("/api/remote/<client_id>/status")
def remote_status(client_id: str):
    return jsonify(remote_tunnel.session_status(client_id))
