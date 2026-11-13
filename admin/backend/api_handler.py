# backend/api_handler.py
"""Socket-based API command handling."""
from __future__ import annotations

import socket
import threading
from datetime import datetime
from typing import Tuple

from config import (
    HOST,
    UPDATE_CONTROL_PORT,
    DLC_MASTER_PATH,
    clients,
    clients_lock,
    load_server_config,
    save_server_config,
)
from dlc_manager import get_master_manifest, update_single_client, update_single_client_folder_dlc
from socket_utils import send_msg, recv_msg


def api_broadcast_updates() -> Tuple[bool, str]:
    """Initiates an update for all connected clients."""
    print("\n--- [API] INITIATING UPDATE BROADCAST ---")
    master_manifest = get_master_manifest(DLC_MASTER_PATH)
    server_config = load_server_config()
    server_config['last_update'] = datetime.now().isoformat()
    save_server_config(server_config)
    print(f"[SERVER] Broadcasting with Version: {server_config['version']}, Package: {server_config['package']}")

    with clients_lock:
        active_clients = dict(clients)

    if not active_clients:
        print("[SERVER] No clients connected to update.")
        return True, "Broadcast initiated, but no clients were connected."

    for client_id, client_data in active_clients.items():
        update_single_client(client_id, client_data, master_manifest, server_config)

    print("\n--- UPDATE BROADCAST COMPLETE ---")
    return True, f"Broadcast complete. Updated {len(active_clients)} client(s)."


def api_update_specific_client(target_id: str) -> Tuple[bool, str]:
    """Initiates an update for a single specified client."""
    print(f"\n--- [API] INITIATING SINGLE CLIENT UPDATE for '{target_id}' ---")
    with clients_lock:
        if target_id not in clients:
            msg = f"Client ID '{target_id}' not found."
            print(f"[API] ERROR: {msg}")
            return False, msg
        client_data = clients[target_id]

    try:
        server_config = load_server_config()
        server_config['last_update'] = datetime.now().isoformat()
        master_manifest = get_master_manifest(DLC_MASTER_PATH)
        update_single_client(target_id, client_data, master_manifest, server_config)
        
        msg = f"Update successfully pushed to '{target_id}'."
        print(f"\n--- SINGLE CLIENT UPDATE for '{target_id}' COMPLETE ---")
        return True, msg
    except Exception as e:
        msg = f"An unexpected error occurred: {e}"
        print(f"[API] ERROR: {msg}")
        return False, msg


def handle_api_command(conn) -> None:
    """Handles a single incoming API command."""
    try:
        request = recv_msg(conn)
        if not request:
            return

        command = request.get("command")
        print(f"[API] Received command: {command}")
        
        if command == "broadcast_updates":
            success, message = api_broadcast_updates()
            send_msg(conn, {"status": "success" if success else "error", "message": message})

        elif command == "update_client":
            client_id = request.get("client_id")
            if not client_id:
                send_msg(conn, {"status": "error", "message": "Missing 'client_id' parameter."})
                return
            success, message = api_update_specific_client(client_id)
            send_msg(conn, {"status": "success" if success else "error", "message": message})

        elif command == "update_client_by_id_service":
            client_id = request.get("client_id")
            target_folders = request.get("list_folder_dlc")
            
            if not client_id or not target_folders:
                send_msg(conn, {"status": "error", "message": "Missing 'client_id' or 'list_folder_dlc' parameter."})
                return
            if not isinstance(target_folders, list):
                send_msg(conn, {"status": "error", "message": "'list_folder_dlc' must be a list of folder names."})
                return

            with clients_lock:
                if client_id not in clients:
                    send_msg(conn, {"status": "error", "message": f"Client ID '{client_id}' not found."})
                    return
                client_data = clients[client_id]

            server_config = load_server_config()
            master_manifest = get_master_manifest(DLC_MASTER_PATH)
            
            success, message = update_single_client_folder_dlc(
                client_id, client_data, master_manifest, server_config, target_folders
            )
            send_msg(conn, {"status": "success" if success else "error", "message": message})

        else:
            send_msg(conn, {"status": "error", "message": f"Unknown command: '{command}'"})
            
    except Exception as e:
        print(f"[API] Error handling API command: {e}")
    finally:
        conn.close()


def start_api_listener() -> None:
    """Starts the listener for API commands on UPDATE_CONTROL_PORT."""
    api_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    api_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    api_socket.bind((HOST, UPDATE_CONTROL_PORT))
    api_socket.listen()
    print(f"[API] Listening for API commands on {HOST}:{UPDATE_CONTROL_PORT}")
    
    while True:
        try:
            conn, addr = api_socket.accept()
            threading.Thread(target=handle_api_command, args=(conn,), daemon=True).start()
        except OSError:
            break
