"""Whether each camera channel films inside the shop or outside it (table camera_placement).

`placement` is what the app goes by. It starts as the admin server's answer
(utils/camera_placement.py) and a person can change it; once a person has
(set_by = 'user'), the server is not asked about that channel again and a late
answer does not overwrite it. The server's own answer stays in the ai_* columns.

A row exists once the server answered, refused the picture, or a person chose.
A refused picture leaves a row with no placement: that channel is still asked
about, with its next picture.
"""
import json
from typing import Optional

from data_service.db.session import get_db

PLACEMENTS = ("inside", "outside", "uncertain")
REVIEW_BELOW = 0.6   # confidence under which a person should look (the contract leaves it to us)

_COLUMNS = ("ip", "channel", "placement", "set_by", "ai_choice", "ai_confidence", "ai_probabilities",
            "ai_area", "ai_model", "ai_at", "ai_error", "user_at")


def read_placed_channels() -> set:
    """{(ip, channel)} of every channel that has a placement: not to be asked about again."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT ip, channel FROM camera_placement WHERE placement IS NOT NULL OR set_by IS NOT NULL")
        return {(ip, int(ch)) for ip, ch in cur.fetchall()}
    finally:
        conn.close()


def read_inside_channels() -> set:
    """{(ip, channel)} set as inside the shop, by the AI or a person: the only channels whose
    outside-the-shop part is asked for (camera_outside_area)."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT ip, channel FROM camera_placement WHERE placement = 'inside'")
        return {(ip, int(ch)) for ip, ch in cur.fetchall()}
    finally:
        conn.close()


def _row(values) -> dict:
    row = dict(zip(_COLUMNS, values))
    row["channel"] = int(row["channel"])
    if row["ai_confidence"] is not None:
        row["ai_confidence"] = float(row["ai_confidence"])
    try:
        row["ai_probabilities"] = json.loads(row["ai_probabilities"]) if row["ai_probabilities"] else None
    except ValueError:
        row["ai_probabilities"] = None
    for k in ("ai_at", "user_at"):
        row[k] = row[k].isoformat(sep=" ") if row[k] is not None else None
    row["needs_review"] = row["set_by"] != "user" and (
        row["placement"] in (None, "uncertain")
        or row["ai_confidence"] is None or row["ai_confidence"] < REVIEW_BELOW)
    return row


def read_camera_placements(ip: Optional[str] = None) -> list:
    conn = get_db()
    try:
        cur = conn.cursor()
        sql = f"SELECT {', '.join(_COLUMNS)} FROM camera_placement"
        if ip:
            cur.execute(sql + " WHERE ip = ? ORDER BY channel", (ip,))
        else:
            cur.execute(sql + " ORDER BY ip, channel")
        return [_row(r) for r in cur.fetchall()]
    finally:
        conn.close()


def read_camera_placement(ip: str, channel: int) -> Optional[dict]:
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT {', '.join(_COLUMNS)} FROM camera_placement WHERE ip = ? AND channel = ?",
                    (ip, int(channel)))
        r = cur.fetchone()
        return _row(r) if r else None
    finally:
        conn.close()


def save_ai_answer(ip: str, channel: int, answer: dict) -> None:
    """The server's answer; it becomes the placement unless a person has chosen one."""
    probabilities = json.dumps(answer["probabilities"]) if answer.get("probabilities") else None
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO camera_placement (ip, channel, placement, set_by, ai_choice, ai_confidence, "
            "ai_probabilities, ai_area, ai_model, ai_at, ai_error) "
            "VALUES (?, ?, ?, 'ai', ?, ?, ?, ?, ?, NOW(), NULL) "
            "ON DUPLICATE KEY UPDATE "
            "placement = IF(set_by = 'user', placement, VALUES(placement)), "
            "set_by = IF(set_by = 'user', set_by, 'ai'), "
            "ai_choice = VALUES(ai_choice), ai_confidence = VALUES(ai_confidence), "
            "ai_probabilities = VALUES(ai_probabilities), ai_area = VALUES(ai_area), "
            "ai_model = VALUES(ai_model), ai_at = NOW(), ai_error = NULL",
            (ip, int(channel), answer["choice"], answer["choice"], answer.get("confidence"),
             probabilities, (answer.get("area") or "")[:32] or None, (answer.get("model") or "")[:64] or None))
        conn.commit()
    finally:
        conn.close()


def save_ai_refusal(ip: str, channel: int, error: str) -> None:
    """The server would not judge the picture: a row with no placement, so the channel shows as not set."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO camera_placement (ip, channel, ai_at, ai_error) VALUES (?, ?, NOW(), ?) "
            "ON DUPLICATE KEY UPDATE ai_at = NOW(), ai_error = VALUES(ai_error)",
            (ip, int(channel), error[:128]))
        conn.commit()
    finally:
        conn.close()


def set_user_placement(ip: str, channel: int, placement: str) -> dict:
    """A person's choice; it wins over the server's answer from now on. Every press of the
    switch, the same side again included (user, 9 Nov), also asks the server for this
    channel's outside-the-shop polygons again (camera_outside_area_crud.mark_switched)."""
    if placement not in ("inside", "outside"):
        raise ValueError("placement must be inside or outside")
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO camera_placement (ip, channel, placement, set_by, user_at) "
            "VALUES (?, ?, ?, 'user', NOW()) "
            "ON DUPLICATE KEY UPDATE placement = VALUES(placement), set_by = 'user', user_at = NOW()",
            (ip, int(channel), placement))
        conn.commit()
    finally:
        conn.close()
    try:   # the choice is saved; a failed re-ask must not turn the press into an error
        from data_service.db.crud.camera_outside_area_crud import mark_switched
        mark_switched(ip, channel)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"[outdoor-area] re-ask not marked: {type(e).__name__}")
    return read_camera_placement(ip, channel)


def forget_beyond(ip: str, count: int) -> int:
    """Delete the rows of an NVR's channels past its own channel count (its screen, not a
    camera: utils/snapshot_cache.beyond); returns how many."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM camera_placement WHERE ip = ? AND channel > ?", (ip, int(count)))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()
