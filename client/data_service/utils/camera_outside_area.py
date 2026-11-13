"""Which part of each camera's picture is outside the shop? Asked of the admin server (Outdoor Area).

Why: the after-hours watch alerted on people walking past the glass front (008, AVI001,
UNS013, 061IT, 061MONO, 8-9 Nov 2026). With the part of the picture that shows the outside
kept per channel, monitor_service leaves out anyone whose feet are in it.

Only channels set as inside the shop (camera_placement, by the AI or a person; user, 9 Nov
2026): an outside camera is left out of the watch whole, and one not judged yet waits for its
inside/outside answer first. When (user, 9 Nov 2026), and never otherwise:
- a channel's first picture, while it has no camera_outside_area row ('first');
- every press of that channel's inside/outside switch that leaves it inside, the side it is
  already on included, with a new picture taken first (snapshot_cache.retake), so the camera is judged as it is now
  ('switch', camera_outside_area_crud.mark_switched, called from set_user_placement).
Only the outside area is taken from the answer; inside/outside stays what the person set on
the switch (camera_placement_crud). This endpoint gives no inside/outside answer anyway.

The contract: ai-camera-admin/backend/camera_placement_service/docs/client-contract.md,
"Outdoor Area" (python_server 37d13972, 4935ef1f):
    POST {API_HOST}/api/outdoor-area   {"jpeg": "<base64>", "camera": "<label>"}
    200 {"model": "...", "answers": {"has_outdoor": {"noul": 0-1},
         "outdoor_area": {"box": {"top", "bottom", "left", "right"} (0-1), "confidence": 0-1}}}
Same headers, picture rules and 400/401/503 handling as Camera Placement (utils/camera_placement.py):
one call at a time every ROUND_S, BACKOFF_S after the server could not answer, a refused
picture (400) not sent again until the switch is pressed, KEY_PAUSE_S after a 401.

What is kept (read_answer):
- has_outdoor under HAS_OUTDOOR_MIN: nothing outside in this camera, no area. Jev always
  returns a box, so the box alone means nothing.
- else the box, kept as one 4-corner polygon in fractions of the picture (monitor_service
  takes polygons, so a better shape later needs no change there);
- except a picture under 1280x720 (SMALL_PICTURE) with box confidence under SMALL_BOX_MIN:
  the contract found such boxes on shelves (AVI008, 352x288), so no area and every alert kept.
The picture is snapshot_cache's: the NVR main stream, raw, at most 1280 wide — what the
contract asks for. has_outdoor, the box confidence and the picture size are kept with it.

Never log a header: the Device Key is in it.
"""
from __future__ import annotations

import base64
import logging
import struct
import threading
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

OUTDOOR_AREA_PATH = "/api/outdoor-area"
ROUND_S = 60
TIMEOUT_S = 60                      # the contract's client timeout
BACKOFF_S = (60, 5 * 60, 30 * 60)   # after 1, 2, 3+ failed calls in a row
KEY_PAUSE_S = 30 * 60
MAX_JPEG = 2 * 1024 * 1024
MAX_BODY = 256 * 1024               # the answer is about 1 KB; a bigger one is not read
HAS_OUTDOOR_MIN = 0.5               # the contract's suggestion
SMALL_PICTURE = (1280, 720)
SMALL_BOX_MIN = 0.3                 # the contract's suggestion for a small picture

_lock = threading.Lock()
_started = False
# (ip, channel) -> the request answered (reason, when wanted), in memory too: an answer the
# database could not keep is still never asked for twice.
_done: dict[tuple[str, int], tuple] = {}
_failures = 0
_next_try = 0.0


def outdoor_area_url() -> str:
    from data_service.configs.server_api_config import API_HOST
    return f"{API_HOST}{OUTDOOR_AREA_PATH}"


def camera_label(ip: str, channel: int) -> str:
    return f"{ip}_ch{int(channel):02d}"


