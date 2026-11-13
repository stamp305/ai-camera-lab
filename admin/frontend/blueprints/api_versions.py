# frontend/blueprints/api_versions.py
"""Version manager API endpoints (Git multi-version DLC)."""
from __future__ import annotations

import requests
from flask import current_app, jsonify, request

from blueprints import bp
from services.api_client import api_delete, api_get, api_patch, api_post


@bp.route("/api/branches", methods=["GET"])
def api_get_branches():
    """Get all branches from the backend."""
    try:
        resp = api_get("/branches")
        resp.raise_for_status()
        return jsonify(resp.json())
    except requests.exceptions.RequestException as exc:
        current_app.logger.error(f"Failed to fetch branches: {exc}")
        return jsonify({"error": "Upstream service unavailable"}), 503


@bp.route("/api/branches", methods=["POST"])
def api_create_branch():
    """Register a branch manually."""
    data = request.get_json(silent=True) or {}
    code = (data.get("activation_code") or "").strip()
    if not code:
        return jsonify({"error": "Missing 'activation_code'"}), 400
    try:
        resp = api_post("/branches", {
            "activation_code": code,
            "branch_name": (data.get("branch_name") or "").strip() or None,
        })
        if resp.status_code == 409:
            return jsonify(resp.json()), 409
        resp.raise_for_status()
        return jsonify(resp.json())
    except requests.exceptions.RequestException as exc:
        current_app.logger.error(f"Failed to register branch {code}: {exc}")
        return jsonify({"error": "Failed to connect to the version management service."}), 503


@bp.route("/api/branches/<activation_code>", methods=["DELETE"])
def api_delete_branch(activation_code: str):
    """Delete a branch."""
    try:
        resp = api_delete(f"/branches/{activation_code}")
        if resp.status_code == 404:
            return jsonify(resp.json()), 404
        resp.raise_for_status()
        return jsonify(resp.json())
    except requests.exceptions.RequestException as exc:
        current_app.logger.error(f"Failed to delete branch {activation_code}: {exc}")
        return jsonify({"error": "Failed to connect to the version management service."}), 503


@bp.route("/api/bitbucket/branches", methods=["GET"])
def api_bitbucket_branches():
    """Get remote branches of the Bitbucket DLC repo."""
    try:
        resp = api_get("/branches/bitbucket/branches", timeout=current_app.config["HTTP_TIMEOUT_LONG"])
        resp.raise_for_status()
        return jsonify(resp.json())
    except requests.exceptions.RequestException as exc:
        current_app.logger.error(f"Failed to fetch bitbucket branches: {exc}")
        return jsonify({"error": "Failed to connect to the version management service."}), 503


@bp.route("/api/bitbucket/commits", methods=["GET"])
def api_bitbucket_commits():
    """Get commits of a Bitbucket branch for the DLC repo."""
    branch = request.args.get("branch")
    limit = request.args.get("limit", default="", type=str)
    try:
        path = "/branches/bitbucket/commits"
        query = []
        if branch:
            query.append(f"branch={branch}")
        if limit:
            query.append(f"limit={limit}")
        if query:
            path += "?" + "&".join(query)
        resp = api_get(path, timeout=current_app.config["HTTP_TIMEOUT_LONG"])
        resp.raise_for_status()
        return jsonify(resp.json())
    except requests.exceptions.RequestException as exc:
        current_app.logger.error(f"Failed to fetch bitbucket commits: {exc}")
        return jsonify({"error": "Failed to connect to the version management service."}), 503


@bp.route("/api/bitbucket/store", methods=["POST"])
def api_bitbucket_store():
    """Store chosen Bitbucket commits as local DLC versions."""
    data = request.get_json(silent=True) or {}
    items = data.get("items")
    if not items:
        return jsonify({"error": "Missing 'items'"}), 400
    try:
        resp = api_post("/branches/bitbucket/store", {"items": items},
                        timeout=current_app.config["HTTP_TIMEOUT_STORE"])
        if resp.status_code >= 400:
            detail = ""
            try:
                body = resp.json() or {}
                detail = body.get("detail") or body.get("error") or ""
            except Exception:
                pass
            return jsonify({"error": detail or "Failed to store versions."}), resp.status_code
        resp.raise_for_status()
        return jsonify(resp.json())
    except requests.exceptions.RequestException as exc:
        current_app.logger.error(f"Failed to store bitbucket versions: {exc}")
        return jsonify({"error": "Failed to store versions."}), 503


