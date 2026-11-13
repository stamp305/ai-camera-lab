"""LINE through the internal relay — the only place that sends, so no Branch holds a LINE token."""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime

import requests

from camera_alert_service import alerts

logger = logging.getLogger(__name__)

# The relay on the office network that the cronjobs use too; it needs no token.
LINE_RELAY_URL = os.getenv("CAMERA_ALERT_LINE_URL", "http://10.10.10.85:5003/callback_adv_new")
# Two groups: someone in the store after hours goes to the people who answer for the branch,
# every camera problem (health, angle, daily check) to the CCTV group. CAMERA_ALERT_LINE_GROUP,
# when set, sends both to that one group instead (for trying this out without bothering either).
INTRUSION_GROUP = os.getenv("CAMERA_ALERT_LINE_GROUP_INTRUSION", "Branch Watcher")
CCTV_GROUP = os.getenv("CAMERA_ALERT_LINE_GROUP_CCTV", "CCTV Alert")
GROUP_OVERRIDE = os.getenv("CAMERA_ALERT_LINE_GROUP", "")
CHECK_EVERY_SECONDS = 60


def group_for(event_type) -> str:
    """The LINE group for an event type ("intrusion", "angle", "daily_check", ...)."""
    return GROUP_OVERRIDE or (INTRUSION_GROUP if event_type == "intrusion" else CCTV_GROUP)


def send_line(message: str, group: str = None) -> bool:
    try:
        resp = requests.post(
            LINE_RELAY_URL,
            json={"message": message, "to": group or group_for(None), "type": "text", "depart": "python"},
            timeout=10,
        )
        if resp.ok:
            return True
        logger.error("[camera-alert] LINE relay answered %s", resp.status_code)
    except requests.RequestException as exc:
        logger.error("[camera-alert] LINE relay unreachable: %s", exc)
    return False


def send_image(image_b64: str, group: str = None) -> bool:
    """One picture (JPEG, base64) to a group through the relay's image type."""
    try:
        resp = requests.post(
            LINE_RELAY_URL,
            json={"message": image_b64, "to": group or group_for(None), "type": "image", "depart": "python"},
            timeout=30,
        )
        if resp.ok:
            return True
        logger.error("[camera-alert] LINE relay answered %s to a picture", resp.status_code)
    except requests.RequestException as exc:
        logger.error("[camera-alert] LINE relay unreachable for a picture: %s", exc)
    return False


def send_due_alerts(session_factory, now: datetime = None, send=None) -> int:
    """One LINE message per Branch with cameras due; returns how many were sent.

    A message that fails to send is not marked, so the next round tries it again.
    """
    now = now or datetime.now()
    send = send or send_line  # looked up per call, not bound once at import
    sent = 0
    with session_factory() as session:
        for code, rows in alerts.due_alerts(session, now).items():
            # apply_health already fell back to the identified branch name when the Branch sent no store.
            label = next((r.branch_label for r in rows if r.branch_label), None) or "สาขา (ไม่ทราบชื่อ)"
            if send(alerts.health_message(label, rows, now)):
                alerts.mark_alerted(session, rows, now)
                sent += 1
    return sent


def start_alert_loop(session_factory) -> threading.Thread:
    def loop():
        stop = threading.Event()
        while not stop.wait(CHECK_EVERY_SECONDS):
            try:
                send_due_alerts(session_factory)
            except Exception:
                logger.exception("[camera-alert] alert round failed")

    thread = threading.Thread(target=loop, name="camera-alert-loop", daemon=True)
    thread.start()
    return thread
