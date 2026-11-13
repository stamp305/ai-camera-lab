"""Last reported health of each Branch camera, kept beside the branches table."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from data_service.core.database.branch_db import Base


class CameraHealth(Base):
    """One row per (Branch, camera): its status now, and since when.

    `since` moves only when the status changes, so how long a camera has been in
    trouble is now - since. `alerted_status`/`alerted_at` remember the last LINE
    alert for this camera so it is not repeated every minute.
    """

    __tablename__ = "camera_health"
    __table_args__ = (UniqueConstraint("activation_code", "camera", name="uq_camera_health_branch_camera"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    activation_code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    camera: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    since: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_report: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    store_open: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    branch_label: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    alerted_status: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    alerted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
