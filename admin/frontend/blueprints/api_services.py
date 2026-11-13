# frontend/blueprints/api_services.py
"""Service, package, and DLC management API endpoints."""
from __future__ import annotations

import os
import threading
import time
from uuid import uuid4

import requests
from flask import current_app, jsonify, request

from blueprints import bp
from config import Config
from extensions import socketio
from services.api_client import api_delete, api_get, api_post, search_user
from services.dlc_transfer import models_missing, needs_parts, send_dlc_file, takes_parts
from services.dlc_service import (
    cipher_suite,
    dlc_jobs,
    dlc_jobs_lock,
    update_clients,
    update_clients_lock,
)


# --- Service Management --------------------------------------------------------
@bp.route("/api/services/all", methods=["GET"])
def api_get_all_services():
    """Get all available services."""
    try:
        resp = api_get("/services")
        resp.raise_for_status()
        return jsonify(resp.json())
    except requests.exceptions.RequestException as exc:
        current_app.logger.error(f"Could not get master services list from API: {exc}")
        return jsonify({"error": "Upstream service unavailable"}), 503


@bp.route("/api/services/add", methods=["POST"])
def api_add_master_service():
    """Add a new master service."""
    data = request.get_json() or {}
    required = ["name", "description", "dlc_folder", "package"]
    if not all(f in data for f in required):
        return jsonify({"error": "Missing required fields"}), 400
    try:
        resp = api_post("/services", data)
        # ส่งเหตุผลจริงจาก backend กลับไป: ก่อนหน้านี้ทุก error รวมทั้ง 400 ที่บอก
        # ว่า "โฟลเดอร์ซ้ำ" ถูกกลืนเป็น "ต่อ API ไม่ได้" ซึ่งไม่จริงและพาไปหา
        # ปัญหาผิดเรื่อง
        if 400 <= resp.status_code < 500:
            try:
                detail = (resp.json() or {}).get("detail")
            except Exception:
                detail = None
            return jsonify({"error": detail or "ข้อมูลที่ส่งไปไม่ถูกต้อง"}), resp.status_code
        resp.raise_for_status()
        current_app.logger.info(f"Successfully added new master service: {data['name']}")
        return jsonify(resp.json()), resp.status_code
    except requests.exceptions.RequestException as exc:
        current_app.logger.error(f"Failed to add master service via API: {exc}")
        return jsonify({"error": "Failed to connect to the service management API."}), 503


@bp.route("/api/services/<service_id>", methods=["DELETE"])
def api_delete_master_service(service_id: str):
    """Delete a master service by ID."""
    try:
        resp = api_delete(f"/services/{service_id}")
        resp.raise_for_status()
        current_app.logger.info(f"Successfully deleted master service ID: {service_id}")
        return jsonify({"message": "Service deleted successfully."}), 200
    except requests.exceptions.RequestException as exc:
        current_app.logger.error(f"Failed to delete master service via API: {exc}")
        return jsonify({"error": "Failed to connect to the service management API."}), 503


# --- Package Management -------------------------------------------------------------
@bp.route("/api/packages", methods=["GET"])
def api_list_packages():
    """List all packages."""
    try:
        resp = api_get("/packages")
        resp.raise_for_status()
        return jsonify(resp.json())
    except requests.exceptions.RequestException as exc:
        current_app.logger.error(f"Failed to list packages: {exc}")
        return jsonify({"error": "Failed to list packages"}), 503


@bp.route("/api/packages", methods=["POST"])
def api_create_package():
    """Create a new package."""
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Package name required"}), 400
    try:
        resp = api_post("/packages", {"name": name})
        resp.raise_for_status()
        return jsonify(resp.json()), resp.status_code
    except requests.exceptions.RequestException as exc:
        current_app.logger.error(f"Failed to create package: {exc}")
        return jsonify({"error": "Failed to create package"}), 503


@bp.route("/api/packages/<int:package_id>", methods=["DELETE"])
def api_delete_package(package_id: int):
    """Delete a package by ID."""
    try:
        resp = api_delete(f"/packages/{package_id}")
        resp.raise_for_status()
        return jsonify(resp.json()), resp.status_code
    except requests.exceptions.RequestException as exc:
        current_app.logger.error(f"Failed to delete package {package_id}: {exc}")
        return jsonify({"error": "Failed to delete package"}), 503


