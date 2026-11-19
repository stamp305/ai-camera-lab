"""Does each camera film inside the shop or outside it? Asked of the admin server once per channel.

The admin server judges one still per camera (POST /ai-camera/api/camera-placement,
contract: ai-camera-admin/backend/camera_placement_service/docs/client-contract.md) and
answers inside / outside / uncertain with its confidence. The answer is kept in
camera_placement (camera_placement_crud) as the channel's placement until a person
changes it there.

Once per channel, keyed by (ip, channel): a channel is asked only while it has no row,
so restarting the app, or editing a network's username or password, asks nothing again.
A new network, a new IP, or a larger channel count brings channels with no row: those are
asked. The picture is the one Camera Selection already took (snapshot_cache), sent at the
NVR's own size (get_original; the contract: "do not resize it yourself" — until 16 Nov 2026
the 1280-wide page tile was sent); a channel
with no picture (no signal, empty) is not asked until it has one. A branch set up before
this existed has no rows yet, so each of its channels is asked once after the update.

One call at a time, every ROUND_S. Each call costs the server money, so nothing here
asks twice for an answer it got: an answer is also remembered in memory, in case it could
not be written to the database. When the server cannot answer (not deployed yet, 5xx,
timeout), nobody is asked again until the next wait in BACKOFF_S has passed. A picture the
server refused (400) is not sent again; a new picture of that channel (after an app
restart, or Retake picture) is. A Device Key the server did not accept (401) stops the
calls for KEY_PAUSE_S; the key itself is renewed elsewhere (device_identity).

Never log a header: the Device Key is in it.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import threading
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

ROUND_S = 60
TIMEOUT_S = 60                       # the contract's client timeout
BACKOFF_S = (60, 5 * 60, 30 * 60)    # after 1, 2, 3+ failed calls in a row
KEY_PAUSE_S = 30 * 60
MAX_JPEG = 2 * 1024 * 1024
CHOICES = ("inside", "outside", "uncertain")

_lock = threading.Lock()
_started = False
_answered: set[tuple[str, int]] = set()
_refused: dict[tuple[str, int], str] = {}   # (ip, channel) -> sha256 of the picture refused
_failures = 0
_next_try = 0.0


def placement_url() -> str:
    from data_service.configs.server_api_config import API_HOST
    return f"{API_HOST}/api/camera-placement"


def camera_label(ip: str, channel: int) -> str:
    return f"{ip}_ch{int(channel):02d}"


def ask(jpeg: bytes, camera: str, headers: dict) -> tuple[int, Optional[dict]]:
    """One call. (status, body); status 0 when the server could not be reached."""
    try:
        r = requests.post(placement_url(), headers=headers, timeout=TIMEOUT_S,
                          json={"jpeg": base64.b64encode(jpeg).decode(), "camera": camera})
    except requests.RequestException:
        return 0, None
    try:
        body = r.json()
    except ValueError:
        body = None
    return r.status_code, body if isinstance(body, dict) else None


def read_answer(body: Optional[dict]) -> Optional[dict]:
    """The fields kept from a 200 body, or None when it has no usable label."""
    answers = (body or {}).get("answers") or {}
    label = answers.get("label") or {}
    choice = label.get("choice")
    if choice not in CHOICES:
        return None
    area = answers.get("area") or {}
    probabilities = label.get("probabilities")
    return {
        "choice": choice,
        "confidence": _number(label.get("confidence")),
        "probabilities": probabilities if isinstance(probabilities, dict) else None,
        "area": area.get("choice") if isinstance(area.get("choice"), str) else None,
        "model": body.get("model") if isinstance(body.get("model"), str) else None,
    }


def _number(value) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _wanted() -> list[tuple[str, int]]:
    """Channels with a picture and no placement row, in the order Camera Selection lists them."""
    from data_service.db.crud.camera_placement_crud import read_placed_channels
    from data_service.utils import snapshot_cache
    placed = read_placed_channels()
    with _lock:
        done = placed | _answered
    return [t for t in snapshot_cache._scanned_channels()
            if t not in done and not snapshot_cache.beyond(*t) and snapshot_cache.get(*t) is not None]


def run_round(now: Optional[float] = None) -> int:
    """Ask for every channel that needs it; returns how many calls were made."""
    global _failures, _next_try
    import device_identity
    from data_service.db.crud.camera_placement_crud import save_ai_answer, save_ai_refusal
    from data_service.utils import snapshot_cache
    now = time.time() if now is None else now
    if now < _next_try:
        return 0
    headers = device_identity.admin_auth_headers()
    if not headers:
        return 0   # no Device Key yet: the server would only say 401
    calls = 0
    for ip, channel in _wanted():
        jpeg = snapshot_cache.get_original(ip, channel)
        if not jpeg or len(jpeg) > MAX_JPEG:
            continue
        digest = hashlib.sha256(jpeg).hexdigest()
        with _lock:
            if _refused.get((ip, channel)) == digest:
                continue
        status, body = ask(jpeg, camera_label(ip, channel), headers)
        calls += 1
        if status == 200 and (answer := read_answer(body)):
            with _lock:
                _answered.add((ip, channel))
                _failures = 0
            try:
                save_ai_answer(ip, channel, answer)
            except Exception as e:
                logger.error(f"[placement] {camera_label(ip, channel)}: answer not saved: {e}")
            logger.info(f"[placement] {camera_label(ip, channel)}: {answer['choice']} "
                        f"(confidence {answer['confidence']}, {answer['model']})")
            continue
        if status == 400:
            error = str((body or {}).get("error") or "bad request")[:128]
            with _lock:
                _refused[(ip, channel)] = digest
            try:
                save_ai_refusal(ip, channel, error)
            except Exception as e:
                logger.error(f"[placement] {camera_label(ip, channel)}: refusal not saved: {e}")
            logger.warning(f"[placement] {camera_label(ip, channel)}: picture refused: {error}")
            continue
        if status == 401:
            _next_try = time.time() + KEY_PAUSE_S
            logger.warning(f"[placement] Device Key not accepted; asking again in {KEY_PAUSE_S // 60} min")
            return calls
        # 404 (server not updated yet), 5xx, timeout, a 200 with no label: try again later
        with _lock:
            _failures += 1
            wait = BACKOFF_S[min(_failures, len(BACKOFF_S)) - 1]
        _next_try = time.time() + wait
        logger.warning(f"[placement] server could not answer ({status or 'no connection'}); "
                       f"asking again in {wait // 60} min")
        return calls
    return calls


def _loop() -> None:
    while True:
        try:
            run_round()
        except Exception as e:
            logger.warning(f"[placement] round failed: {e}")
        time.sleep(ROUND_S)


def start() -> None:
    """Start the background thread once per process."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_loop, name="camera-placement", daemon=True).start()
