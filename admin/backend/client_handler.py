# backend/client_handler.py
"""Client connection handling and webhook notifications."""
from __future__ import annotations

import os
import socket
import threading
import time
from typing import Any, Dict

import requests

from config import clients, clients_lock
from socket_utils import recv_msg

# Dictionary to track when a client was last seen (disconnected)
client_last_seen: Dict[str, float] = {}

def monitor_clients_offline_status() -> None:
    """Background task to check if clients have been disconnected for 30 minutes."""
    while True:
        time.sleep(60)  # Check every 1 minute to see if we hit the 30 min mark
        current_time = time.time()
        
        with clients_lock:
            for cid, last_seen_time in list(client_last_seen.items()):
                # If client is currently connected, it's fine
                if cid in clients:
                    continue
                
                elapsed_seconds = current_time - last_seen_time
                # 3 times of 10 minutes = 30 minutes (1800 seconds)
                if elapsed_seconds >= 1800:
                    print(f"[ALERT] Client '{cid}' is offline or not sending data for {elapsed_seconds/60:.0f} minutes.")
                    client_last_seen[cid] = current_time



def notify_webhook_on_client_change(event_type: str, client_id: str, client_info: Dict[str, Any]) -> None:
    """Sends a PATCH request to the webhook URL when a client connects or disconnects."""
    webhook_url = os.getenv("AI_CAMERA_ADMIN_BACKEND_URL", "http://127.0.0.1:8011") + "/users/online"

    if not webhook_url:
        return

    with clients_lock:
        current_clients = list(clients.keys())

    payload = {
        "current_client_list": current_clients
    }

    try:
        print(f"[WEBHOOK] Sending '{event_type}' notification for client '{client_id}' to {webhook_url}")
        response = requests.patch(webhook_url, json=payload, timeout=5)
        response.raise_for_status()
        print(f"[WEBHOOK] Notification sent successfully (Status: {response.status_code}).")
    except requests.exceptions.RequestException as e:
        print(f"[WEBHOOK] ERROR: Failed to send notification for client '{client_id}': {e}")


def handle_client(conn, addr) -> None:
    """Handles a single client connection."""
    client_id = None
    client_info_for_webhook = {}
    
    try:
        ident_msg = recv_msg(conn)
        if not ident_msg or ident_msg.get("command") != "identify":
            print(f"[SERVER] Client at {addr} failed to identify. Closing connection.")
            return

        client_info = ident_msg.get("info")
        client_id = client_info.get("activateID") if client_info else None
        
        if not client_id:
            print(f"[SERVER] Client at {addr} sent invalid ID. Closing connection.")
            return

        client_info_for_webhook = client_info

        with clients_lock:
            if client_id in clients:
                print(f"[SERVER] Client '{client_id}' reconnected. Closing old connection.")
                try:
                    clients[client_id]['conn'].close()
                except OSError:
                    pass
            
            clients[client_id] = {"conn": conn, "info": client_info}
            threading.Thread(
                target=notify_webhook_on_client_change,
                args=("client_connected", client_id, client_info),
                daemon=True
            ).start()
        
        print(f"[SERVER] Client '{client_id}' (v{client_info.get('version', 'N/A')}) connected from {addr}.")

        if client_info.get('version', 'N/A') == 'N/A' and client_info.get('activateID', '') != '':
            print(f"[SERVER] Client '{client_id}' is new or unversioned. Triggering an automatic update.")

        # Keep connection alive
        while True:
            if conn.recv(1, socket.MSG_PEEK) == b'':
                break
            time.sleep(5)
            
    except (ConnectionResetError, BrokenPipeError, TypeError, OSError):
        pass
    except Exception as e:
        print(f"[SERVER] Error with client '{client_id}': {e}")
    finally:
        if client_id:
            print(f"[SERVER] Client '{client_id}' disconnected.")
            is_client_removed = False
            with clients_lock:
                if client_id in clients and clients[client_id]['conn'] == conn:
                    del clients[client_id]
                    is_client_removed = True
                
                if is_client_removed:
                    client_last_seen[client_id] = time.time()
            
            if is_client_removed:
                threading.Thread(
                    target=notify_webhook_on_client_change,
                    args=("client_disconnected", client_id, client_info_for_webhook),
                    daemon=True
                ).start()
        conn.close()


def accept_connections(server_socket) -> None:
    """Accept incoming client connections."""
    while True:
        try:
            conn, addr = server_socket.accept()
            threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()
        except OSError:
            break

