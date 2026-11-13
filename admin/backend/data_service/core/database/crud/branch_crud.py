# data_service/core/database/crud/branch_crud.py
"""Low-level SQLAlchemy CRUD operations for the branches table."""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy.orm import Session

from data_service.core.models.branch import Branch


def get_branch(session: Session, activation_code: str) -> Optional[Branch]:
    """Return the branch matching the given activation code, if any."""
    return (
        session.query(Branch)
        .filter(Branch.activation_code == activation_code)
        .first()
    )


def list_branches(session: Session) -> List[Branch]:
    """Return all branches ordered by activation code."""
    return (
        session.query(Branch)
        .order_by(Branch.activation_code.asc())
        .all()
    )


def create_branch(
    session: Session,
    activation_code: str,
    branch_name: Optional[str] = None,
    serial_number: Optional[str] = None,
    mac_address: Optional[str] = None,
) -> Branch:
    """Create and commit a new branch row."""
    branch = Branch(
        activation_code=activation_code,
        branch_name=branch_name,
        serial_number=serial_number,
        mac_address=mac_address,
        status="active",
    )
    session.add(branch)
    session.commit()
    session.refresh(branch)
    return branch


def upsert_branch(
    session: Session,
    activation_code: str,
    branch_name: Optional[str] = None,
    client_version: Optional[str] = None,
    serial_number: Optional[str] = None,
    mac_address: Optional[str] = None,
    online: bool = True,
) -> Branch:
    """Update an existing branch or create it when missing.

    Called when a branch connects so the table always reflects the latest
    machine status and the version it reported.
    """
    branch = get_branch(session, activation_code)
    if branch is None:
        branch = create_branch(
            session,
            activation_code,
            branch_name,
            serial_number=serial_number,
            mac_address=mac_address,
        )
    if branch_name:
        branch.branch_name = branch_name
    if serial_number:
        branch.serial_number = serial_number
    if mac_address:
        branch.mac_address = mac_address
    if client_version:
        branch.last_version = client_version
    branch.status = "active" if online else branch.status
    if online:
        branch.last_online_at = datetime.now()
    session.commit()
    session.refresh(branch)
    return branch


def assign_version(session: Session, activation_code: str, version: str) -> Optional[Branch]:
    """Assign a Git version to a branch; returns the branch or None."""
    branch = get_branch(session, activation_code)
    if branch is None:
        return None
    branch.assigned_version = version
    session.commit()
    session.refresh(branch)
    return branch


def set_offline(session: Session, activation_code: str) -> None:
    """Mark a branch as offline (used on socket disconnect)."""
    branch = get_branch(session, activation_code)
    if branch is None:
        return
    branch.status = "offline"
    session.commit()


def set_online(session: Session, activation_code: str, client_version: Optional[str] = None) -> None:
    """Mark a branch as online and bump its last_online_at."""
    branch = get_branch(session, activation_code)
    if branch is None:
        return
    branch.status = "active"
    branch.last_online_at = datetime.now()
    if client_version:
        branch.last_version = client_version
    session.commit()


def set_all_offline(session: Session) -> None:
    """Mark every branch as offline."""
    for branch in list_branches(session):
        branch.status = "offline"
    session.commit()
