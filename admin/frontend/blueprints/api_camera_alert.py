from __future__ import annotations

import requests
from flask import current_app, jsonify, request

from blueprints import bp
from services.api_client import api_base


@bp.route('/api/camera-alert/<any(health, event, image):feed>', methods=["POST"])
def api_camera_alert_feed(feed: str):
    """Pass a Branch's camera health report, alert event or alert picture to the backend's camera_alert_service.

    gate.py has already checked the Device Key, so the X-Activation-Code header is the
    Branch this really is. It goes on as X-Branch-Code; the body never names the Branch,
    so one Branch cannot report for another. The backend's answer comes back as it gave it.
    """
    code = request.headers.get("X-Activation-Code", "").strip()
    try:
        upstream = requests.post(
            f"{api_base()}/camera-alert-service/{feed}",
            data=request.get_data(),
            headers={
                "Content-Type": request.content_type or "application/json",
                "X-Branch-Code": code,
            },
            timeout=current_app.config["HTTP_TIMEOUT_LONG"],
        )
    except requests.exceptions.RequestException as exc:
        current_app.logger.error(f"Failed to forward camera-alert/{feed}: {exc}")
        return jsonify({"error": "Upstream service unavailable"}), 503
    return current_app.response_class(
        response=upstream.content,
        status=upstream.status_code,
        content_type=upstream.headers.get("Content-Type", "application/json"),
    )
