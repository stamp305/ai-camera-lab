# data_service/core/models/branch.py
"""SQLAlchemy model for the branches (client branch machines) table."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from data_service.core.database.branch_db import Base


class Branch(Base):
    """A client branch machine managed by the admin server.

    The assigned_version column controls which Git managed version this branch
    will receive when it connects or when an update is forced.
    """

    __tablename__ = "branches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    activation_code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    branch_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    serial_number: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    mac_address: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    # Added by deploy/migrations/20241201_add_role_column.sql as NOT NULL. The model
    # has to carry it too — otherwise every INSERT omits the column and the database
    # rejects the row, so no branch can ever be created.
    role: Mapped[str] = mapped_column(
        String(20), nullable=False, default="viewer", server_default="viewer"
    )
    assigned_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    last_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    last_online_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    def to_dict(self) -> dict:
        """Serialize the branch to a plain dictionary for API responses."""
        return {
            "activation_code": self.activation_code,
            "branch_name": self.branch_name,
            "serial_number": self.serial_number,
            "mac_address": self.mac_address,
            "status": self.status,
            "assigned_version": self.assigned_version,
            "last_version": self.last_version,
            "last_online_at": self.last_online_at.isoformat() if self.last_online_at else None,
        }
