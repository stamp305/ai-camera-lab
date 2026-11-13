# data_service/core/services/branch_service.py
"""Business logic for the branches (client machines) feature.

Combines the SQLAlchemy branches table with the Git managed services version manager so
each branch can be assigned an independent Git version and force-updated.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from data_service.core.database.branch_db import get_session_factory
from data_service.core.database.crud.branch_crud import (
    assign_version as crud_assign_version,
)
from data_service.core.database.crud.branch_crud import (
    create_branch as crud_create_branch,
)
from data_service.core.database.crud.branch_crud import (
    get_branch as crud_get_branch,
)
from data_service.core.database.crud.branch_crud import (
    list_branches as crud_list_branches,
)
from data_service.core.database.crud.user_crud import read_all_users_from_db
from config import BUILD_JOBS_DIR
from managed_services_manager import ManagedServicesManager

_managed_services_manager = ManagedServicesManager()


def _branch_to_dict(branch) -> Dict[str, Any]:
    """Serialize a Branch ORM object to a dictionary."""
    return branch.to_dict()


def sync_branches_from_users() -> None:
    """Mirror the user list (รายการผู้ใช้ทั้งหมด) into the branches table.

    The branches table only ever gained rows an admin created by hand, so the
    version manager showed "0 รายการ" even though every store is already
    registered as a user. Users are the source of truth for *which* branches
    exist (activation code, name, serial, MAC, online flag); the branches
    table stays the source of truth for the version columns, which is why
    rows are updated in place instead of recreated.
    """
    try:
        users = read_all_users_from_db()
    except Exception as e:  # users DB unreachable — serve whatever we already have
        print(f"[BRANCHES] Could not read users for branch sync: {e}")
        return

    codes = set()
    with get_session_factory()() as session:
        for user in users:
            code = (user.get("activation_code") or "").strip()
            if not code:
                continue
            codes.add(code)
            branch = crud_get_branch(session, code)
            if branch is None:
                branch = crud_create_branch(session, code, user.get("branch_name"))
            branch.branch_name = user.get("branch_name") or branch.branch_name
            branch.serial_number = user.get("serial_number") or branch.serial_number
            branch.mac_address = user.get("mac_address") or branch.mac_address
            branch.status = "active" if user.get("is_online") else "offline"
            # last_version is deliberately not taken from users.version: that
            # column holds the package/service version, not the client app
            # version. It is filled by report_identify() when the branch
            # connects and tells us what it is actually running.
            session.add(branch)

        # A user removed from the user list is no longer a branch.
        for branch in crud_list_branches(session):
            if branch.activation_code not in codes:
                session.delete(branch)
        session.commit()


def list_branches() -> List[Dict[str, Any]]:
    """Return all branches, mirroring the user list first."""
    sync_branches_from_users()
    with get_session_factory()() as session:
        return [_branch_to_dict(b) for b in crud_list_branches(session)]


def report_identify(activation_code: str, version: Optional[str] = None,
                    branch_name: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Record that a branch just connected and what version it reports.

    Called by the Socket.IO ``identify`` handler. Returns the branch (so the
    caller can read ``assigned_version``), or None when the activation code
    is not a known user.
    """
    with get_session_factory()() as session:
        branch = crud_get_branch(session, activation_code)
        if branch is None:
            # First contact since the user was added — pull the user list in.
            sync_branches_from_users()
            branch = crud_get_branch(session, activation_code)
        if branch is None:
            return None
        branch.status = "active"
        branch.last_online_at = datetime.now()
        if version:
            branch.last_version = version
        if branch_name and not branch.branch_name:
            branch.branch_name = branch_name
        session.add(branch)
        session.commit()
        session.refresh(branch)
        return _branch_to_dict(branch)


def get_branch(activation_code: str) -> Optional[Dict[str, Any]]:
    """Return a single branch by activation code, or None."""
    with get_session_factory()() as session:
        branch = crud_get_branch(session, activation_code)
        return _branch_to_dict(branch) if branch else None


def create_branch(activation_code: str, branch_name: Optional[str] = None) -> Dict[str, Any]:
    """Create a new branch."""
    with get_session_factory()() as session:
        branch = crud_create_branch(session, activation_code, branch_name)
        return _branch_to_dict(branch)


class UnknownVersionError(ValueError):
    """The version asked for is not in the registry."""


def assign_version(activation_code: str, version: str) -> Optional[Dict[str, Any]]:
    """Assign a Git version to a branch; None if the branch does not exist.

    The assignment is a plain string in the branches table with nothing tying
    it to the version registry, so any typo used to be stored happily and only
    surfaced later as "has no build artifact yet" when someone pressed update.
    Deleting a version left the branches pointing at it in the same state.
    Refusing an id the registry does not know keeps new dead pointers out; the
    ones already stored are a separate cleanup.
    """
    known = _managed_services_manager.get_registry()
    if version not in known:
        raise UnknownVersionError(
            f"ไม่มีเวอร์ชัน '{version}' ในทะเบียน — มีอยู่: {', '.join(sorted(known)) or '(ยังไม่มีเลย)'}"
        )
    with get_session_factory()() as session:
        branch = crud_assign_version(session, activation_code, version)
        return _branch_to_dict(branch) if branch else None