@bp.route("/api/bitbucket/version/<version_id>", methods=["DELETE"])
def api_bitbucket_delete_version(version_id: str):
    """Delete a stored version."""
    try:
        resp = api_delete(f"/branches/bitbucket/version/{version_id}")
        resp.raise_for_status()
        return jsonify(resp.json())
    except requests.exceptions.RequestException as exc:
        current_app.logger.error(f"Failed to delete version {version_id}: {exc}")
        return jsonify({"error": "Failed to delete version."}), 503


@bp.route("/api/bitbucket/version/<version_id>", methods=["PATCH"])
def api_bitbucket_rename_version(version_id: str):
    """Rename a stored version."""
    data = request.get_json(silent=True) or {}
    new_id = (data.get("new_id") or "").strip()
    if not new_id:
        return jsonify({"error": "Missing 'new_id'"}), 400
    try:
        resp = api_patch(f"/branches/bitbucket/version/{version_id}", {"new_id": new_id})
        if resp.status_code == 400:
            return jsonify(resp.json()), 400
        resp.raise_for_status()
        return jsonify(resp.json())
    except requests.exceptions.RequestException as exc:
        current_app.logger.error(f"Failed to rename version {version_id}: {exc}")
        return jsonify({"error": "Failed to rename version."}), 503


@bp.route("/api/bitbucket/version/<version_id>/rebuild", methods=["POST"])
def api_bitbucket_rebuild_version(version_id: str):
    """Re-publish a stored version to the build farm (remote Mac builders)."""
    try:
        resp = api_post(f"/branches/bitbucket/version/{version_id}/rebuild", {}, timeout=120)
        if resp.status_code >= 400:
            detail = ""
            try:
                body = resp.json() or {}
                detail = body.get("detail") or body.get("error") or ""
            except Exception:
                pass
            return jsonify({"error": detail or "Failed to request rebuild."}), resp.status_code
        resp.raise_for_status()
        return jsonify(resp.json())
    except requests.exceptions.RequestException as exc:
        current_app.logger.error(f"Failed to rebuild version {version_id}: {exc}")
        return jsonify({"error": "Failed to request rebuild."}), 503


@bp.route("/api/bitbucket/version/<version_id>/mode", methods=["POST"])
def api_bitbucket_version_mode(version_id: str):
    """Mock admin: ship a stored version as source, or order its .so build."""
    mode = (request.get_json(silent=True) or {}).get("mode")
    if mode not in ("so", "source"):
        return jsonify({"error": "mode must be 'so' or 'source'"}), 400
    try:
        # Extracting a commit, or reaching the builder, takes a while.
        resp = api_post(f"/branches/bitbucket/version/{version_id}/mode", {"mode": mode},
                        timeout=current_app.config["HTTP_TIMEOUT_STORE"])
        if resp.status_code >= 400:
            detail = ""
            try:
                body = resp.json() or {}
                detail = body.get("detail") or body.get("error") or ""
            except Exception:
                pass
            return jsonify({"error": detail or "Failed to change delivery mode."}), resp.status_code
        return jsonify(resp.json())
    except requests.exceptions.RequestException as exc:
        current_app.logger.error(f"Failed to set mode of {version_id}: {exc}")
        return jsonify({"error": "Failed to change delivery mode."}), 503


@bp.route("/api/bitbucket/version/<version_id>/cancel-build", methods=["POST"])
def api_bitbucket_cancel_build(version_id: str):
    """Cancel a pending/in-progress build job for a version."""
    try:
        resp = api_post(f"/build-farm/jobs/cancel-by-version/{version_id}", {}, timeout=30)
        if resp.status_code >= 400:
            detail = ""
            try:
                body = resp.json() or {}
                detail = body.get("detail") or body.get("error") or ""
            except Exception:
                pass
            return jsonify({"error": detail or "Failed to cancel build."}), resp.status_code
        resp.raise_for_status()
        return jsonify(resp.json())
    except requests.exceptions.RequestException as exc:
        current_app.logger.error(f"Failed to cancel build for {version_id}: {exc}")
        return jsonify({"error": "Failed to cancel build."}), 503


