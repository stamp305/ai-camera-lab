# frontend/blueprints/api_system.py
import subprocess
import os
import logging
from datetime import datetime
from flask import jsonify
from blueprints import bp
from extensions import socketio

logger = logging.getLogger(__name__)

# The git root is likely the python_server directory
# This file is in python_server/ai-camera-admin/frontend/blueprints/
GIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# Mock admin: found from this file, so the copy works whatever its folder is called
# (it lives in admin/, not ai-camera-admin/).
CLIENT_UPDATE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                                 'backend', 'client_update_master')

import zipfile
import shutil


@bp.route('/api/force-main-update/<activation_code>', methods=['POST'])
def force_main_update(activation_code: str):
    """Push the main-app update payload to a specific client over Socket.IO."""
    from socket_handlers import push_main_update

    result = push_main_update([activation_code])
    status = 200 if result.get("success") else 502
    return jsonify(result), status


@bp.route('/api/broadcast-force-update', methods=['POST'])
def api_broadcast_force_update():
    """Broadcast a force-update signal to all connected clients.

    Each client reacts by emitting `request_update`, which triggers the
    backend force-update (push assigned version) via the request_update handler.
    """
    from services.managed_service import update_clients, update_clients_lock
    with update_clients_lock:
        sids = [meta["sid"] for meta in update_clients.values()]
    for sid in sids:
        socketio.emit("broadcast_force_update", {"command": "broadcast_force_update"}, to=sid)
    return jsonify({"success": True, "clients": len(sids)})


@bp.route('/api/system/upload-client-zip', methods=['POST'])
def upload_client_zip():
    from flask import request
    try:
        if 'file' not in request.files:
            return jsonify({"error": "No file part"}), 400
        file = request.files['file']
        if file.filename == '':
            return jsonify({"error": "No selected file"}), 400
        
        if not file.filename.endswith('.zip'):
            return jsonify({"error": "กรุณาอัพโหลดไฟล์ .zip เท่านั้น"}), 400
            
        os.makedirs(CLIENT_UPDATE_DIR, exist_ok=True)
        
        temp_zip_path = os.path.join(os.path.dirname(CLIENT_UPDATE_DIR), 'temp_client_update.zip')
        file.save(temp_zip_path)
        
        # Clean existing directory EXCEPT .git
        for item in os.listdir(CLIENT_UPDATE_DIR):
            if item == '.git': continue
            item_path = os.path.join(CLIENT_UPDATE_DIR, item)
            if os.path.isdir(item_path):
                shutil.rmtree(item_path)
            else:
                os.remove(item_path)
                
        # Extract new files
        with zipfile.ZipFile(temp_zip_path, 'r') as zip_ref:
            zip_ref.extractall(CLIENT_UPDATE_DIR)
            
        os.remove(temp_zip_path)
        
        # Handle nested folder (if they zipped the folder itself instead of contents)
        extracted_items = [i for i in os.listdir(CLIENT_UPDATE_DIR) if i != '.git']
        if len(extracted_items) == 1:
            single_item_path = os.path.join(CLIENT_UPDATE_DIR, extracted_items[0])
            if os.path.isdir(single_item_path):
                for item in os.listdir(single_item_path):
                    shutil.move(os.path.join(single_item_path, item), CLIENT_UPDATE_DIR)
                os.rmdir(single_item_path)

        # Create VERSION file if missing (timestamp-based)
        version_path = os.path.join(CLIENT_UPDATE_DIR, "VERSION")
        if not os.path.exists(version_path):
            ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            with open(version_path, 'w') as f:
                f.write(f"V.{ts}\n")
            logger.info(f"[UPLOAD-ZIP] Created VERSION file: V.{ts}")

        return jsonify({"success": True, "message": "อัพโหลดไฟล์อัพเดตสำเร็จ พร้อมอัพเดตให้สาขาแล้ว!"})
        
    except Exception as e:
        logger.error(f"Upload zip error: {str(e)}")
        return jsonify({"error": str(e)}), 500