def force_update(activation_code: str) -> Dict[str, Any]:
    """Force an immediate version push to a connected branch.

    Skipped (with a subtle notice) when the assigned version is already the
    version the branch is currently running, so admins are not spammed with
    pointless re-pushes.
    """
    branch = get_branch(activation_code)
    if branch is None:
        return {"success": False, "message": f"ไม่พบสาขา '{activation_code}'"}
    assigned = branch.get("assigned_version")
    last = branch.get("last_version")
    if assigned and last and assigned == last:
        return {
            "success": True,
            "skipped": True,
            "message": f"สาขา '{activation_code}' ใช้เวอร์ชัน '{last}' ตรงกับที่กำหนดอยู่แล้ว",
        }
    from branch_update_manager import force_update as push_now

    success, message = push_now(activation_code)
    return {"success": success, "message": message}


def list_git_versions() -> List[Dict[str, Any]]:
    """List available Git versions (tags + commits) from the managed services repo."""
    return _managed_services_manager.list_versions()


# -------------------------------------------------------------------------------------
# Bitbucket commits (version source = ai-camera-client repo)
# -------------------------------------------------------------------------------------
def list_bitbucket_branches() -> List[str]:
    """List remote branch names from the Bitbucket managed services repo."""
    return _managed_services_manager.list_remote_branches()


def list_bitbucket_commits(branch: Optional[str] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """List recent commits of a Bitbucket branch (author/date/message + stored flag)."""
    return _managed_services_manager.list_commits(branch, limit=limit)


def store_bitbucket_versions(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Store chosen Bitbucket commits as local versions.

    Each item: {commit, version_id?, label?, bump?}. ``version_id`` defaults
    to the short commit hash unless the admin supplied a custom one. When
    ``bump`` ('major'/'minor'/'bug') is given the version number is generated
    automatically: commits are numbered oldest-first by commit date and each
    bumps forward from the highest version already stored.
    """
    for item in items:
        if not item.get("commit"):
            raise ValueError("Each item needs a 'commit'.")
        bump = item.get("bump")
        if bump is not None and bump not in ("major", "minor", "bug"):
            raise ValueError("bump ต้องเป็นหนึ่งใน major, minor, bug เท่านั้น")

    stored = []
    for item in items:
        if item.get("bump"):
            continue
        stored.append(
            _managed_services_manager.store_version(
                item["commit"],
                version_id=item.get("version_id"),
                label=item.get("label"),
                mode=item.get("mode") or "so",
            )
        )

    auto_items = [i for i in items if i.get("bump")]
    if auto_items:
        dated = sorted(
            ((_managed_services_manager.commit_date(i["commit"]) or "", i) for i in auto_items),
            key=lambda t: t[0],
        )
        for _, item in dated:
            vid = _managed_services_manager.generate_version(item["bump"])
            stored.append(_managed_services_manager.store_version(
                item["commit"], version_id=vid, label=vid, mode=item.get("mode") or "so"))
    return stored


def set_version_mode(version_id: str, mode: str) -> Dict[str, Any]:
    """Mock admin: switch a stored version between source and .so delivery."""
    return _managed_services_manager.set_mode(version_id, mode)


def delete_version(version_id: str) -> None:
    """Remove a stored version (registry + cache folder)."""
    _managed_services_manager.delete_version(version_id)


def pending_version_for(branch: str) -> Optional[Dict[str, Any]]:
    """What a branch is running versus what it has been assigned.

    The branch asks with the store id it knows about itself, which is not
    necessarily its activation code, so every identifier the admin stores is
    accepted. Returns None when nothing matches.
    """
    needle = (branch or "").strip().lower()
    if not needle:
        return None
    with get_session_factory()() as session:
        for row in crud_list_branches(session):
            for field in (row.activation_code, row.branch_name, row.serial_number):
                if field and str(field).strip().lower() == needle:
                    return {
                        "branch": row.activation_code,
                        "current_version": row.last_version,
                        "pending_version": row.assigned_version,
                    }
    return None


def rebuild_version(version_id: str) -> Dict[str, Any]:
    """Order the build farm to rebuild a stored version.

    Names the version's commit to the connected builders, which archive it out
    of their own clone and send back compiled .so. Nothing is extracted here.
    Raises ValueError when the version is not stored.
    """
    registry = _managed_services_manager.get_registry()
    if version_id not in registry:
        raise ValueError(f"Version '{version_id}' is not stored.")
    import build_farm_gateway as build_gateway

    commit = _managed_services_manager.publish_to_farm(
        version_id, registry[version_id].get("commit")
    )
    # With no builder on /build nothing happens at all, so say that rather than
    # reporting a build that will never start.
    builders = build_gateway.connected_builder_count()
    return {
        "version": version_id,
        "commit": commit,
        "notified": builders > 0,
        "builders": builders,
    }


def rename_version(old_id: str, new_id: str) -> Dict[str, Any]:
    """Rename a stored version and keep branch references in sync.

    Any branch whose ``assigned_version`` / ``last_version`` pointed at the
    old id is updated to the new id so the admin UI never shows a dangling
    version reference.
    """
    result = _managed_services_manager.rename_version(old_id, new_id)
    with get_session_factory()() as session:
        for branch in crud_list_branches(session):
            changed = False
            if branch.assigned_version == old_id:
                branch.assigned_version = new_id
                changed = True
            if branch.last_version == old_id:
                branch.last_version = new_id
                changed = True
            if changed:
                session.add(branch)
        session.commit()
    return result


def delete_branch(activation_code: str) -> bool:
    """Delete a branch row; returns True when it existed."""
    with get_session_factory()() as session:
        branch = crud_get_branch(session, activation_code)
        if branch is None:
            return False
        session.delete(branch)
        session.commit()
        return True