@bp.route("/api/managed-versions", methods=["GET"])
def api_get_dlc_versions():
    """Get available Git versions (tags + commits) from the backend."""
    try:
        resp = api_get("/branches/git-versions")
        resp.raise_for_status()
        data = resp.json()
        # One call for stage + message + percent. The page polls this twice a
        # second while a build runs, so the three separate reads it used to do
        # were three chances to show a stage and a message from different
        # moments of the same build.
        try:
            prog_resp = api_get("/build-farm/build-progress")
            prog_resp.raise_for_status()
            progress = prog_resp.json()
        except Exception:
            progress = {}
        for v in data:
            entry = progress.get(v.get("id", "")) or {}
            v["build_stage"] = entry.get("stage", "none")
            v["build_message"] = entry.get("message", "")
            v["build_percent"] = entry.get("percent")
        return jsonify(data)
    except requests.exceptions.RequestException as exc:
        current_app.logger.error(f"Failed to fetch git versions: {exc}")
        return jsonify({"error": "Upstream service unavailable"}), 503


@bp.route("/api/branches/<activation_code>/assign-version", methods=["POST"])
def api_assign_branch_version(activation_code: str):
    """Assign a Git version to a branch."""
    data = request.get_json(silent=True) or {}
    version = data.get("version")
    if not version:
        return jsonify({"error": "Missing 'version'"}), 400
    try:
        resp = api_patch(f"/branches/{activation_code}/version", {"version": version})
        # 400 carries the reason the backend refused — an unknown version id,
        # typically. Reporting it as "cannot connect" sent people looking at
        # the wrong thing entirely.
        if resp.status_code in (400, 404):
            return jsonify({"error": resp.json().get("detail", "")}), resp.status_code
        resp.raise_for_status()
        return jsonify(resp.json())
    except requests.exceptions.RequestException as exc:
        current_app.logger.error(f"Failed to assign version to {activation_code}: {exc}")
        return jsonify({"error": "Failed to connect to the version management service."}), 503


@bp.route("/api/branches/<activation_code>/force-update", methods=["POST"])
def api_force_branch_update(activation_code: str):
    """Force an immediate Git version push to a branch."""
    try:
        resp = api_post(f"/branches/{activation_code}/force-update", timeout=current_app.config["HTTP_TIMEOUT_STORE"])
        resp.raise_for_status()
        return jsonify(resp.json())
    except requests.exceptions.RequestException as exc:
        current_app.logger.error(f"Failed to force update {activation_code}: {exc}")
        return jsonify({"error": "Failed to trigger the update."}), 503

import os
from werkzeug.utils import secure_filename

@bp.route("/api/build/upload", methods=["POST"])
def api_build_upload():
    """Receive a zipped client app from the build farm."""
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400
    file = request.files['file']
    version_id = request.form.get("version")
    if not version_id:
        return jsonify({"error": "Missing version"}), 400
        
    try:
        from config import Config
        cache_dir = Config.MANAGED_SERVICES_VERSIONS_CACHE
    except AttributeError:
        return jsonify({"error": "Configuration missing MANAGED_SERVICES_VERSIONS_CACHE"}), 500

    target_dir = os.path.join(cache_dir, version_id)
    
    import shutil
    import tarfile
    import tempfile
    
    if os.path.exists(target_dir):
        shutil.rmtree(target_dir, ignore_errors=True)
    os.makedirs(target_dir, exist_ok=True)
    
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".tar.gz")
    try:
        file.save(tmp.name)
        with tarfile.open(tmp.name, mode="r:gz") as tar:
            tar.extractall(target_dir)
            
        current_app.logger.info(f"Build payload extracted successfully to {target_dir}")
        return jsonify({"message": "Upload and extraction successful", "path": target_dir})
    except Exception as e:
        current_app.logger.error(f"Failed to extract payload: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass


@bp.route("/api/branches/<activation_code>/trigger-build", methods=["POST"])
def api_trigger_build(activation_code: str):
    """Trigger the client to run build_mac_app.sh and upload the zip back."""
    from services.managed_service import update_clients, update_clients_lock
    from extensions import socketio
    
    # 1. Find the client's socket session ID
    sid = None
    with update_clients_lock:
        meta = update_clients.get(activation_code)
        if meta and "sid" in meta:
            sid = meta["sid"]
            
    if not sid:
        return jsonify({"error": "Client is offline or not found."}), 404
        
    # 2. Tell the client to build and where to upload the result
    # We provide the upload URL that points to api_build_upload above.
    upload_url = f"{current_app.config.get('FRONTEND_BASE_URL', 'http://localhost:8002')}/ai-camera/api/build/upload"

    socketio.emit("trigger_build", {"upload_url": upload_url}, to=sid)
    
    current_app.logger.info(f"Triggered build on branch {activation_code}")
    return jsonify({"success": True, "message": f"Triggered build on branch {activation_code}"})
