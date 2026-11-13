# backend/server.py
"""AI Camera Admin Backend Server - Entry Point.

This is the main server file that orchestrates:
- Client socket connections
- API socket listener
- DLC folder monitoring
- FastAPI REST API
- Admin CLI
"""
from __future__ import annotations

import os
import socket
import threading

import socketio
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from port_config import API_PORTS
from config import (
    HOST,
    CLIENT_UPDATE_PORT,
    DLC_MASTER_PATH,
    MANAGED_SERVICES_REPO_PATH,
    MANAGED_SERVICES_VERSIONS_CACHE,
    SERVER_CONFIG_FILE,
    save_server_config,
)
from client_handler import accept_connections, monitor_clients_offline_status
from api_handler import start_api_listener
from dlc_monitor import monitor_dlc_folder_changes
from dlc_manager import list_dlc_folders
from admin_cli import run_admin_menu

# Remote Mac builder gateway (Socket.IO /build + one-time build jobs)
import build_farm_gateway as build_gateway

# Traffic feeds branch clients send (mounted below)
from ai_camera_service.app import app as ai_camera_app
# Camera health reports and alert events; the server sends the LINE alerts
import camera_alert_service.app as camera_alert
# Camera Placement: a Branch asks whether one camera films inside or outside the shop
import camera_placement_service.app as camera_placement

# Import data service routers
from data_service.api import users as ds_users, services as ds_services, auth as ds_auth, packages as ds_packages
from data_service.api.branches import router as ds_branches
from data_service.api.dummy_data import router as ds_dummy_data
from data_service.api.exception_handlers import register_exception_handlers
from data_service.core.database.branch_db import init_branches_db
from data_service.core.database.crud.auth_crud import add_device_key_hash_column_if_missing


# --------------------------------------------------------------------------------------
# FastAPI Application
# --------------------------------------------------------------------------------------
fastapi_app = FastAPI(title="Backend Utility API", version="1.0.0")
fastapi_app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register exception handlers for consistent error responses
register_exception_handlers(fastapi_app)

# Register data service routers
fastapi_app.include_router(ds_users.router)
fastapi_app.include_router(ds_services.router)
fastapi_app.include_router(ds_auth.router)
fastapi_app.include_router(ds_packages.router)
fastapi_app.include_router(ds_branches)
fastapi_app.include_router(ds_dummy_data)

# Build farm gateway endpoints (/build-farm/*) + ASGI app that also serves
# Socket.IO on the default /socket.io path (namespace /build).
fastapi_app.include_router(build_gateway.router)

# Traffic feeds (ADR 0009). Mounted, not included, so the app keeps its own 400 handler.
# Branches reach it only through the frontend's Device Key gate.
fastapi_app.mount("/ai-camera-service", ai_camera_app)
# Same gate, same way in: frontend /ai-camera/api/camera-alert/<feed>.
fastapi_app.mount("/camera-alert-service", camera_alert.app)
# Same gate again: frontend /ai-camera/api/camera-placement.
fastapi_app.mount("/camera-placement-service", camera_placement.app)


@fastapi_app.on_event("startup")
async def _capture_asgi_loop():
    """Let sync code (publish_to_farm) emit Socket.IO events thread-safely."""
    import asyncio
    build_gateway.set_event_loop(asyncio.get_running_loop())


asgi_app = socketio.ASGIApp(build_gateway.sio, other_asgi_app=fastapi_app)


@fastapi_app.get("/dlc-folders", summary="List DLC folders")
def get_dlc_folders():
    """Return list of available DLC folders."""
    return sorted(list_dlc_folders(DLC_MASTER_PATH))


# --------------------------------------------------------------------------------------
# Main Server Logic
# --------------------------------------------------------------------------------------
def main():
    """Main entry point for the server."""
    # Initialize config file if not exists
    if not os.path.exists(SERVER_CONFIG_FILE):
        save_server_config({
            "version": "1.0.0",
            "package": "Base",
            "last_update": "",
            "webhook_url": ""
        })

    # Ensure DLC master directory exists
    os.makedirs(DLC_MASTER_PATH, exist_ok=True)

    # Initialize branches table and Git managed services directories
    init_branches_db()
    camera_alert.init()
    try:
        add_device_key_hash_column_if_missing()
    except Exception as e:  # users DB unreachable — Activation answers 500 until the column exists
        print(f"[USERS] Could not ensure users.device_key_hash: {e}")
    os.makedirs(MANAGED_SERVICES_REPO_PATH, exist_ok=True)
    os.makedirs(MANAGED_SERVICES_VERSIONS_CACHE, exist_ok=True)
    from managed_services_manager import ManagedServicesManager
    ManagedServicesManager().ensure_remote()
    ManagedServicesManager().ensure_default_latest()

    from data_service.core.services.pipeline_service import start_loop
    start_loop()
    
    # Start client socket server
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind((HOST, CLIENT_UPDATE_PORT))
    server_socket.listen()
    print(f"[SERVER] Listening for clients on {HOST}:{CLIENT_UPDATE_PORT}")

    # Start background threads
    accept_thread = threading.Thread(target=accept_connections, args=(server_socket,), daemon=True)
    accept_thread.start()

    offline_monitor_thread = threading.Thread(target=monitor_clients_offline_status, daemon=True)
    offline_monitor_thread.start()

    api_thread = threading.Thread(target=start_api_listener, daemon=True)
    api_thread.start()

    monitor_thread = threading.Thread(target=monitor_dlc_folder_changes, daemon=True)
    monitor_thread.start()

    # Start FastAPI (+ Socket.IO /build gateway) in a separate thread
    def run_fastapi():
        try:
            # 127.0.0.1: nginx and the frontend are on this box; any other caller would
            # skip the frontend's gate (ADR 0009).
            uvicorn.run(asgi_app, host="127.0.0.1", port=int(API_PORTS["data_service"]), log_level="info")
        except Exception as e:
            print(f"[FASTAPI] Failed to start embedded API server: {e}")

    run_fastapi()

if __name__ == '__main__':
    main()