# frontend/socket_handlers.py
"""Socket.IO event handlers for client communication."""
from __future__ import annotations

import os
import base64
import threading
import time
from datetime import datetime
from typing import Any, Optional

import requests
from flask import current_app, request
from flask_socketio import disconnect

from config import Config
from extensions import socketio
from gate import check_socket_connect, socket_identify_refusal
from services.dlc_transfer import models_missing, needs_parts, send_dlc_file, takes_parts
from services.dlc_service import (
    build_master_manifest,
    cipher_suite,
    update_clients,
    update_clients_lock,
)


def _webhook_notify(event_type: str, client_id: str, info: dict):
    """Notify webhook about client connection events."""
    webhook_url = os.getenv("AI_CAMERA_ADMIN_BACKEND_URL", "http://127.0.0.1:8011") + "/users/online"
    payload = {
        "event": event_type,
        "client_id": client_id,
        "timestamp": datetime.utcnow().isoformat(),
        "current_client_list": [cid for cid in update_clients.keys() if cid],
        "client_versions": {
            cid: (meta.get("info") or {}).get("version")
            for cid, meta in update_clients.items()
            if cid and (meta.get("info") or {}).get("version")
        },
    }
    try:
        resp = requests.patch(webhook_url, json=payload, timeout=5)
        if not resp.ok:
            current_app.logger.warning(f"[WEBHOOK] Notify failed status={resp.status_code} body={resp.text}")
        else:
             current_app.logger.info(f"[WEBHOOK] Notify success: {resp.status_code}")
    except Exception as e:
        current_app.logger.warning(f"[WEBHOOK] Failed to notify {webhook_url}: {e}")


# sid -> (the Activation Code the connection proved at connect or at its first
# identify, or None while it is provisional (no auth); whether it proved it with
# a Device Key). Set only once accepted, dropped on disconnect. No entry: not
# accepted yet, or already gone.
_connect_codes = {}


@socketio.on("connect")
def sio_connect(auth=None):
    """Handle client connection. The gate refuses it, or says which code it proved.

    Never log `auth`: it carries the Device Key.
    """
    connected = check_socket_connect(auth)
    # verify-device yields to other handlers: a client that hung up meanwhile has
    # already been through sio_disconnect, so remembering it now would leak.
    if not socketio.server.manager.is_connected(request.sid, request.namespace):
        return
    _connect_codes[request.sid] = connected
    current_app.logger.info(f"[SOCKET] Client connected sid={request.sid}")


