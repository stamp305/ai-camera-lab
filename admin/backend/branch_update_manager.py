# backend/branch_update_manager.py
"""Version-based managed services update push.

Given a branch's assigned Git version (from the branches table), this module
pushes exactly the files of that version to the branch over the raw update
socket (port 65432), encrypting file content with Fernet + Base64.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

import requests

from config import (
    CLIENT_UPDATE_MASTER_PATH,
    FRONTEND_BASE_URL,
    clients,
    clients_lock,
    load_server_config,
)
from data_service.core.database.branch_db import get_session_factory
from data_service.core.database.crud.branch_crud import (
    get_branch,
    set_offline,
    upsert_branch,
)
from managed_manager import update_single_client
from managed_services_manager import ManagedServicesManager

_managed_services_manager = ManagedServicesManager()

_update_in_flight: set = set()
_update_lock = threading.Lock()
_main_update_lock = threading.Lock()


def _get_assigned_version(activation_code: str) -> Optional[str]:
    """Read the assigned Git version for a branch from the database."""
    try:
        with get_session_factory()() as session:
            branch = get_branch(session, activation_code)
            return branch.assigned_version if branch else None
    except Exception as e:
        print(f"[MANAGED SERVICES] Failed to read assigned version for '{activation_code}': {e}")
        return None


def _get_last_version(activation_code: str) -> Optional[str]:
    """The version a branch reported it is running, or None if unknown."""
    try:
        with get_session_factory()() as session:
            branch = get_branch(session, activation_code)
            return branch.last_version if branch else None
    except Exception as e:
        print(f"[MANAGED SERVICES] Failed to read last version for '{activation_code}': {e}")
        return None


def _relative_files(root: str) -> set:
    """Every file under ``root`` as a '/'-separated path relative to it."""
    out = set()
    for dirpath, _dirs, files in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir == ".":
            rel_dir = ""
        for name in files:
            rel = os.path.join(rel_dir, name) if rel_dir else name
            out.add(rel.replace(os.sep, "/"))
    return out


def _shadowed(staged: str) -> Tuple[set, set]:
    """Files on the branch that would override what this tree ships (mock admin).

    Python imports X.so before X.py when both sit in one folder, so switching a
    branch between source and .so leaves the old kind in charge:
    - a source tree ships X.py, and an X.so from an earlier .so build would keep
      running instead — every X.so beside a shipped X.py goes;
    - a .so tree ships X.so, and the X.py of an earlier source push would stay on
      disk as readable source — every X.py the tree no longer carries goes, with
      the __pycache__ next to it (bytecode reads back into source just as well).
    Paths that do not exist on the branch are skipped there (updater.py).
    """
    shipped = _relative_files(staged)
    files, dirs = set(), set()
    for rel in shipped:
        stem, ext = os.path.splitext(rel)
        if ext == ".py" and not rel.startswith("._"):
            files.add(stem + ".so")
        elif ext == ".so":
            if stem + ".py" not in shipped:
                files.add(stem + ".py")
            parent = os.path.dirname(rel)
            dirs.add(f"{parent}/__pycache__" if parent else "__pycache__")
    return files - shipped, dirs


def _write_delete_manifest(staged: str, previous_version: Optional[str]) -> int:
    """Record what the branch must delete, on top of the repo's own list.

    The push only ever copies files over, so anything dropped from the client
    repo stays on every store forever unless something says to remove it. The
    repo ships a hand-written DELETE_MANIFEST.json, which only covers what
    somebody remembered to add — deleting a whole folder left 128 stale files
    on the test machine and would have on all 40 branches.

    Both trees are here, so the diff needs nothing from the branch: whatever
    the version it currently runs contained and this one does not is gone.
    A folder is listed as a folder only when the new tree has no file under it
    at all, which is the case that leaves empty directories behind otherwise.

    Skipped when the branch's current version is unknown or no longer cached —
    a missing baseline must never be read as "the payload dropped everything".
    updater.py re-checks its own protected paths before removing anything.
    """
    path = os.path.join(staged, "DELETE_MANIFEST.json")
    existing = {"files": [], "folders": []}
    try:
        with open(path) as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            existing["files"] = list(loaded.get("files") or [])
            existing["folders"] = list(loaded.get("folders") or [])
    except (OSError, ValueError):
        pass

    shadow_files, shadow_dirs = _shadowed(staged)

    def write(gone_files=(), gone_dirs=()) -> int:
        merged = {
            "files": sorted(set(existing["files"]) | set(gone_files) | shadow_files),
            "folders": sorted(set(existing["folders"]) | set(gone_dirs) | shadow_dirs),
        }
        with open(path, "w") as f:
            json.dump(merged, f)
        print(f"[MANAGED SERVICES] DELETE_MANIFEST: {len(gone_files)} file(s) and "
              f"{len(gone_dirs)} folder(s) dropped since '{previous_version}', plus "
              f"{len(shadow_files)} file(s) / {len(shadow_dirs)} folder(s) the other "
              f"delivery mode would leave in charge")
        return len(gone_files) + len(gone_dirs)

    if not previous_version:
        return write()
    # Read the cache folder directly rather than through get_version_path().
    # That helper *extracts the version from git* when the folder is missing,
    # which does the wrong thing twice here: a branch keeps naming its
    # last_version long after that version is deleted from the registry, and
    # resolving a registry id that is not a git ref raises — taking the whole
    # force-update down with a 500 instead of falling through to the "no
    # baseline" path this check already exists to provide. On the versions it
    # could resolve it would be no better: it would drop an unbuilt plaintext
    # tree into a cache that is only ever supposed to hold compiled ones.
    old_tree = os.path.join(_managed_services_manager.cache_dir, previous_version)
    if not os.path.isdir(old_tree):
        print(f"[MANAGED SERVICES] No cached tree for '{previous_version}' — "
              f"no drop list, only the delivery-mode cleanup.")
        return write()

    # Files the store owns rather than the payload. They are in updater.py's
    # EXCLUSION_LIST, so asking for them would only be refused on the branch —
    # but dropping one from the payload would otherwise put a delete request for
    # the store's own port settings in every update.
    branch_owned = {"client_port_config.py", "client_config.json", "client_version.json"}
    removed = _relative_files(old_tree) - _relative_files(staged) - branch_owned
    if not removed:
        return write()

    new_dirs = {d.split("/", 1)[0] for d in _relative_files(staged) if "/" in d}
    gone_dirs = sorted({r.split("/", 1)[0] for r in removed if "/" in r} - new_dirs)
    covered = tuple(d + "/" for d in gone_dirs)
    gone_files = sorted(r for r in removed if not r.startswith(covered))

    return write(gone_files, gone_dirs)


def _get_version_manifest_safe(version: str) -> Optional[Dict[str, str]]:
    """Return the manifest of a cached Git version, or None on failure."""
    try:
        return _managed_services_manager.get_version_manifest(version)
    except (RuntimeError, ValueError) as e:
        print(f"[MANAGED SERVICES] Cannot resolve version '{version}': {e}")
        return None


def _reject_if_unbuilt(version: str) -> Optional[str]:
    """Return an error message when a version has no build receipt.

    The version cache now only ever receives compiled trees — install_artifact()
    writes them after checking the builder's signature — so an entry without a
    receipt means no build has landed for it at all, not that a half-finished
    one left plaintext behind. Pushing it would send a branch a folder the
    builder never produced.

    This receipt is also the only signature check in the chain that reaches a
    branch: nothing on the branch verifies what it is handed, it trusts this
    server. Refusing here is what keeps that trust honest.
    """
    # Mock admin: a version stored as source ships its plain tree on purpose.
    if _managed_services_manager.get_mode(version) == "source":
        return None
    try:
        import build_farm_gateway as build_gateway

        if build_gateway.is_version_built(version):
            return None
    except Exception as e:
        return f"Cannot confirm version '{version}' was built: {e}"
    return (
        f"Version '{version}' has no build artifact yet — it is plaintext source, "
        f"not a compiled payload. Rebuild it and wait for the builder to finish."
    )


def push_version_update(client_id: str, client_data: Dict[str, Any]) -> Tuple[bool, str]:
    """Push the branch's assigned Git version to it immediately.

    Returns (success, message). If no version is assigned the branch is
    left untouched (so legacy managed_master updates still apply).
    """
    version = _get_assigned_version(client_id)
    if not version:
        msg = f"Branch '{client_id}' has no assigned Git version."
        print(f"[MANAGED SERVICES] {msg}")
        return False, msg

    unbuilt = _reject_if_unbuilt(version)
    if unbuilt:
        print(f"[MANAGED SERVICES] {unbuilt}")
        return False, unbuilt

    manifest = _get_version_manifest_safe(version)
    if manifest is None:
        msg = f"Assigned version '{version}' could not be extracted for '{client_id}'."
        return False, msg

    print(f"\n--- [MANAGED SERVICES] PUSHING VERSION '{version}' TO '{client_id}' ---")
    try:
        server_config = load_server_config()
        server_config['last_update'] = datetime.now().isoformat()
        update_single_client(
            client_id, client_data, manifest, server_config,
            source_path=_managed_services_manager.get_version_path(version),
        )
        print(f"--- [MANAGED SERVICES] PUSH TO '{client_id}' COMPLETE ---")
        return True, f"Version '{version}' pushed to '{client_id}'."
    except Exception as e:
        msg = f"Error pushing version '{version}' to '{client_id}': {e}"
        print(f"[MANAGED SERVICES] {msg}")
        return False, msg


def auto_update_on_identify(client_id: str, client_data: Dict[str, Any],
                            client_version: Optional[str]) -> None:
    """Called after a branch identifies on the raw socket.

    If the branch has an assigned Git version and its reported version does
    not match, push the assigned version files automatically so the branch
    updates itself before starting normally.
    """
    version = _get_assigned_version(client_id)
    if not version:
        return
    if client_version and client_version == version:
        print(f"[MANAGED SERVICES] Branch '{client_id}' already on '{version}'. Skipping.")
        return
    if client_version:
        print(f"[MANAGED SERVICES] Branch '{client_id}' on '{client_version}' != assigned '{version}'. Updating...")
    else:
        print(f"[MANAGED SERVICES] Branch '{client_id}' has no version. Updating to '{version}'...")
    with _update_lock:
        if client_id in _update_in_flight:
            print(f"[MANAGED SERVICES] Update already in flight for '{client_id}'. Skipping duplicate push.")
            return
        _update_in_flight.add(client_id)
    try:
        push_version_update(client_id, client_data)
    finally:
        with _update_lock:
            _update_in_flight.discard(client_id)


def force_update(client_id: str) -> Tuple[bool, str]:
    """Force an immediate version push to one connected branch.

    Tries both update channels:
    - Raw update socket (legacy mock clients).
    - Frontend Socket.IO main_update_* push (real clients).
    """
    results = []

    with clients_lock:
        client_data = clients.get(client_id)
    if client_data is not None:
        success, msg = push_version_update(client_id, client_data)
        results.append((success, msg))

    success, msg = push_via_frontend_main_update(client_id)
    results.append((success, msg))

    ok = any(success for success, _ in results)
    message = "; ".join(msg for _, msg in results)
    return ok, message


def _sync_version_to_client_update_master(version: str) -> Tuple[bool, str]:
    """Replace the main-update payload dir with the assigned version's tree."""
    dst = CLIENT_UPDATE_MASTER_PATH
    try:
        src = _managed_services_manager.get_version_path(version)
        if not os.path.isdir(src):
            return False, f"Version '{version}' cache missing at '{src}'."
        os.makedirs(dst, exist_ok=True)
        for item in os.listdir(dst):
            if item == ".git":
                continue
            p = os.path.join(dst, item)
            if os.path.isdir(p):
                shutil.rmtree(p)
            else:
                os.remove(p)
        for item in os.listdir(src):
            if item == ".git":
                continue
            s = os.path.join(src, item)
            d = os.path.join(dst, item)
            if os.path.isdir(s):
                shutil.copytree(s, d, dirs_exist_ok=True)
            else:
                shutil.copy2(s, d)
        with open(os.path.join(dst, "client_version.json"), "w") as f:
            json.dump({"version": version}, f)
        # The version tree carries the client repo's own VERSION file, and
        # push_main_update stamps the branch from that file — so without this
        # the branch reports the repo's version instead of the assigned one,
        # never matches, and gets pushed the same update on every reconnect.
        with open(os.path.join(dst, "VERSION"), "w") as f:
            f.write(f"{version}\n")
        print(f"[MANAGED SERVICES] Synced version '{version}' into '{dst}'.")
        return True, f"Synced version '{version}' into client_update_master."
    except Exception as e:
        msg = f"Failed syncing version '{version}' into client_update_master: {e}"
        print(f"[MANAGED SERVICES] {msg}")
        return False, msg


