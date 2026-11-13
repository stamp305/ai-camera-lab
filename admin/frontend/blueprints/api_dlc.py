# frontend/blueprints/api_dlc.py
"""DLC-related API endpoints."""
from __future__ import annotations

import requests
from flask import current_app, jsonify

from blueprints import bp
from services.api_client import api_get
from services.dlc_service import (
    dlc_jobs,
    dlc_jobs_lock,
    update_clients,
    update_clients_lock,
)
from extensions import socketio


def broadcast_check_for_updates():
    """Broadcast check_for_updates to all connected clients."""
    with update_clients_lock:
        sids = [meta["sid"] for meta in update_clients.values()]
    for sid in sids:
        socketio.emit("check_for_updates", {"command": "check_for_updates"}, to=sid)


@bp.route("/api/broadcast-dlc-update", methods=["POST"])
def api_broadcast_dlc_update():
    """Trigger a DLC diff cycle for all connected clients.

    Simply emits check_for_updates; actual diffing occurs when each client sends its manifest.
    """
    broadcast_check_for_updates()
    return jsonify({"status": "queued", "clients": len(update_clients)})


@bp.route("/api/dlc-job/<job_id>/status", methods=["GET"])
def api_get_dlc_job_status(job_id: str):
    """Get the status of a DLC distribution job."""
    with dlc_jobs_lock:
        job = dlc_jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "job_id": job_id,
        "status": job.get("status"),
        "activation_code": job.get("activation_code"),
        "requested_folders": job.get("requested_folders"),
        "started": job.get("started"),
        "finished": job.get("finished"),
        "error": job.get("error"),
        "total_files": job.get("total_files"),
        "processed_files": job.get("processed_files"),
        "current_folder": job.get("current_folder"),
        "current_file": job.get("current_file"),
    }), 200


@bp.route("/api/dlc-folders", methods=["GET"])
def api_get_dlc_folders():
    """Fetch DLC folders directly from backend service."""
    try:
        resp = api_get("/dlc-folders")
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            return jsonify({"error": "Invalid backend response"}), 502
        return jsonify(sorted(map(str, data)))
    except requests.exceptions.RequestException as exc:
        current_app.logger.error(f"Failed to fetch dlc folders from backend: {exc}")
        return jsonify({"error": "Upstream service unavailable"}), 503


@bp.route("/api/auth/activate", methods=["POST"])
def api_redirect_activate():
    """Proxy activation request to backend auth service."""
    from flask import request
    from services.api_client import api_post
    
    payload = request.get_json(silent=True) or {}
    try:
        upstream = api_post("/auth/activate", payload)
        content_type = upstream.headers.get("Content-Type", "")
        if "application/json" in content_type.lower():
            try:
                return jsonify(upstream.json()), upstream.status_code
            except ValueError:
                pass
        return current_app.response_class(
            response=upstream.content,
            status=upstream.status_code,
            mimetype=content_type or "application/octet-stream",
        )
    except requests.exceptions.RequestException as exc:
        current_app.logger.error(f"Failed to reach auth activation endpoint: {exc}")
        return jsonify({"error": "Activation service unavailable"}), 503