@socketio.on("identify")
def sio_identify(data: Any):
    """Handle client identification.

    What it returns is the acknowledgement of this identify, for a client that asked
    for one: {"status": "ok" | "retry" | "refused" | "missing_activateID", ...}. A
    client that did not ask ignores it and keeps listening for the "identified"
    event, which is still sent. "retry" says the identify was not taken (the
    connection is not accepted yet): the client sends it again.
    """
    if not isinstance(data, dict) or data.get("command") != "identify":
        return {"status": "ignored"}
    info = data.get("info") or {}
    current_app.logger.info(f"[SOCKET] Identify received: {info}")
    client_id = info.get("activateID")
    if not client_id or not str(client_id).strip():
        missing = {"status": "missing_activateID"}
        socketio.emit("identified", missing, to=request.sid)
        return missing
    connected = _connect_codes.get(request.sid)
    if connected is None:
        # Handlers run concurrently in production, so an identify sent right behind
        # connect can arrive while the gate still judges it. A real client waits
        # for the connect ack.
        current_app.logger.info(f"[SOCKET] Ignored identify before connect was accepted sid={request.sid}")
        return {"status": "retry"}
    code, keyed = connected
    # A connected client repeats its identify every few minutes, in case this side
    # lost track of the connection. One already registered on this very connection
    # is answered as it is, without reporting it online or checking its key again.
    with update_clients_lock:
        known = update_clients.get(client_id)
        repeat = code == client_id and known and known["sid"] == request.sid
    if repeat:
        # The repeat is the branch's only regular word from the admin, so answer it
        # with the version assigned now, not the one cached when it connected: a
        # version assigned since then otherwise never reached it before a reconnect.
        assigned_version = _fetch_assigned_version(client_id, on_failure=known.get("assigned_version"))
        with update_clients_lock:
            meta = update_clients.get(client_id)
            if meta and meta["sid"] == request.sid:
                meta["assigned_version"] = assigned_version
        return {"status": "ok", "client_id": client_id,
                "assigned_version": assigned_version, "known": True}
    refusal = socket_identify_refusal(code, keyed, client_id)
    if refusal:
        current_app.logger.info(f"[SOCKET] Refused identify as {client_id} sid={request.sid} ({refusal}); disconnecting")
        disconnect()
        return {"status": "refused"}
    # A keyless identify waits on verify-device: the client may have hung up,
    # or another identify on this sid pinned a code, meanwhile.
    if _connect_codes.get(request.sid) not in (connected, (client_id, keyed)):
        return {"status": "retry"}
    _connect_codes[request.sid] = (client_id, keyed)  # pins a provisional connection to its first code
    with update_clients_lock:
        update_clients[client_id] = {
            "sid": request.sid,
            "info": info,
            "connected_at": time.time(),
        }
    _report_identify(client_id, info)
    assigned_version = _fetch_assigned_version(client_id)
    with update_clients_lock:
        meta = update_clients.get(client_id)
        if meta and meta["sid"] == request.sid:
            meta["assigned_version"] = assigned_version  # for the answer to a repeat
    reply = {"status": "ok", "client_id": client_id, "assigned_version": assigned_version}
    # Both: a client from before acknowledgements only listens for this event.
    socketio.emit("identified", reply, to=request.sid)
    _webhook_notify("client_connected", client_id, info)
    current_app.logger.info(f"[SOCKET] Identified client_id={client_id} sid={request.sid} keyed={keyed}")
    _maybe_auto_update(client_id, info, assigned_version)
    return reply


def _report_identify(client_id: str, info: dict) -> None:
    """Tell the backend the branch is online and which version it runs.

    Without this the version manager can never show a branch's current
    version, and the auto-update below would re-push the same version on
    every reconnect.
    """
    try:
        base = current_app.config.get("DATA_SERVICE_API_BASE", "http://localhost:8011")
        requests.post(
            f"{base}/branches/{client_id}/identify",
            json={
                "version": (info or {}).get("version"),
                "branch_name": (info or {}).get("branchName"),
            },
            timeout=5,
        )
    except Exception as e:
        current_app.logger.warning(f"[SOCKET] Could not report identify for '{client_id}': {e}")


def _fetch_assigned_version(client_id: str, on_failure: Optional[str] = None) -> Optional[str]:
    """Look up the admin-assigned Git version for a branch from the backend.

    ``on_failure`` is returned when the backend cannot be asked, so a caller with
    a value already in hand does not hand the branch "nothing assigned" instead.
    """
    try:
        base = current_app.config.get("DATA_SERVICE_API_BASE", "http://localhost:8011")
        resp = requests.get(f"{base}/branches", timeout=5)
        if resp.status_code != 200:
            return on_failure
        for branch in resp.json():
            if branch.get("activation_code") == client_id:
                return branch.get("assigned_version")
    except Exception:
        return on_failure
    return None


def _maybe_auto_update(client_id: str, info: dict, assigned_version: Optional[str]) -> None:
    """Auto-push the admin-assigned version when a branch reports a different one.

    The admin assigning a version to a branch IS the authorization to update:
    whenever the branch identifies with a version that does not match its
    assigned version, the assigned version is pushed automatically so the
    branch updates itself without needing to be force-updated manually.
    """
    if not assigned_version:
        return
    current_version = (info or {}).get("version") if isinstance(info, dict) else None
    if current_version == assigned_version:
        return
    app = current_app._get_current_object()
    base = app.config.get("DATA_SERVICE_API_BASE", "http://localhost:8011")
    app.logger.info(
        f"[AUTO-UPDATE] Branch '{client_id}' on '{current_version}' != assigned '{assigned_version}'. Pushing assigned version..."
    )

    def _trigger():
        try:
            resp = requests.post(f"{base}/branches/{client_id}/force-update", timeout=60)
            app.logger.info(
                f"[AUTO-UPDATE] Force-update response for '{client_id}': HTTP {resp.status_code}"
            )
        except Exception as e:
            app.logger.error(f"[AUTO-UPDATE] Trigger failed for '{client_id}': {e}")

    threading.Thread(target=_trigger, daemon=True).start()


