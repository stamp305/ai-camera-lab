"""What a Branch report means, and when it becomes a LINE alert.

The Branch only says what each camera looks like right now. How long counts as a
problem, whether the store is open, and how often to repeat an alert are decided
here, so they change with a server deploy rather than a DLC release to every Branch.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from camera_alert_service.models import CameraHealth

STATUS_TEXT = {
    "rtsp_down": "เชื่อมต่อกล้องไม่ได้ (RTSP)",
    "black": "ภาพดำ / ไม่มีภาพ",
    # Blurred or broken compared with what that camera normally shows; a camera that
    # has always looked like this is never reported as degraded (the Branch decides).
    "degraded": "คุณภาพภาพลดลง",
}
STATUSES = {"ok", *STATUS_TEXT}

# The Branch's once-a-day camera check (monitor_service/daily_check.py): what it found. The
# Branch also says what is wrong with a degraded picture; the message leaves that out.
DAILY_TEXT = {
    "rtsp_down": "เชื่อมต่อกล้องไม่ได้ (RTSP)",
    "no_frames": "ไม่ได้รับภาพจากกล้องตัวใดเลย (ตรวจเครื่องและเครือข่ายของสาขา)",
    "black": "ภาพดำ / ไม่มีภาพ",
    "degraded": "คุณภาพภาพลดลง",
}

# How long a camera must stay in a status before it is worth an alert.
PROBLEM_AFTER = {
    "rtsp_down": timedelta(minutes=30),
    "black": timedelta(minutes=30),
    "degraded": timedelta(minutes=30),
}
# A Branch reports every minute; a row older than this is a Branch that went quiet,
# which the offline check (cronjobs/monitor_ai_camera.py) already reports.
STALE_AFTER = timedelta(minutes=5)
# The same camera, same problem, is repeated at most once a day.
REMIND_AFTER = timedelta(hours=24)
# ...unless the camera was back to ok at least this long in between: then a new outage
# is a new problem and is alerted like the first. A short ok blip in a long outage is not.
RECOVERED_AFTER = timedelta(minutes=30)

MAX_CAMERAS = 64
_CAMERA_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
_LABEL_FIELD_MAX = 100


class ReportError(ValueError):
    """The body is not a report this service understands (answered 400)."""


def _text(value, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ReportError(f"{field} must be a string")
    return value.strip()[:_LABEL_FIELD_MAX]


def store_label(store) -> Optional[str]:
    """`8888 จ.เชียงใหม่ (เซ็นทรัล)` from the store the Branch sent, as monitor_ai_camera.py writes it."""
    if not isinstance(store, dict):
        return None
    short_code = _text(store.get("short_code"), "store.short_code")
    province = _text(store.get("province"), "store.province")
    name = _text(store.get("branch_name"), "store.branch_name")
    if short_code and province and name:
        return f"{short_code} จ.{province} ({name})"
    return name or short_code or None


def camera_label(camera: str) -> str:
    """`กล้อง 3 (192.168.20.240)` from `cam_192_168_20_240_3`."""
    parts = camera.split("_")
    if len(parts) == 6 and parts[0] == "cam":
        return f"กล้อง {parts[-1]} ({'.'.join(parts[1:5])})"
    return camera


def parse_health(body) -> Tuple[Optional[bool], Optional[str], Dict[str, str]]:
    """(store_open, branch label, {camera: status}) from a health report, or ReportError."""
    if not isinstance(body, dict):
        raise ReportError("body must be a JSON object")
    store_open = body.get("store_open")
    if store_open is not None and not isinstance(store_open, bool):
        raise ReportError("store_open must be true, false or null")
    cameras = body.get("cameras")
    if not isinstance(cameras, dict) or len(cameras) > MAX_CAMERAS:
        raise ReportError(f"cameras must be an object of at most {MAX_CAMERAS} cameras")
    statuses = {}
    for camera, info in cameras.items():
        if not _CAMERA_RE.match(camera):
            raise ReportError(f"bad camera name: {camera[:70]!r}")
        status = info.get("status") if isinstance(info, dict) else None
        if status not in STATUSES:
            raise ReportError(f"{camera}: status must be one of {sorted(STATUSES)}")
        statuses[camera] = status
    return store_open, store_label(body.get("store")), statuses


def apply_health(session: Session, code: str, body, now: datetime) -> int:
    """Record one report. Cameras the Branch no longer reports are forgotten."""
    store_open, label, statuses = parse_health(body)
    rows = {r.camera: r for r in session.scalars(
        select(CameraHealth).where(CameraHealth.activation_code == code))}
    for camera, status in statuses.items():
        row = rows.get(camera)
        if row is None:
            row = CameraHealth(activation_code=code, camera=camera, status=status, since=now, last_report=now)
            session.add(row)
        elif row.status != status:
            if row.status == "ok" and now - row.since >= RECOVERED_AFTER:
                row.alerted_status = row.alerted_at = None
            row.status, row.since = status, now
        row.last_report, row.store_open = now, store_open
        if label:
            row.branch_label = label
    gone = [c for c in rows if c not in statuses]
    if gone:
        session.execute(delete(CameraHealth).where(
            CameraHealth.activation_code == code, CameraHealth.camera.in_(gone)))
    session.commit()
    return len(statuses)


def due_alerts(session: Session, now: datetime) -> Dict[str, List[CameraHealth]]:
    """Rows worth an alert now, by Branch. Nothing is marked; see mark_alerted."""
    due = defaultdict(list)
    for row in session.scalars(select(CameraHealth).where(CameraHealth.status != "ok")):
        if not row.store_open:  # closed, or the Branch could not tell: lights off is not a fault
            continue
        if now - row.last_report > STALE_AFTER:
            continue
        if now - row.since < PROBLEM_AFTER.get(row.status, timedelta.max):
            continue
        if row.alerted_status == row.status and row.alerted_at and now - row.alerted_at < REMIND_AFTER:
            continue
        due[row.activation_code].append(row)
    return dict(due)


def mark_alerted(session: Session, rows: List[CameraHealth], now: datetime) -> None:
    for row in rows:
        row.alerted_status, row.alerted_at = row.status, now
    session.commit()


def _duration(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ชม. {minutes} นาที" if hours else f"{minutes} นาที"


def health_message(label: str, rows: List[CameraHealth], now: datetime) -> str:
    lines = [f"⚠️ แจ้งเตือนกล้อง AI Camera: {label}"]
    for row in sorted(rows, key=lambda r: r.camera):
        lines.append(f"- {camera_label(row.camera)}: {STATUS_TEXT[row.status]} "
                     f"(เป็นมา {_duration(now - row.since)})")
    return "\n".join(lines)


def event_message(label: str, body) -> str:
    """The LINE text for one event, or ReportError for an event this service does not know."""
    if not isinstance(body, dict):
        raise ReportError("body must be a JSON object")
    kind = body.get("type")
    if kind == "intrusion":
        camera = body.get("camera")
        if not isinstance(camera, str) or not _CAMERA_RE.match(camera):
            raise ReportError("intrusion needs a camera")
        at = _text(body.get("detected_at"), "detected_at") or "-"
        note = _text(body.get("note"), "note")  # the Branch's second look / lights, when it had one
        return (f"🚨 พบคนในร้านหลังเวลาปิด: {label}\n"
                f"- {camera_label(camera)} เวลา {at}" + (f"\n- {note}" if note else ""))
    if kind == "angle":
        items = body.get("items")
        if not isinstance(items, list) or not items or len(items) > MAX_CAMERAS:
            raise ReportError("angle needs items")
        lines, swapped = [], False
        for item in items:
            camera = item.get("camera") if isinstance(item, dict) else None
            if not isinstance(camera, str) or not _CAMERA_RE.match(camera):
                raise ReportError("angle item needs a camera")
            other = item.get("swapped_with")
            if isinstance(other, str) and _CAMERA_RE.match(other):
                swapped = True
                lines.append(f"🔄 {camera_label(camera)} สลับกับ {camera_label(other)}")
            else:
                lines.append(f"⚠️ {camera_label(camera)} มุมกล้องเปลี่ยนไปจากเดิม")
        header = "[SWAP] แจ้งเตือน AI Camera" if swapped else "[ALERT] แจ้งเตือน AI Camera"
        return f"{header}\n{label}:\n" + "\n".join(lines)
    if kind == "daily_check":
        items = body.get("items")
        if not isinstance(items, list) or not items or len(items) > MAX_CAMERAS:
            raise ReportError("daily_check needs items")
        lines = []
        for item in items:
            camera = item.get("camera") if isinstance(item, dict) else None
            if not isinstance(camera, str) or not _CAMERA_RE.match(camera):
                raise ReportError("daily_check item needs a camera")
            status = item.get("status")
            if status not in DAILY_TEXT:
                raise ReportError(f"{camera}: daily_check status must be one of {sorted(DAILY_TEXT)}")
            lines.append(f"- {camera_label(camera)}: {DAILY_TEXT[status]}")
        return f"⚠️ ตรวจกล้องประจำวัน AI Camera: {label}\n" + "\n".join(lines)
    raise ReportError(f"unknown event type: {str(kind)[:30]!r}")
