# backend/config.py
"""Configuration settings and state management for the backend server."""
from __future__ import annotations

import json
import os
import threading
from typing import Any, Dict

from cryptography.fernet import Fernet
from port_config import SOCKET_PORTS

# --------------------------------------------------------------------------------------
# Server Configuration
# --------------------------------------------------------------------------------------
HOST = '127.0.0.1'
CLIENT_UPDATE_PORT = int(SOCKET_PORTS.get('client_update', 65432))
UPDATE_CONTROL_PORT = int(SOCKET_PORTS.get('update_control', 65433))

# File paths
DLC_MASTER_PATH = 'dlc_master'
MANAGED_MASTER_PATH = 'managed_master'
SERVER_CONFIG_FILE = 'server_config.json'
DLC_STATE_FILE = 'dlc_state.json'

# Client code repo (multi-version update source) + stored version cache.
# Folders live under client_code/:
#   client_code/source   = git clone of the client-code Bitbucket repo
#   client_code/versions = extracted + compiled versions served to clients
MANAGED_SERVICES_REPO_PATH = 'client_code/source'
MANAGED_SERVICES_VERSIONS_CACHE = 'client_code/versions'

# Main-app update payload dir (the frontend pushes this over Socket.IO main_update_*)
CLIENT_UPDATE_MASTER_PATH = os.getenv("AI_CAMERA_ADMIN_CLIENT_UPDATE_MASTER", 'client_update_master')

# Python interpreter used to Cython-compile version payloads. It must be a
# CPython 3.12 build WITH Cython + setuptools installed AND match the OS/arch
# of the branch clients. On the real server this store-time compile step is
# not used (pre-built .so artifacts are produced by the Mac build farm).
CYTHON_PYTHON = os.getenv(
    "AI_CAMERA_ADMIN_CYTHON_PYTHON",
    "/Users/puriphat.bu/Documents/ai-camera-client-secure/python_env/bin/python3",
)

# Frontend base URL (real clients connect to the frontend Socket.IO server)
FRONTEND_BASE_URL = os.getenv("AI_CAMERA_ADMIN_FRONTEND_URL", "http://localhost:8002")

# --------------------------------------------------------------------------------------
# Remote Mac builder (build farm gateway)
# --------------------------------------------------------------------------------------
# Public base URL builders use to reach this backend's /build-farm endpoints.
BACKEND_PUBLIC_URL = os.getenv("AI_CAMERA_ADMIN_BACKEND_PUBLIC_URL", "http://localhost:8011")
# Shared secret builders must present (Socket.IO auth payload + x-builder-key header).
# Resolution order: env var -> backend/.builder_key file -> local default.
def _load_builder_key() -> str:
    val = os.getenv("AI_CAMERA_ADMIN_BUILDER_KEY")
    if val:
        return val
    try:
        key_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".builder_key")
        with open(key_file) as f:
            return f.read().strip()
    except OSError:
        return "local-builder-key"


ADMIN_BUILDER_KEY = _load_builder_key()
# Ed25519 public key used to verify signed build artifacts from the builder.
BUILDER_SIGNING_PUBKEY = os.getenv(
    "AI_CAMERA_ADMIN_BUILDER_PUBKEY",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "builder_signing.pub"),
)
# One-time build jobs + source snapshots handed to remote builders.
BUILD_JOBS_DIR = os.path.join("client_code", "build_jobs")

# Bitbucket managed services source - the client repo that branch machines run.
# Versions are pulled from commits of this repository.
BITBUCKET_REPO_URL = os.getenv("AI_CAMERA_ADMIN_BITBUCKET_URL",
                               "git@bitbucket.org:develop128/ai-camera-client.git")
# Optional sub-folder inside the repo that holds the managed services files.
# Empty string '' means the whole repository tree is a version.
BITBUCKET_MANAGED_SERVICES_SUBDIR = os.getenv("AI_CAMERA_ADMIN_BITBUCKET_SUBDIR", "")
BITBUCKET_DEFAULT_BRANCH = os.getenv("AI_CAMERA_ADMIN_BITBUCKET_BRANCH", "main")

# SQLAlchemy database for the branches table (SQLite by default for local testing)
BRANCH_DB_URL = os.getenv("AI_CAMERA_ADMIN_BRANCH_DB_URL", "sqlite:///branches.db")

# Encryption (must match frontend and client)
# Not in git (mock admin): AI_CAMERA_ADMIN_DLC_KEY from the environment (run_mock_admin.sh).
ENCRYPTION_KEY = os.getenv("AI_CAMERA_ADMIN_DLC_KEY", "").strip().encode()
cipher_suite = Fernet(ENCRYPTION_KEY)

# --------------------------------------------------------------------------------------
# Client State Management
# --------------------------------------------------------------------------------------
clients: Dict[str, Dict[str, Any]] = {}
clients_lock = threading.Lock()


# --------------------------------------------------------------------------------------
# Configuration File Functions
# --------------------------------------------------------------------------------------
def load_server_config() -> Dict[str, Any]:
    """Loads the server's configuration file."""
    try:
        with open(SERVER_CONFIG_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        print(f"[SERVER] WARNING: '{SERVER_CONFIG_FILE}' not found or invalid. Using defaults.")
        return {"version": "0.0.0", "package": "Unknown", "webhook_url": ""}


def save_server_config(config_data: Dict[str, Any]) -> None:
    """Saves the server's configuration file."""
    with open(SERVER_CONFIG_FILE, 'w') as f:
        json.dump(config_data, f, indent=2)


# --------------------------------------------------------------------------------------
# DLC State File Functions
# --------------------------------------------------------------------------------------
def load_dlc_state() -> Dict[str, str]:
    """Loads the last known DLC manifest state from a file."""
    try:
        with open(DLC_STATE_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_dlc_state(manifest_data: Dict[str, str]) -> None:
    """Saves the current DLC manifest state to a file."""
    with open(DLC_STATE_FILE, 'w') as f:
        json.dump(manifest_data, f, indent=2)