# --- User Details ----------------------------------------------------------------
@bp.route("/api/user/<activation_code>/details", methods=["GET"])
def api_get_user_details(activation_code: str):
    """Get detailed information for a user."""
    try:
        resp = api_get("/users")
        resp.raise_for_status()
        dataset = resp.json()
        user_details = search_user(dataset, activation_code)
        if user_details is None:
            return jsonify({"error": "User not found"}), 404
        svc_resp = api_get(f"/users/{activation_code}/services")
        svc_resp.raise_for_status()
        service_dataset = svc_resp.json()
        payload = {
            "activation_code": user_details.get("activation_code"),
            "package": user_details.get("package"),
            "is_online": user_details.get("is_online", False),
            "services": service_dataset,
        }
        return jsonify(payload)
    except requests.exceptions.HTTPError as exc:
        if exc.response.status_code == 404:
            return jsonify({"error": "User not found"}), 404
        return jsonify({"error": "Upstream service error"}), 502
    except requests.exceptions.RequestException as exc:
        current_app.logger.error(f"Failed to get user details for {activation_code}: {exc}")
        return jsonify({"error": "Upstream service unavailable"}), 503


# --- User Services Update (with DLC push) ---------------------------------------
@bp.route("/api/user/<activation_code>/update-services", methods=["POST"])
def api_update_user_services(activation_code: str):
    """Update services for a user and push DLC content."""
    data = request.get_json() or {}
    service_ids = data.get("service_ids")
    new_package = data.get("package")
    
    # Validate and cast service_ids
    try:
        service_ids = list(map(int, service_ids)) if service_ids is not None else None
    except Exception:
        return jsonify({"error": "Invalid service_ids format"}), 400
    
    if new_package is None or service_ids is None:
        return jsonify({"error": "Both 'package' and 'service_ids' are required fields"}), 400
    
    api_url = f"/users/{activation_code}/services"
    
    # Read client version from dedicated config file
    client_version = "1.0.0"
    try:
        import json as _json
        _cv_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '..', 'backend', 'client_version.json')
        with open(_cv_path, 'r') as _f:
            client_version = _json.load(_f).get("version", "1.0.0")
    except Exception:
        current_app.logger.warning("Could not read client_version.json, using default 1.0.0")
    
    payload = {"package": new_package, "version": client_version, "service_ids": service_ids}
    current_app.logger.info(f"Proxying update-services request for {activation_code}")
    
    try:
        resp = api_post(api_url, payload)
        resp.raise_for_status()

        # Fetch services to determine DLC folders
        try:
            services_resp = api_get("/services")
            services_resp.raise_for_status()
            all_services = services_resp.json()
        except Exception as exc:
            current_app.logger.error(f"Could not retrieve services list for DLC push: {exc}")
            return jsonify(resp.json()), resp.status_code

        # Build mapping id->dlc_folder
        id_to_folder = {}
        for svc in all_services:
            try:
                sid = int(svc.get("id")) if svc.get("id") is not None else None
                folder = svc.get("dlc_folder") or svc.get("name")
                if sid is not None and folder:
                    id_to_folder[sid] = str(folder)
            except Exception:
                continue
        selected_folders = sorted({id_to_folder.get(sid) for sid in service_ids if id_to_folder.get(sid)})

        job_id = None
        if selected_folders:
            job_id = uuid4().hex
            with dlc_jobs_lock:
                dlc_jobs[job_id] = {
                    "status": "running",
                    "activation_code": activation_code,
                    "requested_folders": selected_folders,
                    "started": time.time(),
                }

            # Count total files
            total_files = _count_total_files(selected_folders)
            with dlc_jobs_lock:
                dlc_jobs[job_id].update({
                    "total_files": total_files,
                    "processed_files": 0,
                    "current_folder": None,
                    "current_file": None,
                })

            # Capture app reference for use in background thread
            app = current_app._get_current_object()

            # Start async push
            def _wrapped_push():
                with app.app_context():
                    try:
                        def _progress_hook(folder: str, rel_file: str | None, increment: bool):
                            with dlc_jobs_lock:
                                job = dlc_jobs.get(job_id)
                                if not job:
                                    return
                                if increment:
                                    job["processed_files"] = min(
                                        job.get("processed_files", 0) + 1,
                                        job.get("total_files", 0) or 0
                                    )
                                job["current_folder"] = folder
                                job["current_file"] = rel_file

                        _push_selected_dlc_with_progress(
                            activation_code, selected_folders, _progress_hook, job_id,
                            set(id_to_folder.values()),
                        )
                        with dlc_jobs_lock:
                            job = dlc_jobs.get(job_id)
                            # Do not paint over a failure: the push marks the
                            # job as error when the branch is offline, and
                            # reporting "complete" on top of that is what hid
                            # a silently dropped push from the admin.
                            if job and job.get("status") != "error":
                                job.update({"status": "complete", "finished": time.time()})
                    except Exception as exc:
                        app.logger.error(f"[DLC] Job {job_id} failed: {exc}")
                        with dlc_jobs_lock:
                            job = dlc_jobs.get(job_id)
                            if job:
                                job.update({"status": "error", "error": str(exc), "finished": time.time()})

            threading.Thread(target=_wrapped_push, daemon=True).start()
        else:
            # No folders selected - remove all
            _handle_no_folders_selected(activation_code, set(id_to_folder.values()))

        backend_payload = resp.json()
        if job_id:
            backend_payload["dlc_job_id"] = job_id
            backend_payload.setdefault("message", "Services updated. DLC distribution in progress.")
        return jsonify(backend_payload), resp.status_code

    except requests.exceptions.HTTPError as exc:
        status_code = exc.response.status_code
        try:
            error_message = exc.response.json().get("error", "An error occurred on the backend service.")
        except Exception:
            error_message = "An error occurred on the backend service."
        current_app.logger.error(f"API returned error {status_code} for updating {activation_code}: {error_message}")
        return jsonify({"error": error_message}), status_code
    except requests.exceptions.RequestException as exc:
        current_app.logger.error(f"Could not connect to backend to update services for {activation_code}: {exc}")
        return jsonify({"error": "The user management service is currently unavailable."}), 503


