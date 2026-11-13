from __future__ import annotations

import requests
from flask import current_app, jsonify, request

from blueprints import bp
from services.api_client import api_post_multipart


@bp.route("/api/dummy-data/upload", methods=["POST"])
def api_dummy_data_upload():
    """Pass a whole-branch pipeline zip through to the data service untouched.

    All validation (store_id sanitising, snapshot naming) lives in the backend;
    the frontend is a pass-through pipe and stores nothing itself.
    """
    if "file" not in request.files:
        return jsonify({"error": "No file part"}), 400
    store_id = (request.form.get("store_id") or "").strip()
    if not store_id:
        return jsonify({"error": "Missing 'store_id'"}), 400

    file = request.files["file"]
    try:
        resp = api_post_multipart(
            "/dummy-data/upload",
            data={"store_id": store_id},
            files={"file": (file.filename, file.stream, file.mimetype)},
        )
        try:
            payload = resp.json()
        except ValueError:
            payload = {"error": resp.text[:500]}
        return jsonify(payload), resp.status_code
    except requests.exceptions.RequestException as exc:
        current_app.logger.error(
            f"Failed to forward dummy-data upload for store {store_id}: {exc}"
        )
        return jsonify({"error": "Upstream service unavailable"}), 503