@socketio.on("manifest")
def sio_manifest(data: Any):
    """Handle client manifest and perform DLC diff."""
    if not isinstance(data, dict):
        return
    
    # Identify client_id from sid
    client_id = None
    with update_clients_lock:
        for cid, meta in update_clients.items():
            if meta["sid"] == request.sid:
                client_id = cid
                break
    if client_id is None:
        return
    
    master_manifest = build_master_manifest()
    client_manifest = data
    
    # Determine actions
    client_files = set(client_manifest.keys())
    master_files = set(master_manifest.keys())
    delete_list = sorted(client_files - master_files)

    current_app.logger.info(f"[DLC] Client {client_id}")
    current_app.logger.info(f"[DLC]   Client manifest files: {len(client_files)}")
    current_app.logger.info(f"[DLC]   Master manifest files: {len(master_files)}")

    add_or_update = []
    for rel in master_files:
        server_hash = master_manifest[rel]
        if rel not in client_manifest or client_manifest.get(rel) != server_hash:
            add_or_update.append(rel)

    current_app.logger.info(
        f"[DLC] Diff for {client_id}: delete={len(delete_list)} update/add={len(add_or_update)}"
    )
    
    missing = models_missing(add_or_update)
    if missing:
        current_app.logger.error(
            f"[DLC] {client_id}: update held back — not in DLC master: {', '.join(missing)}. "
            f"Copy the model .bin files to the server (backend/tools/pack_model.py makes them)")
        socketio.emit("manifest_ack", {"received_keys": list(data.keys())[:10]}, to=request.sid)
        return

    if needs_parts(add_or_update) and not takes_parts(socketio, request.sid):
        current_app.logger.warning(
            f"[DLC] {client_id}: update held back — it has files that go in parts and this "
            f"branch's client cannot receive them; it keeps its current services until its client is updated")
        socketio.emit("manifest_ack", {"received_keys": list(data.keys())[:10]}, to=request.sid)
        return

    # Send deletes first
    for rel in delete_list:
        socketio.emit(
            "delete_file",
            {"command": "delete_file", "filename": rel},
            to=request.sid,
        )
    
    # Send add/update with base64 content
    for rel in add_or_update:
        # Large files (models) go in acknowledged parts (services/dlc_transfer.py).
        send_dlc_file(socketio, request.sid, rel, cipher_suite, current_app.logger)
    
    # Completion notification
    socketio.emit(
        "update_complete",
        {"command": "update_complete", "stats": {"deleted": len(delete_list), "added": len(add_or_update)}},
        to=request.sid,
    )
    
    # Acknowledge manifest reception (legacy style)
    socketio.emit("manifest_ack", {"received_keys": list(data.keys())[:10]}, to=request.sid)


@socketio.on("request_update")
def sio_request_update(data: Any):
    """Client asks admin to push its assigned version now (manual button / force broadcast)."""
    client_id = None
    with update_clients_lock:
        for cid, meta in update_clients.items():
            if meta["sid"] == request.sid:
                client_id = cid
                break
    if client_id is None:
        return
    base = current_app.config.get("DATA_SERVICE_API_BASE", "http://localhost:8011")
    current_app.logger.info(f"[REQUEST-UPDATE] Client '{client_id}' requested update: {data}")
    try:
        resp = requests.post(f"{base}/branches/{client_id}/force-update", timeout=60)
        current_app.logger.info(f"[REQUEST-UPDATE] force-update response HTTP {resp.status_code}")
    except Exception as e:
        current_app.logger.error(f"[REQUEST-UPDATE] Trigger failed for '{client_id}': {e}")


