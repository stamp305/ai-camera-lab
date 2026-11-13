# data_service/api/branches.py
"""Branch and Git managed services version API endpoints."""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from data_service.core.services import branch_service

router = APIRouter(prefix="/branches", tags=["branches"])


class BranchCreateRequest(BaseModel):
    """Payload for creating a branch."""
    activation_code: str
    branch_name: Optional[str] = None


class AssignVersionRequest(BaseModel):
    """Payload for assigning a Git version to a branch."""
    version: str


class IdentifyReportRequest(BaseModel):
    """What a branch reported when it connected."""
    version: Optional[str] = None
    branch_name: Optional[str] = None


class StoreVersionItem(BaseModel):
    """One Bitbucket commit to store as a local version."""
    commit: str
    version_id: Optional[str] = None
    label: Optional[str] = None
    bump: Optional[str] = None
    # Mock admin: "so" (build farm, the default) or "source" (plain .py).
    mode: str = "so"


class StoreVersionsRequest(BaseModel):
    """Payload for storing multiple Bitbucket commits as versions."""
    items: List[StoreVersionItem]


class VersionModeRequest(BaseModel):
    """Mock admin: how a stored version ships — "so" or "source"."""
    mode: str


class RenameVersionRequest(BaseModel):
    """Payload for renaming a stored version."""
    new_id: str


# =============================================================
# Branch Endpoints
# =============================================================
@router.get("")
def get_all_branches():
    """List all branches."""
    return branch_service.list_branches()


@router.post("")
def create_branch(request: BranchCreateRequest):
    """Create a new branch."""
    if not request.activation_code:
        raise HTTPException(status_code=400, detail="activation_code is required")
    return branch_service.create_branch(request.activation_code, request.branch_name)


@router.post("/{activation_code}/identify")
def report_branch_identify(activation_code: str, request: IdentifyReportRequest):
    """Record a branch connecting and the version it is running."""
    branch = branch_service.report_identify(
        activation_code, request.version, request.branch_name
    )
    if branch is None:
        raise HTTPException(status_code=404, detail=f"Branch '{activation_code}' not found")
    return {"branch": branch}


@router.patch("/{activation_code}/version")
def assign_git_version(activation_code: str, request: AssignVersionRequest):
    """Assign a Git version to a branch."""
    if not request.version:
        raise HTTPException(status_code=400, detail="version is required")
    try:
        branch = branch_service.assign_version(activation_code, request.version)
    except branch_service.UnknownVersionError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if branch is None:
        raise HTTPException(status_code=404, detail=f"Branch '{activation_code}' not found")
    return {"message": f"Assigned version '{request.version}' to '{activation_code}'", "branch": branch}


@router.post("/{activation_code}/force-update")
def force_branch_update(activation_code: str):
    """Force an immediate Git version push to a branch."""
    return branch_service.force_update(activation_code)


@router.delete("/{activation_code}")
def remove_branch(activation_code: str):
    """Delete a branch row."""
    existed = branch_service.delete_branch(activation_code)
    if not existed:
        raise HTTPException(status_code=404, detail=f"Branch '{activation_code}' not found")
    return {"message": f"Branch '{activation_code}' removed."}


# =============================================================
# Bitbucket Commit Endpoints (version source)
# =============================================================
@router.get("/bitbucket/branches")
def get_bitbucket_branches():
    """List remote branches of the Bitbucket managed services repo."""
    try:
        return {"branches": branch_service.list_bitbucket_branches()}
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/bitbucket/commits")
def get_bitbucket_commits(branch: Optional[str] = None, limit: Optional[int] = None):
    """List recent commits of a Bitbucket branch (author/date/message).

    ``limit`` controls how many of the newest commits are checked
    (e.g. 1 = latest only, 5/10 = that many commits back). When omitted,
    only commits new since the last pull are returned.
    """
    try:
        return branch_service.list_bitbucket_commits(branch, limit=limit)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/bitbucket/store")
def store_bitbucket_versions(request: StoreVersionsRequest):
    """Store chosen Bitbucket commits as local managed services versions."""
    try:
        items = [item.dict() for item in request.items]
        stored = branch_service.store_bitbucket_versions(items)
        # How many builders were on /build when the order went out. Zero means
        # the versions are stored but nothing is compiling — the page used to
        # say "building automatically" either way.
        import build_farm_gateway as build_gateway
        return {"stored": stored, "builders": build_gateway.connected_builder_count()}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/bitbucket/version/{version_id}")
def remove_bitbucket_version(version_id: str):
    """Delete a stored version (registry + cache folder)."""
    branch_service.delete_version(version_id)
    return {"message": f"Version '{version_id}' removed."}


@router.get("/bitbucket/pending")
def branch_pending_version(branch: str):
    """Version a branch runs vs the one assigned to it.

    The client's store-info page polls this through its own
    /api/updates/pending to decide whether to flag an update. Nothing served
    this path before, so that check answered 404 and silently did nothing.
    """
    result = branch_service.pending_version_for(branch)
    if result is None:
        raise HTTPException(status_code=404, detail=f"No branch matches '{branch}'.")
    return result


@router.post("/bitbucket/version/{version_id}/rebuild")
def rebuild_bitbucket_version(version_id: str):
    """Re-publish a stored version to the build farm (for remote Mac builders)."""
    try:
        result = branch_service.rebuild_version(version_id)
        message = (
            f"Build requested for '{version_id}'."
            if result.get("notified")
            else f"Source for '{version_id}' is ready, but no builder is connected — nothing is building."
        )
        return {"message": message, **result}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/bitbucket/version/{version_id}/mode")
def set_bitbucket_version_mode(version_id: str, request: VersionModeRequest):
    """Mock admin: ship a stored version as source, or order its .so build."""
    try:
        return branch_service.set_version_mode(version_id, request.mode)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/bitbucket/version/{version_id}")
def rename_bitbucket_version(version_id: str, request: RenameVersionRequest):
    """Rename a stored version (registry key + cache folder)."""
    try:
        new_id = (request.new_id or "").strip()
        if not new_id:
            raise HTTPException(status_code=400, detail="new_id is required")
        result = branch_service.rename_version(version_id, new_id)
        return {"message": f"Version '{version_id}' renamed to '{new_id}'.", "version": result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# =============================================================
# Git Managed Services Version Endpoints
# =============================================================
@router.get("/git-versions")
def get_git_versions():
    """List available Git versions (tags + commits) from the managed services repo."""
    try:
        return branch_service.list_git_versions()
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