def push_via_frontend_main_update(client_id: str) -> Tuple[bool, str]:
    """Push the branch's assigned Git version to a real (Socket.IO) client.

    The version tree is synced into ``client_update_master``, then the
    frontend is asked to run its main_update_* push for that branch.
    """
    version = _get_assigned_version(client_id)
    if not version:
        msg = f"Branch '{client_id}' has no assigned Git version."
        print(f"[MANAGED SERVICES] {msg}")
        return False, msg

    unbuilt = _reject_if_unbuilt(version)
    if unbuilt:
        print(f"[MANAGED SERVICES] {unbuilt}")
        return False, unbuilt

    with _main_update_lock:
        ok, msg = _sync_version_to_client_update_master(version)
        if not ok:
            return False, msg
        # Staged tree in hand: work out what this branch has to drop before the
        # frontend starts shipping files, since the payload carries the list.
        _write_delete_manifest(CLIENT_UPDATE_MASTER_PATH, _get_last_version(client_id))
        print(f"[MANAGED SERVICES] Triggering frontend main-update push for '{client_id}' (version '{version}') ...")
        try:
            resp = requests.post(
                f"{FRONTEND_BASE_URL}/ai-camera/api/force-main-update/{client_id}",
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            success = bool(data.get("success"))
            message = data.get("message") or data.get("error") or f"HTTP {resp.status_code}"
            print(f"[MANAGED SERVICES] Frontend push result for '{client_id}': {message}")
            return success, message
        except requests.exceptions.RequestException as e:
            msg = f"Failed to trigger frontend update for '{client_id}': {e}"
            print(f"[MANAGED SERVICES] {msg}")
            return False, msg


def force_update_all() -> Tuple[bool, str]:
    """Force an immediate version push to every connected branch."""
    with clients_lock:
        connected = dict(clients)
    if not connected:
        return False, "No clients connected."

    results = []
    for client_id, client_data in connected.items():
        success, msg = push_version_update(client_id, client_data)
        results.append((client_id, success, msg))

    ok = sum(1 for _, s, _ in results if s)
    return ok > 0, f"Pushed to {ok}/{len(results)} client(s)."


def register_branch_identify(activation_code: str, client_info: Optional[Dict[str, Any]]) -> None:
    """Upsert the branch row when a branch connects to the raw socket."""
    try:
        with get_session_factory()() as session:
            upsert_branch(
                session,
                activation_code=activation_code,
                branch_name=client_info.get("branchName") if client_info else None,
                client_version=client_info.get("version") if client_info else None,
                serial_number=client_info.get("serial") if client_info else None,
                mac_address=client_info.get("mac") if client_info else None,
                online=True,
            )
    except Exception as e:
        print(f"[MANAGED SERVICES] Failed to register branch '{activation_code}': {e}")


def mark_branch_offline(activation_code: str) -> None:
    """Mark a branch as offline when its socket disconnects."""
    try:
        with get_session_factory()() as session:
            set_offline(session, activation_code)
    except Exception as e:
        print(f"[MANAGED SERVICES] Failed to mark branch '{activation_code}' offline: {e}")


def start_auto_update(client_id: str, client_version: Optional[str]) -> None:
    """Spawn a background thread to auto-update a branch after identify."""
    with clients_lock:
        if client_id not in clients:
            return
        client_data = clients[client_id]
    threading.Thread(
        target=auto_update_on_identify,
        args=(client_id, client_data, client_version),
        daemon=True,
    ).start()