# --- Helper functions for DLC push -----------------------------------------------
def _count_total_files(folders: list[str]) -> int:
    """Count total files in the given folders."""
    total = 0
    for f in folders:
        root = os.path.join(Config.DLC_MASTER_PATH, f)
        if not os.path.isdir(root):
            continue
        for _r, _d, files in os.walk(root):
            total += len(files)
    return total


def _push_selected_dlc_with_progress(client_id: str, folders: list[str], hook, job_id: str,
                                     service_folders: set | None = None):
    """Push selected DLC folders to a client with progress tracking.

    ``service_folders`` is every folder that belongs to a real service. Only
    those may be deleted from a branch: managed_services also holds shared
    payloads (configs/) that no service owns, and removing one breaks every
    service that imports it.
    """
    with update_clients_lock:
        meta = update_clients.get(client_id)
        sid = meta.get("sid") if meta else None

    if not sid:
        # The branch drops its socket for a few seconds after every DLC update
        # to restart services. A save landing in that window used to return
        # here quietly while the job was still reported as complete, so the
        # admin saw a green result for a push that never left the server.
        msg = (f"สาขา {client_id} ออฟไลน์อยู่ ยังไม่ได้ส่งไฟล์ "
               f"— กดบันทึกอีกครั้งเมื่อสาขาออนไลน์")
        current_app.logger.warning(f"[DLC] {msg}")
        with dlc_jobs_lock:
            job = dlc_jobs.get(job_id)
            if job:
                job.update({"status": "error", "error": msg, "finished": time.time()})
        return

    wanted = []
    for folder in folders:
        root = os.path.join(Config.DLC_MASTER_PATH, folder)
        for r, _d, files in os.walk(root):
            wanted += [os.path.relpath(os.path.join(r, f), Config.DLC_MASTER_PATH).replace(os.sep, "/") for f in files]
    missing = models_missing(wanted)
    if missing:
        msg = (f"ยังไม่ได้ส่งให้สาขา {client_id}: บน server ยังไม่มีไฟล์โมเดล {', '.join(missing)} "
               f"— คัดลอกไฟล์ .bin ขึ้น server ก่อน (สร้างด้วย backend/tools/pack_model.py)")
        current_app.logger.error(f"[DLC] {msg}")
        with dlc_jobs_lock:
            job = dlc_jobs.get(job_id)
            if job:
                job.update({"status": "error", "error": msg, "finished": time.time()})
        return
    if needs_parts(wanted) and not takes_parts(socketio, sid):
        # Nothing has been sent: half an update (new code, no model) would stop
        # services the branch runs today.
        msg = (f"สาขา {client_id} ใช้โปรแกรมเวอร์ชันเก่าที่รับไฟล์ใหญ่ (โมเดล) ไม่ได้ ยังไม่ได้ส่ง "
               f"— สาขายังใช้งานบริการเดิมได้ตามปกติ อัปเดตโปรแกรมที่สาขาก่อนแล้วกดบันทึกอีกครั้ง")
        current_app.logger.warning(f"[DLC] {msg}")
        with dlc_jobs_lock:
            job = dlc_jobs.get(job_id)
            if job:
                job.update({"status": "error", "error": msg, "finished": time.time()})
        return

    prev = meta.get("last_sent_folders", []) if meta else []
    
    try:
        server_top_level = [
            name for name in os.listdir(Config.DLC_MASTER_PATH)
            if os.path.isdir(os.path.join(Config.DLC_MASTER_PATH, name))
        ]
    except Exception:
        server_top_level = []

    # Only folders that belong to a service may be removed. Without this,
    # every save deleted shared payloads like configs/ as well, because they
    # have no service id to tick and so always counted as "not selected".
    candidates = set(prev) | set(server_top_level)
    if service_folders is not None:
        candidates &= set(service_folders)
    removed = sorted(candidates - set(folders))

    if meta is not None:
        meta["last_sent_folders"] = folders
    
    current_app.logger.info(f"[DLC] Pushing targeted folders to {client_id}: add/update={folders} remove={removed}")
    socketio.emit("dlc_update_begin", {"folders": folders, "remove": removed}, to=sid)
    
    for folder in removed:
        socketio.emit("delete_folder", {"folder": folder}, to=sid)
    
    for folder in folders:
        root = os.path.join(Config.DLC_MASTER_PATH, folder)
        if not os.path.isdir(root):
            current_app.logger.info(f"[DLC] Folder not found for push: {folder}")
            continue
        
        # Recalculate if needed
        with dlc_jobs_lock:
            job = dlc_jobs.get(job_id)
            need_recalc = job and (job.get("total_files", 0) == 0)
        if need_recalc:
            folder_file_count = sum(len(files) for _, _, files in os.walk(root))
            with dlc_jobs_lock:
                job = dlc_jobs.get(job_id)
                if job:
                    job["total_files"] = (job.get("total_files", 0) or 0) + folder_file_count
        
        socketio.emit("service_folder_begin", {"folder": folder}, to=sid)
        base_len = len(root.rstrip(os.sep)) + 1
        file_count = 0
        current_app.logger.info(f"[DLC] Pushing folder {folder} to {client_id}")
        
        for r, _d, files in os.walk(root):
            for fname in files:
                full = os.path.join(r, fname)
                rel_inside = full[base_len:].replace(os.sep, "/")
                current_app.logger.info(f"[DLC]   Sending file {folder}/{rel_inside} to {client_id}")
                # Large files (models) go in acknowledged parts (services/dlc_transfer.py);
                # only .py is encrypted — other files must land on disk as they are.
                if not send_dlc_file(socketio, sid, f"{folder}/{rel_inside}", cipher_suite, current_app.logger):
                    continue
                file_count += 1
                hook(folder, rel_inside, True)
                time.sleep(2)
        
        socketio.emit("service_folder_complete", {"folder": folder, "file_count": file_count}, to=sid)
    
    socketio.emit("dlc_update_complete", {"folders": folders, "removed": removed}, to=sid)


