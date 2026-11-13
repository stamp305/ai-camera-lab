from __future__ import annotations

import requests
from flask import current_app, jsonify, request

from blueprints import bp
from services.api_client import api_base


@bp.route('/api/traffic/<any(rate, "in-pass", hotzone, "zone-hour"):feed>', methods=["POST"])
def api_traffic_feed(feed: str):
    """Pass one traffic feed through to the backend's ai_camera_service (ADR 0009).

    The body goes on as the Branch sent it, and the backend's status, body and
    Content-Type come back as it gave them, 400 and 500 included. These feeds
    exist to end globalapi2's 200 that wrote nothing, so nothing here may turn a
    failure into a 200. gate.py has already checked the Device Key.
    """
    try:
        upstream = requests.post(
            f"{api_base()}/ai-camera-service/traffic/{feed}",
            data=request.get_data(),
            headers={"Content-Type": request.content_type or "application/json"},
            timeout=current_app.config["HTTP_TIMEOUT_LONG"],
        )
    except requests.exceptions.RequestException as exc:
        current_app.logger.error(f"Failed to forward traffic/{feed}: {exc}")
        return jsonify({"error": "Upstream service unavailable"}), 503
    return current_app.response_class(
        response=upstream.content,
        status=upstream.status_code,
        content_type=upstream.headers.get("Content-Type", "application/json"),
    )
