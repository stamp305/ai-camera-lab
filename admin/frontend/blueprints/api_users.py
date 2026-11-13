# frontend/blueprints/api_users.py
"""User and group management API endpoints."""
from __future__ import annotations

import requests
from flask import current_app, jsonify, request

from blueprints import bp
from services.api_client import api_delete, api_get, api_post


# --- User & Group Management ---------------------------------------------------------
@bp.route("/api/users", methods=["GET"])
def api_get_users():
    """Get all users."""
    try:
        resp = api_get("/users")
        resp.raise_for_status()
        current_app.logger.debug(f"[API] Fetched users: {resp.json()}")
        return jsonify(resp.json())
    except requests.exceptions.RequestException as exc:
        current_app.logger.error(f"Could not connect to external API for users: {exc}")
        return jsonify({"error": "Upstream service unavailable"}), 503


@bp.route("/api/groups", methods=["GET"])
def api_get_groups():
    """Get all group names."""
    try:
        resp = api_get("/users")
        resp.raise_for_status()
        data = resp.json()
        return jsonify(sorted([g["group_name"] for g in data]))
    except requests.exceptions.RequestException as exc:
        current_app.logger.error(f"Could not get groups from API: {exc}")
        return jsonify({"error": "Upstream service unavailable"}), 503


@bp.route("/api/add-user", methods=["POST"])
def api_add_user():
    """Add a new user."""
    data = request.get_json() or {}
    required = ["activation_code", "serial_number", "mac_address", "group_name"]
    if not all(f in data for f in required):
        return jsonify({"error": "Missing required fields"}), 400
    payload = {
        "activation_code": data["activation_code"],
        "serial_number": data["serial_number"],
        "mac_address": data["mac_address"],
        "group_name": data["group_name"],
        "branch_name": data.get("branch_name"),
    }
    try:
        resp = api_post("/users", payload, timeout=current_app.config["HTTP_TIMEOUT_SHORT"])
        # Pass through validation errors
        if resp.status_code == 400:
            return jsonify(resp.json()), 400
        if 500 <= resp.status_code < 600:
            return jsonify({"error": "Upstream user service error"}), 502
        resp.raise_for_status()
        current_app.logger.info(f"Successfully added user {data['activation_code']} via API.")
        return jsonify({"message": "User added successfully!"}), 201
    except requests.exceptions.ConnectTimeout:
        return jsonify({"error": "User management service timeout"}), 504
    except requests.exceptions.ConnectionError:
        return jsonify({"error": "Failed to connect to the user management service."}), 503
    except requests.exceptions.HTTPError as exc:
        try:
            return jsonify(exc.response.json()), exc.response.status_code
        except Exception:
            return jsonify({"error": "Upstream HTTP error"}), 502
    except requests.exceptions.RequestException as exc:
        current_app.logger.error(f"Unhandled error adding user: {exc}")
        return jsonify({"error": "Failed to connect to the user management service."}), 503

from services.api_client import api_put

@bp.route("/api/user/<activation_code>/info", methods=["PUT"])
def api_update_user_info(activation_code: str):
    """Update user's group and branch."""
    data = request.get_json() or {}
    group_name = data.get("group_name")
    branch_name = data.get("branch_name")
    if not group_name:
        return jsonify({"error": "Missing group_name"}), 400
    try:
        resp = api_put(f"/users/{activation_code}/info", {"group_name": group_name, "branch_name": branch_name})
        resp.raise_for_status()
        current_app.logger.info(f"Successfully updated info for user {activation_code} via API.")
        return jsonify({"message": f"User info updated successfully."}), 200
    except requests.exceptions.RequestException as exc:
        current_app.logger.error(f"Failed to update user info via API: {exc}")
        return jsonify({"error": "Failed to connect to the user management service."}), 503


@bp.route("/api/user/<activation_code>", methods=["DELETE"])
def api_delete_user(activation_code: str):
    """Delete a user by activation code."""
    try:
        resp = api_delete(f"/users/{activation_code}")
        resp.raise_for_status()
        current_app.logger.info(f"Successfully deleted user {activation_code} via API.")
        return jsonify({"message": f"User {activation_code} deleted successfully."}), 200
    except requests.exceptions.RequestException as exc:
        current_app.logger.error(f"Failed to delete user via API: {exc}. Cannot fallback for this action.")
        return jsonify({"error": "Failed to connect to the user management service."}), 503


@bp.route("/api/check-activation-code/<code>", methods=["GET"])
def api_check_activation_code(code: str):
    """Check if an activation code already exists."""
    try:
        resp = api_get("/users")
        resp.raise_for_status()
        for group in resp.json():
            for user in group.get("users", []):
                if user.get("activation_code") == code:
                    return jsonify({"exists": True})
        return jsonify({"exists": False})
    except Exception:
        return jsonify({"error": "Upstream service unavailable"}), 503


@bp.route("/api/activation-code", methods=["GET"])
def api_generate_activation_code():
    """Proxy endpoint to fetch a new activation code from backend service."""
    try:
        resp = api_get("/users/activation-code")
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict) or "activation_code" not in data:
            return jsonify({"error": "Malformed upstream response"}), 502
        return jsonify(data)
    except requests.exceptions.RequestException as exc:
        current_app.logger.error(f"Failed to fetch activation code: {exc}")
        return jsonify({"error": "Unable to fetch activation code"}), 503


# --- Version Management --------------------------------------------------------------
from socket_handlers import push_main_update

@bp.route("/api/update-version", methods=["POST"])
def api_update_version():
    """Trigger version update for specified users via Socket.IO."""
    data = request.get_json() or {}
    activation_codes = data.get("activation_code_list", [])
    if not activation_codes:
        return jsonify({"error": "activation_code_list is empty or missing"}), 400
    try:
        # Run socket push on a background thread so the HTTP request returns immediately
        # eventlet or thread works.
        import threading
        t = threading.Thread(target=push_main_update, args=(activation_codes,))
        t.daemon = True
        t.start()
        
        current_app.logger.info(f"Triggered socket version update for {len(activation_codes)} users.")
        return jsonify({"message": f"เริ่มอัพเดตโค้ดสำหรับ {len(activation_codes)} สาขาแล้ว!"}), 200
    except Exception as exc:
        current_app.logger.error(f"Failed to trigger version update: {exc}")
        return jsonify({"error": "Failed to trigger update"}), 500


@bp.route("/api/update-version-all", methods=["POST"])
def api_update_version_all():
    """Trigger version update for all users via Socket.IO."""
    try:
        import threading
        t = threading.Thread(target=push_main_update, args=(["ALL"],))
        t.daemon = True
        t.start()
        
        current_app.logger.info("Triggered socket version update for ALL users.")
        return jsonify({"message": "เริ่มอัพเดตโค้ดสำหรับทุกสาขาแล้ว!"}), 200
    except Exception as exc:
        current_app.logger.error(f"Failed to trigger all-user version update: {exc}")
        return jsonify({"error": "Failed to trigger update"}), 500
