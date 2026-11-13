# backend/dlc_manager.py
"""DLC (Downloadable Content) manifest and file operations."""
from __future__ import annotations

import base64
import hashlib
import os
from typing import Any, Dict, List, Optional, Tuple

from config import DLC_MASTER_PATH, cipher_suite, load_server_config
from socket_utils import send_msg, recv_msg


def list_dlc_folders(path: str = DLC_MASTER_PATH) -> List[str]:
    """Scans the given path and returns a list of top-level directory names."""
    if not os.path.exists(path):
        print(f"[SERVER] WARNING: DLC master path '{path}' does not exist.")
        return []
    
    try:
        folders = [name for name in os.listdir(path)
                   if os.path.isdir(os.path.join(path, name))]
        return folders
    except OSError as e:
        print(f"[SERVER] ERROR: Could not read directory '{path}': {e}")
        return []


def get_file_hash(filepath: str) -> str:
    """Calculate SHA256 hash of a file."""
    sha256_hash = hashlib.sha256()
    with open(filepath, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()


# ไฟล์/โฟลเดอร์ที่ต้องไม่เข้า manifest ทั้งฝั่ง master และฝั่งสาขา
# (ฝั่งสาขาใช้ชุดเดียวกันใน client.py get_client_manifest())
#
# .DS_Store เป็นตัวที่อันตรายที่สุด: Finder เขียนทับมันเองทุกครั้งที่เปิดโฟลเดอร์
# ⇒ hash เปลี่ยนเรื่อย ๆ ฝั่ง master จึงส่งไฟล์เดิมซ้ำทุกรอบ และถ้าเครื่องสาขา
# สร้างของตัวเองขึ้นมาในโฟลเดอร์ที่ master ไม่มี มันจะเข้าเงื่อนไข
# (client_files - master_files) ⇒ สั่ง delete_file ทุกรอบ วนไม่จบ
#
# tests/ และ conftest.py เป็นเทสต์ของนักพัฒนา ไม่ใช่โค้ดที่สาขารัน ⇒ ไม่ส่งไปสาขา
# (ฝั่งสาขายังนับมันอยู่ ไฟล์เทสต์ที่เคยส่งไปแล้วจึงโดน delete_file หนึ่งรอบ — ตั้งใจ)
# ห้ามวางโค้ดที่ service ใช้จริงไว้ใต้โฟลเดอร์ชื่อ tests
_MANIFEST_SKIP_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini", "conftest.py"}
_MANIFEST_SKIP_DIRS = {"__pycache__", ".git", "tests"}
_MANIFEST_SKIP_SUFFIXES = (".pyc", ".pyo")


def is_manifest_junk(relative_path: str) -> bool:
    """True ถ้า path นี้เป็นขยะของ OS/ตัวแปล หรือไฟล์เทสต์ ไม่ใช่ payload ที่ต้องส่งให้สาขา."""
    parts = relative_path.replace(os.sep, "/").split("/")
    if any(part in _MANIFEST_SKIP_DIRS for part in parts[:-1]):
        return True
    name = parts[-1]
    return name in _MANIFEST_SKIP_NAMES or name.endswith(_MANIFEST_SKIP_SUFFIXES)


def get_master_manifest(path: str = DLC_MASTER_PATH) -> Dict[str, str]:
    """Build manifest of all DLC files with their hashes."""
    manifest = {}
    if not os.path.exists(path):
        return manifest
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = [d for d in dirnames if d not in _MANIFEST_SKIP_DIRS]
        for filename in filenames:
            full_path = os.path.join(dirpath, filename)
            relative_path = os.path.relpath(full_path, path)
            manifest_key = relative_path.replace(os.sep, '/')
            if is_manifest_junk(manifest_key):
                continue
            manifest[manifest_key] = get_file_hash(full_path)
    return manifest


def update_single_client(client_id: str, client_data: Dict[str, Any], 
                         master_manifest: Dict[str, str], server_config: Dict[str, Any]) -> None:
    """Push updates to a single client."""
    client_sock = client_data['conn']
    try:
        print(f"\n[Updating Client: {client_id}]")
        send_msg(client_sock, {"command": "check_for_updates"})
        client_manifest = recv_msg(client_sock)
        if client_manifest is None:
            print(f"  -> Failed to receive manifest. Client may have disconnected.")
            return

        master_files = set(master_manifest.keys())
        client_files = set(client_manifest.keys())
        update_count = 0

        delete_command_template = {"command": "delete_file", "server_config": server_config}
        add_command_template = {"command": "add_file", "server_config": server_config}

        for filename in (client_files - master_files):
            print(f"  -> Telling to DELETE: {filename}")
            delete_command = delete_command_template.copy()
            delete_command["filename"] = filename
            send_msg(client_sock, delete_command)
            update_count += 1

        for filename in master_files:
            if filename not in client_manifest or client_manifest[filename] != master_manifest[filename]:
                print(f"  -> Telling to ADD/UPDATE: {filename}")
                filepath = os.path.join(DLC_MASTER_PATH, filename.replace('/', os.sep))
                with open(filepath, 'rb') as f:
                    content = f.read()
                
                encrypted_content = cipher_suite.encrypt(content)
                content_b64 = base64.b64encode(encrypted_content).decode('utf-8')
                
                add_command = add_command_template.copy()
                add_command.update({
                    "filename": filename,
                    "content_b64": content_b64,
                    "hash": master_manifest[filename]
                })
                send_msg(client_sock, add_command)
                update_count += 1
        
        if update_count == 0:
            print("  -> Client is already up to date.")

    except (ConnectionResetError, BrokenPipeError, TypeError):
        print(f"  -> Client disconnected during update.")
    except Exception as e:
        print(f"  -> An error occurred while updating client {client_id}: {e}")


def update_single_client_folder_dlc(client_id: str, client_data: Dict[str, Any],
                                     master_manifest: Dict[str, str], server_config: Dict[str, Any],
                                     target_folders: List[str]) -> Tuple[bool, str]:
    """Updates a specific list of folders for a single client."""
    client_sock = client_data['conn']
    
    # Filter the master manifest to only include files in target_folders
    filtered_master_manifest = {}
    for filepath, filehash in master_manifest.items():
        if any(filepath.startswith(f"{folder}/") or filepath == folder for folder in target_folders):
            filtered_master_manifest[filepath] = filehash

    if not filtered_master_manifest:
        msg = f"No files found on server for specified folders: {target_folders}"
        print(f"  -> {msg}")
        return False, msg

    try:
        print(f"\n[Updating Folders for Client: {client_id}]")
        print(f"  -> Target folders: {target_folders}")
        send_msg(client_sock, {"command": "check_for_updates"})
        client_manifest = recv_msg(client_sock)
        if client_manifest is None:
            msg = "Failed to receive manifest. Client may have disconnected."
            print(f"  -> {msg}")
            return False, msg

        # Determine which files to add, update, or delete within the scope of target_folders
        master_files_in_scope = set(filtered_master_manifest.keys())
        client_files_in_scope = set()
        for filepath in client_manifest.keys():
            if any(filepath.startswith(f"{folder}/") or filepath == folder for folder in target_folders):
                client_files_in_scope.add(filepath)

        files_to_delete = client_files_in_scope - master_files_in_scope
        files_to_add_update = master_files_in_scope
        
        update_count = 0

        # Send delete commands
        for filename in files_to_delete:
            print(f"  -> Telling to DELETE: {filename}")
            send_msg(client_sock, {
                "command": "delete_file",
                "filename": filename,
                "server_config": server_config
            })
            update_count += 1

        # Send add/update commands
        for filename in files_to_add_update:
            if filename not in client_manifest or client_manifest[filename] != filtered_master_manifest[filename]:
                print(f"  -> Telling to ADD/UPDATE: {filename}")
                filepath = os.path.join(DLC_MASTER_PATH, filename.replace('/', os.sep))
                with open(filepath, 'rb') as f:
                    content = f.read()
                
                encrypted_content = cipher_suite.encrypt(content)
                content_b64 = base64.b64encode(encrypted_content).decode('utf-8')
                
                send_msg(client_sock, {
                    "command": "add_file",
                    "filename": filename,
                    "content_b64": content_b64,
                    "hash": filtered_master_manifest[filename],
                    "server_config": server_config
                })
                update_count += 1
        
        if update_count == 0:
            print("  -> Client's target folders are already up to date.")
            return True, f"Client '{client_id}' is already up to date for folders: {target_folders}."
        else:
            return True, f"Successfully pushed {update_count} file changes to client '{client_id}'."

    except (ConnectionResetError, BrokenPipeError, TypeError) as e:
        msg = f"Client disconnected during update: {e}"
        print(f"  -> {msg}")
        return False, msg
    except Exception as e:
        msg = f"An error occurred while updating client {client_id}: {e}"
        print(f"  -> {msg}")
        return False, msg