def _handle_no_folders_selected(activation_code: str, service_folders: set | None = None):
    """Handle case when no folders are selected - remove all DLC from client.

    ``service_folders`` limits the removal to folders that belong to a real
    service, so unticking everything strips the branch of its services without
    also deleting shared payloads like configs/ that no service owns.
    """
    with update_clients_lock:
        meta = update_clients.get(activation_code)
        sid = meta.get("sid") if meta else None

    if not sid:
        current_app.logger.warning(
            f"[DLC] สาขา {activation_code} ออฟไลน์ ยังไม่ได้ถอดบริการออกจากเครื่อง "
            f"— กดบันทึกอีกครั้งเมื่อสาขาออนไลน์")
        return

    prev = meta.get("last_sent_folders", []) if meta else []
    try:
        server_top_level = [
            name for name in os.listdir(Config.DLC_MASTER_PATH)
            if os.path.isdir(os.path.join(Config.DLC_MASTER_PATH, name))
        ]
    except Exception:
        server_top_level = []

    candidates = set(prev) | set(server_top_level)
    if service_folders is not None:
        candidates &= set(service_folders)
    removed = sorted(candidates)
    if meta is not None:
        meta["last_sent_folders"] = []

    current_app.logger.info(f"[DLC] No folders selected for {activation_code}. Removing: {removed}")
    socketio.emit("dlc_update_begin", {"folders": [], "remove": removed}, to=sid)

    for folder in removed:
        socketio.emit("delete_folder", {"folder": folder}, to=sid)
        time.sleep(0.5)

    socketio.emit("dlc_update_complete", {"folders": [], "removed": removed}, to=sid)