def jpeg_size(jpeg: bytes) -> Optional[tuple[int, int]]:
    """(width, height) from a JPEG's frame header, without decoding it."""
    i = 2
    while i + 9 < len(jpeg):
        if jpeg[i] != 0xFF:
            return None
        marker = jpeg[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        length = struct.unpack(">H", jpeg[i + 2:i + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h, w = struct.unpack(">HH", jpeg[i + 5:i + 9])
            return (w, h) if w and h else None
        i += 2 + length
    return None


def ask(jpeg: bytes, camera: str, headers: dict) -> tuple[int, Optional[dict]]:
    """One call. (status, body); status 0 when the server could not be reached."""
    try:
        r = requests.post(outdoor_area_url(), headers=headers, timeout=TIMEOUT_S,
                          json={"jpeg": base64.b64encode(jpeg).decode(), "camera": camera})
    except requests.RequestException:
        return 0, None
    if len(r.content or b"") > MAX_BODY:
        return r.status_code, None
    try:
        body = r.json()
    except ValueError:
        body = None
    return r.status_code, body if isinstance(body, dict) else None


def _fraction(value) -> Optional[float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value == value:
        return min(max(float(value), 0.0), 1.0)
    return None


def read_answer(body: Optional[dict], picture: Optional[tuple[int, int]]) -> Optional[dict]:
    """{polygons, has_outdoor, box_confidence, model, why} from a 200 body; None when the body
    is not an Outdoor Area answer (no has_outdoor, or a box that is not four numbers)."""
    answers = (body or {}).get("answers")
    if not isinstance(answers, dict):
        return None
    has = answers.get("has_outdoor")
    has_outdoor = _fraction(has.get("noul")) if isinstance(has, dict) else None
    if has_outdoor is None:
        return None
    area = answers.get("outdoor_area") if isinstance(answers.get("outdoor_area"), dict) else {}
    box = area.get("box") if isinstance(area.get("box"), dict) else {}
    left, right, top, bottom = (_fraction(box.get(k)) for k in ("left", "right", "top", "bottom"))
    confidence = _fraction(area.get("confidence"))
    model = body.get("model") if isinstance(body.get("model"), str) else None
    out = {"polygons": [], "has_outdoor": has_outdoor, "box": None, "box_confidence": confidence, "model": model}
    if None in (left, right, top, bottom) or right <= left or bottom <= top:
        if has_outdoor < HAS_OUTDOOR_MIN:
            return {**out, "why": "no outside in view"}   # the box would not be used anyway
        return None if box else {**out, "why": "no box"}
    out["box"] = {"left": left, "top": top, "right": right, "bottom": bottom}   # kept, used or not
    if has_outdoor < HAS_OUTDOOR_MIN:
        return {**out, "why": "no outside in view"}
    small = picture is None or picture[0] < SMALL_PICTURE[0] or picture[1] < SMALL_PICTURE[1]
    if small and (confidence is None or confidence < SMALL_BOX_MIN):
        return {**out, "why": "small picture, box not trusted"}
    return {**out, "polygons": [[[left, top], [right, top], [right, bottom], [left, bottom]]], "why": "box"}


def _wanted() -> list[tuple[str, int, tuple]]:
    """(ip, channel, (reason, when wanted)) to ask about now: set as inside, a picture, and
    either no row ('first') or the switch pressed since the last answer ('switch'). A press
    to outside stays wanted; it is asked only if the channel is set inside again."""
    from data_service.db.crud.camera_outside_area_crud import channels_to_ask, known_channels
    from data_service.db.crud.camera_placement_crud import read_inside_channels
    from data_service.utils import snapshot_cache
    inside = read_inside_channels()
    asked = channels_to_ask()
    known = known_channels()
    out = []
    for t in snapshot_cache._scanned_channels():
        if t not in inside or snapshot_cache.get(*t) is None:
            continue
        request = asked.get(t) if t in known else ("first", None)
        if request is None:
            continue
        with _lock:
            if _done.get(t) == request:
                continue   # answered, but the row could not be written: still not asked twice
        out.append((t[0], t[1], request))
    return out


def run_round(now: Optional[float] = None) -> int:
    """Ask for every channel that needs it; returns how many calls were made."""
    global _failures, _next_try
    import device_identity
    from data_service.db.crud.camera_outside_area_crud import save_area, save_refusal
    from data_service.utils import snapshot_cache
    now = time.time() if now is None else now
    if now < _next_try:
        return 0
    headers = device_identity.admin_auth_headers()
    if not headers:
        return 0   # no Device Key yet: the server would only say 401
    calls = 0
    for ip, channel, request in _wanted():
        reason = request[0]
        if reason == "switch":
            # A press asks about the camera as it is now, not the picture kept since the app
            # started: take a new one first (Retake picture; on failure the kept one is sent).
            try:
                snapshot_cache.retake(ip, channel)
            except Exception as e:
                logger.warning(f"[outdoor-area] {camera_label(ip, channel)}: new picture not taken: {type(e).__name__}")
        jpeg = snapshot_cache.get(ip, channel)
        if not jpeg or len(jpeg) > MAX_JPEG:
            continue
        cam = camera_label(ip, channel)
        size = jpeg_size(jpeg)
        status, body = ask(jpeg, cam, headers)
        calls += 1
        if status == 200 and (answer := read_answer(body, size)) is not None:
            with _lock:
                _done[(ip, channel)] = request
                _failures = 0
            try:
                save_area(ip, channel, answer, size)
            except Exception as e:
                logger.error(f"[outdoor-area] {cam}: answer not saved: {type(e).__name__}")
            logger.info(f"[outdoor-area] {cam}: {answer['why']} (has_outdoor {answer['has_outdoor']}, "
                        f"box {answer['box_confidence']}, {size[0] if size else '?'}x{size[1] if size else '?'}, "
                        f"{reason}, {answer['model']})")
            continue
        if status == 400:
            error = str((body or {}).get("error") or "bad request")[:128]
            try:
                save_refusal(ip, channel, error)
            except Exception as e:
                logger.error(f"[outdoor-area] {cam}: refusal not saved: {type(e).__name__}")
            with _lock:
                _done[(ip, channel)] = request
            logger.warning(f"[outdoor-area] {cam}: picture refused: {error}")
            continue
        if status == 401:
            _next_try = time.time() + KEY_PAUSE_S
            logger.warning(f"[outdoor-area] Device Key not accepted; asking again in {KEY_PAUSE_S // 60} min")
            return calls
        # 404 (server not updated yet), 5xx, timeout, a 200 that is not an answer: try again later
        with _lock:
            _failures += 1
            wait = BACKOFF_S[min(_failures, len(BACKOFF_S)) - 1]
        _next_try = time.time() + wait
        logger.warning(f"[outdoor-area] server could not answer ({status or 'no connection'}); "
                       f"asking again in {wait // 60} min")
        return calls
    return calls


def _loop() -> None:
    while True:
        try:
            run_round()
        except Exception as e:
            logger.warning(f"[outdoor-area] round failed: {type(e).__name__}")
        time.sleep(ROUND_S)


def start() -> None:
    """Start the background thread once per process."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_loop, name="camera-outdoor-area", daemon=True).start()
