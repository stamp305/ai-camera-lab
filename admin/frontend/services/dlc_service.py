# frontend/services/dlc_service.py
"""DLC (Downloadable Content) file handling, encryption, and manifest building."""
from __future__ import annotations

import hashlib
import os
import threading
from typing import Any, Dict, Optional

from cryptography.fernet import Fernet
from flask import current_app

from config import Config

# -------------------------------------------------------------------------------------
# Encryption
# -------------------------------------------------------------------------------------
cipher_suite = Fernet(Config.ENCRYPTION_KEY)


# -------------------------------------------------------------------------------------
# File Operations
# -------------------------------------------------------------------------------------
def file_hash(path: str) -> str:
    """Calculate SHA256 hash of a file."""
    sha = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha.update(chunk)
    return sha.hexdigest()


def build_master_manifest() -> Dict[str, str]:
    """Return mapping of relative_path -> sha256 for all files under DLC_MASTER_PATH."""
    dlc_path = Config.DLC_MASTER_PATH
    manifest: Dict[str, str] = {}
    if not os.path.isdir(dlc_path):
        return manifest
    base_len = len(dlc_path.rstrip(os.sep)) + 1
    for root, _dirs, files in os.walk(dlc_path):
        for fname in files:
            full = os.path.join(root, fname)
            rel = full[base_len:].replace(os.sep, "/")
            try:
                manifest[rel] = file_hash(full)
            except Exception:
                current_app.logger.warning(f"[DLC] Could not hash {full}")
    return manifest


def read_master_file(rel_path: str) -> Optional[bytes]:
    """Read a file from the DLC master directory securely."""
    dlc_path = Config.DLC_MASTER_PATH
    full = os.path.join(dlc_path, rel_path)
    full = os.path.normpath(full)
    if not full.startswith(dlc_path):  # directory traversal guard
        return None
    try:
        with open(full, "rb") as f:
            return f.read()
    except Exception as exc:
        current_app.logger.error(f"[DLC] Failed reading {rel_path}: {exc}")
        return None


# -------------------------------------------------------------------------------------
# Client Tracking State
# -------------------------------------------------------------------------------------
update_clients: Dict[str, Dict[str, Any]] = {}
update_clients_lock = threading.Lock()


# -------------------------------------------------------------------------------------
# DLC Job Tracking State
# -------------------------------------------------------------------------------------
dlc_jobs: Dict[str, Dict[str, Any]] = {}
dlc_jobs_lock = threading.Lock()
