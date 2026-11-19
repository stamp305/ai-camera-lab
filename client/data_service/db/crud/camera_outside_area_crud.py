"""The part of each channel's picture that films outside the shop (table camera_outside_area).

The admin server's Outdoor Area answer (utils/camera_outside_area.py) is kept as polygons
(today one rectangle, or none); the after-hours watch (monitor_service) leaves out anyone
standing in them, so people beyond the glass front or on the street raise no alarm.

When the server is asked, and only then (user, 9 Nov 2026):
- once per channel, the first time it has a picture: no row yet ('first');
- again whenever a person presses that channel's inside/outside switch ('switch',
  mark_switched() from set_user_placement), the side it is already on included.
Nothing else asks again: not an app restart, not a new picture, not a polygon that came back
empty or with no outside in view. A picture the server refused is not sent again until the switch is pressed.

Polygons are kept in fractions of the picture (0..1, x across, y down), so they fit
whichever stream the watch reads.
"""
import json
from typing import Optional

from data_service.db.session import get_db

MAX_POLYGONS = 20
MAX_POINTS = 64


def clean_polygons(raw) -> Optional[list]:
    """[[[x, y], ...], ...] in 0..1, at most MAX_POLYGONS of at most MAX_POINTS points, each
    with 3 points or more; None when `raw` is not a list of polygons at all."""
    if not isinstance(raw, list):
        return None
    out = []
    for poly in raw:
        if len(out) >= MAX_POLYGONS:
            break
        if not isinstance(poly, list):
            return None
        pts = []
        for p in poly[:MAX_POINTS]:
            if isinstance(p, dict):
                p = [p.get("x"), p.get("y")]
            if not (isinstance(p, (list, tuple)) and len(p) == 2
                    and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in p)):
                return None
            x, y = float(p[0]), float(p[1])
            if x != x or y != y:          # NaN
                return None
            pts.append([min(max(x, 0.0), 1.0), min(max(y, 0.0), 1.0)])
        if len(pts) >= 3:
            out.append(pts)
    return out


def channels_to_ask() -> dict:
    """{(ip, channel): (reason, when it was wanted)} for every row waiting for polygons. Channels
    with no row at all are 'first' and come from the caller's list of channels with a picture."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT ip, channel, reason, wanted_at FROM camera_outside_area WHERE state = 'wanted'")
        return {(ip, int(ch)): (reason, str(wanted_at) if wanted_at else None)
                for ip, ch, reason, wanted_at in cur.fetchall()}
    finally:
        conn.close()


def known_channels() -> set:
    """{(ip, channel)} that have a row: answered, refused, or waiting after a switch."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT ip, channel FROM camera_outside_area")
        return {(ip, int(ch)) for ip, ch in cur.fetchall()}
    finally:
        conn.close()


def save_area(ip: str, channel: int, answer: dict, picture: Optional[tuple]) -> None:
    """An Outdoor Area answer (utils/camera_outside_area.read_answer): the polygons the watch
    uses ([] = none), with has_outdoor, Jev's box (kept whether used or not), its confidence
    and the picture size it came from."""
    polygons = clean_polygons(answer.get("polygons")) or []
    w, h = picture if picture else (None, None)
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO camera_outside_area (ip, channel, state, reason, polygons, has_outdoor, box, box_confidence, "
            "picture_w, picture_h, model, error, answered_at) "
            "VALUES (?, ?, 'done', 'first', ?, ?, ?, ?, ?, ?, ?, NULL, NOW()) "
            "ON DUPLICATE KEY UPDATE state = 'done', polygons = VALUES(polygons), "
            "has_outdoor = VALUES(has_outdoor), box = VALUES(box), box_confidence = VALUES(box_confidence), "
            "picture_w = VALUES(picture_w), picture_h = VALUES(picture_h), model = VALUES(model), "
            "error = NULL, answered_at = NOW()",
            (ip, int(channel), json.dumps(polygons), answer.get("has_outdoor"),
             json.dumps(answer["box"]) if answer.get("box") else None, answer.get("box_confidence"),
             w, h, (answer.get("model") or "")[:64] or None))
        conn.commit()
    finally:
        conn.close()


def save_refusal(ip: str, channel: int, error: str) -> None:
    """The server would not take the picture: not asked again until the switch is pressed.
    A polygon kept from before stays in use."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO camera_outside_area (ip, channel, state, reason, error, answered_at) "
            "VALUES (?, ?, 'refused', 'first', ?, NOW()) "
            "ON DUPLICATE KEY UPDATE state = 'refused', error = VALUES(error), answered_at = NOW()",
            (ip, int(channel), error[:128]))
        conn.commit()
    finally:
        conn.close()


def mark_switched(ip: str, channel: int) -> None:
    """A person pressed the inside/outside switch (either side): ask for this channel's polygons again.
    Until the answer comes, the polygons from before stay in use."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO camera_outside_area (ip, channel, state, reason, wanted_at) "
            "VALUES (?, ?, 'wanted', 'switch', NOW(6)) "
            "ON DUPLICATE KEY UPDATE state = 'wanted', reason = 'switch', wanted_at = NOW(6)",
            (ip, int(channel)))
        conn.commit()
    finally:
        conn.close()


def _box(raw) -> Optional[dict]:
    try:
        b = json.loads(raw) if raw else None
    except ValueError:
        return None
    if not isinstance(b, dict):
        return None
    vals = {k: b.get(k) for k in ("left", "top", "right", "bottom")}
    return vals if all(isinstance(v, (int, float)) for v in vals.values()) else None


def read_outside_areas(ip: Optional[str] = None) -> list:
    conn = get_db()
    try:
        cur = conn.cursor()
        sql = ("SELECT ip, channel, state, reason, polygons, has_outdoor, box, box_confidence, picture_w, picture_h, "
               "model, error, wanted_at, answered_at FROM camera_outside_area")
        if ip:
            cur.execute(sql + " WHERE ip = ? ORDER BY channel", (ip,))
        else:
            cur.execute(sql + " ORDER BY ip, channel")
        rows = cur.fetchall()
    finally:
        conn.close()
    out = []
    for (r_ip, ch, state, reason, polygons, has_outdoor, box, box_conf, pw, ph,
         model, error, wanted_at, answered_at) in rows:
        try:
            polys = clean_polygons(json.loads(polygons)) if polygons else None
        except ValueError:
            polys = None
        out.append({"ip": r_ip, "channel": int(ch), "state": state, "reason": reason,
                    "polygons": polys,
                    "has_outdoor": float(has_outdoor) if has_outdoor is not None else None,
                    "box": _box(box),
                    "box_confidence": float(box_conf) if box_conf is not None else None,
                    "picture": [int(pw), int(ph)] if pw and ph else None,
                    "model": model, "error": error,
                    "wanted_at": wanted_at.isoformat(sep=" ") if wanted_at else None,
                    "answered_at": answered_at.isoformat(sep=" ") if answered_at else None})
    return out


def forget_beyond(ip: str, count: int) -> int:
    """Delete the rows of an NVR's channels past its own channel count (its screen, not a
    camera: utils/snapshot_cache.beyond); returns how many."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM camera_outside_area WHERE ip = ? AND channel > ?", (ip, int(count)))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()