@socketio.on("disconnect")
def sio_disconnect():
    """Handle client disconnection."""
    _connect_codes.pop(request.sid, None)
    removed_id = None
    with update_clients_lock:
        for cid, meta in list(update_clients.items()):
            if meta["sid"] == request.sid:
                # Two connections of one Branch overlap while it reconnects. When the
                # one registered last hangs up first, the other is still open:
                # dropping the Branch here left it connected and shown offline for good.
                other = next((sid for sid, (code, _keyed) in list(_connect_codes.items()) if code == cid), None)
                if other:
                    meta["sid"] = other
                    current_app.logger.info(f"[SOCKET] {cid} kept on its other connection sid={other}")
                else:
                    removed_id = cid
                    del update_clients[cid]
                break
    if removed_id:
        _webhook_notify("client_disconnected", removed_id, {})
        current_app.logger.info(f"[SOCKET] Client disconnected client_id={removed_id}")

import os
from blueprints.api_system import CLIENT_UPDATE_DIR

# How long to wait for a branch to answer main_update_begin. Generous: the
# handler clears temp_update/ off disk before it returns.
BEGIN_ACK_TIMEOUT = 20
# Give the socket room to drain every this many bytes of payload, so a slow
# link is not handed the whole update faster than it can write it out.
DRAIN_EVERY_BYTES = 512 * 1024
DRAIN_PAUSE = 0.15


