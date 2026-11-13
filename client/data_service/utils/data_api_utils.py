"""The camera lists with RTSP details that camera_service fetches: the chosen cameras
(GET /data/active-cameras-config, at start) and the ones judged inside the store
(GET /data/inside-cameras-config, for the after-hours watch). The per-service zone
configs moved to each DLC service (client-to-dlc tickets 18 and 20).
"""

import urllib.parse
from typing import Any, Dict

from data_service.db.session import get_db
from data_service.configs.tracking_config import DEFAULT_RTSP_PORT
from data_service.configs.crypto_util import decrypt_password


def build_rtsp_url(ip: str, channel: int, username: str, password: str) -> str:
    user_encoded = urllib.parse.quote(username, safe='')
    pass_encoded = urllib.parse.quote(password, safe='')
    return f"rtsp://{user_encoded}:{pass_encoded}@{ip}:{DEFAULT_RTSP_PORT}/cam/realmonitor?channel={channel}&subtype=0"


def get_active_cameras():
    """{cam_<ip_with_underscores>_<channel>: {"path_rtsp", "box_detect": None}} for each status=1 channel.

    box_detect stays (always None, every service uses the full frame) so the
    answer keeps the shape a camera_service of any version reads.
    """
    conn = None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT cc.ip, cc.channel, cm_agg.username, cm_agg.password
            FROM camera_channel cc
            JOIN (
                SELECT ip, MIN(username) AS username, MIN(password) AS password
                FROM camera_master GROUP BY ip
            ) cm_agg ON cc.ip = cm_agg.ip
            WHERE cc.status = 1
            """
        )
        rows = cur.fetchall()
        out: Dict[str, Dict[str, Any]] = {}
        for ip, channel, username, password in rows:
            rtsp = build_rtsp_url(ip, channel, username, decrypt_password(password))
            default_key = f"cam_{ip.replace('.', '_')}_{channel}"
            out[default_key] = {"path_rtsp": rtsp, "box_detect": None}
        return out
    finally:
        if conn:
            conn.close()


def get_inside_cameras():
    """{cam_<ip_with_underscores>_<channel>: {"path_rtsp", "box_detect": None}} for every channel
    judged inside the store (camera_placement), chosen or not.

    camera_service watches the ones nobody chose while the store is closed. It used to take
    their address from a chosen camera on the same recorder, so a recorder with none chosen
    was never watched; the credentials are here.
    """
    conn = None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT p.ip, p.channel, cm_agg.username, cm_agg.password
            FROM camera_placement p
            JOIN (
                SELECT ip, MIN(username) AS username, MIN(password) AS password
                FROM camera_master GROUP BY ip
            ) cm_agg ON p.ip = cm_agg.ip
            WHERE p.placement = 'inside'
            """
        )
        out: Dict[str, Dict[str, Any]] = {}
        for ip, channel, username, password in cur.fetchall():
            rtsp = build_rtsp_url(ip, channel, username, decrypt_password(password))
            out[f"cam_{ip.replace('.', '_')}_{channel}"] = {"path_rtsp": rtsp, "box_detect": None}
        return out
    finally:
        if conn:
            conn.close()
