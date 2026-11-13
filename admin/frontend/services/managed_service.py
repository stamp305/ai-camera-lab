# frontend/services/managed_service.py
"""Managed services file handling, encryption, and manifest building.

Kept as a thin wrapper over ``dlc_service`` so the version-manager feature
keeps its original "managed" naming while sharing the single client registry
and encryption suite with the existing DLC feature.
"""
from __future__ import annotations

import hashlib
import os
from typing import Dict, Optional

from flask import current_app

from config import Config

# Re-export the shared client registry + encryption so branch connections
# registered by socket_handlers are visible to the version-manager API too.
from services.dlc_service import (
    cipher_suite,
    update_clients,
    update_clients_lock,
)
from services.dlc_service import dlc_jobs, dlc_jobs_lock

__all__ = [
    "cipher_suite",
    "update_clients",
    "update_clients_lock",
    "dlc_jobs",
    "dlc_jobs_lock",
    "file_hash",
    "build_master_manifest",
    "read_master_file",
]


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
    """Return mapping of relative_path -> sha256 for all files under MANAGED_MASTER_PATH."""
    managed_path = Config.MANAGED_MASTER_PATH
    manifest: Dict[str, str] = {}
    if not os.path.isdir(managed_path):
        return manifest
    base_len = len(managed_path.rstrip(os.sep)) + 1
    for root, _dirs, files in os.walk(managed_path):
        for fname in files:
            full = os.path.join(root, fname)
            rel = full[base_len:].replace(os.sep, "/")
            try:
                manifest[rel] = file_hash(full)
            except Exception:
                current_app.logger.warning(f"[MANAGED] Could not hash {full}")
    return manifest


def read_master_file(rel_path: str) -> Optional[bytes]:
    """Read a file from the managed master directory securely."""
    managed_path = Config.MANAGED_MASTER_PATH
    full = os.path.join(managed_path, rel_path)
    full = os.path.normpath(full)
    if not full.startswith(managed_path):  # directory traversal guard
        return None
    try:
        with open(full, "rb") as f:
            return f.read()
    except Exception as exc:
        current_app.logger.error(f"[MANAGED] Failed reading {rel_path}: {exc}")
        return None