def push_main_update(target_client_ids: list[str]):
    """Push main app update to specified clients."""
    if not os.path.exists(CLIENT_UPDATE_DIR):
        current_app.logger.error("[MAIN UPDATE] client_update_master not found")
        return {"success": False, "error": "client_update_master folder not found"}

    # Ensure VERSION file exists — use assigned version from backend if possible
    version_path = os.path.join(CLIENT_UPDATE_DIR, "VERSION")
    if not os.path.exists(version_path):
        assigned_ver = None
        try:
            base = current_app.config.get("DATA_SERVICE_API_BASE", "http://localhost:8011")
            resp = requests.get(f"{base}/branches", timeout=5)
            if resp.status_code == 200:
                for branch in resp.json():
                    ac = branch.get("activation_code")
                    if ac and (ac in target_client_ids or "ALL" in target_client_ids):
                        av = branch.get("assigned_version")
                        if av:
                            assigned_ver = av
                            break
        except Exception:
            pass
        ver_str = assigned_ver or f"V.{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
        try:
            with open(version_path, 'w') as f:
                f.write(f"{ver_str}\n")
            current_app.logger.info(f"[MAIN UPDATE] Created VERSION file: {ver_str}")
        except Exception as e:
            current_app.logger.warning(f"[MAIN UPDATE] Could not create VERSION: {e}")

    sids_to_update = []
    with update_clients_lock:
        for cid, meta in update_clients.items():
            if cid in target_client_ids or "ALL" in target_client_ids:
                sids_to_update.append(meta["sid"])
                
    if not sids_to_update:
        current_app.logger.warning("[MAIN UPDATE] No online target clients found")
        return {"success": False, "error": "No targeted clients are currently online"}
        
    # Build-time tooling that has no job on a branch machine. generate_secure_env.py
    # is the recipe that bakes the credentials into secure_env.so, so shipping it
    # hands every store the means to read them back; the other two are only ever
    # run on a build host. Nothing on the client imports any of them.
    BUILD_ONLY_FILES = {"generate_secure_env.py", "_secrets.example.py", "build_cython.py"}

    def is_excluded(rel_path):
        ex = {".git", "logs", "temp_update", "dlc_local", "managed_services", ".env", "client_config.json", "client_version.json",
              ".build_state.json"}
        parts = rel_path.split(os.sep)
        if parts[0] in ex: return True
        if rel_path in BUILD_ONLY_FILES: return True
        if rel_path.endswith('.db') or rel_path.endswith('.sqlite'): return True
        # Deliberately no .py/.so pair filter here: the payload the build farm
        # produces already replaces every compiled module's source with a thin
        # launcher, and those launchers must reach the branch to overwrite any
        # full source an older build left behind. frontend_python/main.py also
        # keeps its source on purpose — run_frontend_service loads it by path,
        # so its .so is never imported.
        return False
        
    current_app.logger.info(f"[MAIN UPDATE] Starting push to {len(sids_to_update)} clients...")

    # Ask for an acknowledgement instead of firing and hoping. A sid stays in
    # update_clients until the server notices the socket died, so "1 client
    # online" can mean a connection that will never deliver anything — the push
    # then reports success while the branch receives nothing at all.
    # main_update_complete cannot be acked (that handler exits the process), so
    # the handshake happens here, on the one event the client answers normally.
    live_sids = []
    for sid in sids_to_update:
        try:
            socketio.call("main_update_begin", {"command": "main_update_begin"},
                          to=sid, timeout=BEGIN_ACK_TIMEOUT)
            live_sids.append(sid)
        except Exception as e:
            current_app.logger.warning(
                f"[MAIN UPDATE] No answer from sid={sid} within {BEGIN_ACK_TIMEOUT}s ({e}) — skipping it")
    if not live_sids:
        current_app.logger.error("[MAIN UPDATE] No client acknowledged the start — nothing was sent")
        return {"success": False,
                "error": "สาขาที่เลือกไม่ตอบรับ (เชื่อมต่อค้างอยู่แต่ไม่รับข้อมูล) ยังไม่ได้ส่งอะไรไป"}
    sids_to_update = live_sids
        
    file_count = 0
    sent_bytes = 0
    last_drain = 0
    for dirpath, _, filenames in os.walk(CLIENT_UPDATE_DIR):
        rel_dir = os.path.relpath(dirpath, CLIENT_UPDATE_DIR)
        if rel_dir == '.': rel_dir = ''
        
        if rel_dir and is_excluded(rel_dir):
            continue
            
        for file in filenames:
            rel_file_path = os.path.join(rel_dir, file) if rel_dir else file
            if is_excluded(rel_file_path):
                continue
                
            abs_path = os.path.join(dirpath, file)
            try:
                with open(abs_path, "rb") as f:
                    content = f.read()
                b64 = base64.b64encode(content).decode("ascii")
                for sid in sids_to_update:
                    socketio.emit("main_update_file", {
                        "filename": rel_file_path,
                        "content": b64
                    }, to=sid)
                file_count += 1
                
                # Yield to the event loop to prevent blocking under high load (many files/clients)
                import eventlet
                # Pace on volume, not on file count: 252 files are ~11.6 MB of
                # base64 and a fixed per-file yield pushes it all out in ~3s,
                # which is enough to bury a weak link's send buffer and kill the
                # connection before a single byte is delivered.
                sent_bytes += len(b64)
                if sent_bytes - last_drain >= DRAIN_EVERY_BYTES:
                    last_drain = sent_bytes
                    eventlet.sleep(DRAIN_PAUSE)
                else:
                    eventlet.sleep(0)
                
            except Exception as e:
                current_app.logger.error(f"[MAIN UPDATE] Read error {rel_file_path}: {e}")
                
    # Read pushed version for the completion event
    pushed_version = None
    if os.path.exists(version_path):
        try:
            with open(version_path, 'r') as f:
                pushed_version = f.read().strip()
        except Exception:
            pass

    for sid in sids_to_update:
        socketio.emit("main_update_complete", {
            "command": "main_update_complete",
            "version": pushed_version,
        }, to=sid)

    # Deliberately not "successfully pushed": everything above is emit(), and the
    # only proof the branch applied it is the version it reports when it comes
    # back after updater.py restarts it. The version manager shows that as
    # last_version, so the operator can see the difference between sent and done.
    current_app.logger.info(
        f"[MAIN UPDATE] Sent {file_count} files / {sent_bytes / 1048576:.1f} MB "
        f"(version={pushed_version}) to {len(sids_to_update)} client(s); "
        f"awaiting their restart to confirm")
    return {"success": True,
            "message": f"ส่งไฟล์อัพเดตให้ {len(sids_to_update)} สาขาแล้ว ({file_count} ไฟล์) "
                       f"— สาขาจะรีสตาร์ทเอง รอเวอร์ชันอัปเดตในตารางเพื่อยืนยัน"}
