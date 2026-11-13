"""One still picture per camera channel, taken in the background from app start.

Camera Selection used to ask the NVR for every channel's picture at the moment
the page opened. A Dahua NVR's /cgi-bin/snapshot.cgi often does not answer
within 5 s, for most channels even when asked one at a time, and which ones
fail changes between tries — so the grid came up with different pictures
missing on every visit. Reading one frame over RTSP works for every channel
(2-5 s each).

So this keeps one picture per (ip, channel) for the life of the process:
- a background thread walks every scanned channel, one at a time so the NVR is
  never flooded, and grabs a frame over RTSP (snapshot.cgi only as a fallback);
- a channel that has a picture is never captured again until the app restarts,
  unless someone presses Retake picture (retake);
- a channel that failed is tried again later, backing off from RETRY_INTERVAL_S
  (1, 2, 4, 8 ... min) — each try is up to ~14 s of waiting on the network
  (RTSP open + snapshot.cgi), one channel at a time — and after MAX_AUTO_TRIES
  failed tries the background stops (gave_up): an empty channel is not worth
  asking the NVR about for ever. Retry / Retake picture still work by hand;
- an all-black frame counts as a failure: an NVR can stream black for a camera
  that is gone;
- a channel without a picture is never dropped: Camera Selection lists every
  channel the NVR has (1..its channel count, see ensure_channels) and shows one
  with no picture as "No signal" with when it was last tried and a Retry button
  (retry_now). Zones drawn on a broken camera stay in sight;
- the NVR itself is asked what it knows about each channel (get_nvr_channel_states),
  so a missing picture can be told apart: "broken" (a camera is set up on the NVR
  but is not answering), "absent" (nothing is set up on that channel) or, past the
  NVR's own channel count, "beyond" (on a Dahua NVR the next channel streams its
  multi-view screen, not a camera). Absent and beyond channels are not tried; when
  the NVR will not say, a missing picture is just "no_signal";
- networks or channels added later are picked up on the next round.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import urllib.parse
from typing import Optional

logger = logging.getLogger(__name__)

RETRY_INTERVAL_S = 60       # between rounds while some channel still has no picture
IDLE_INTERVAL_S = 30        # between rounds once every channel has one (new cameras only)
RTSP_TIMEOUT_S = 8
MAX_BACKOFF_S = 15 * 60
MAX_AUTO_TRIES = 5          # failed background tries (1+2+4+8+16 min apart), then only a button press tries
BLACK_LEVEL = 24            # grey value below which a pixel counts as black

_cache: dict[tuple[str, int], bytes] = {}
_failures: dict[tuple[str, int], tuple[int, float, float]] = {}   # key -> (failed tries, next try at, last try at)
_channel_limit: dict[str, int] = {}                        # ip -> the NVR's channel count
_nvr_states: dict[str, tuple[float, dict[int, str]]] = {}  # ip -> (read at, {channel: absent|broken|connected})
STATES_TTL_S = 120
_lock = threading.Lock()
_started = False


def get(ip: str, channel: int) -> Optional[bytes]:
    with _lock:
        return _cache.get((ip, int(channel)))


def put(ip: str, channel: int, jpeg: bytes, replace: bool = False) -> None:
    """Keep the first picture of a channel; later ones replace it only on a Retake."""
    if not jpeg:
        return
    with _lock:
        if replace:
            _cache[(ip, int(channel))] = jpeg
        else:
            _cache.setdefault((ip, int(channel)), jpeg)


def forget_ip(ip: str) -> None:
    """Drop an NVR's pictures (its IP or credentials were edited)."""
    with _lock:
        for key in [k for k in _cache if k[0] == ip]:
            del _cache[key]
        for key in [k for k in _failures if k[0] == ip]:
            del _failures[key]
        _channel_limit.pop(ip, None)
        _nvr_states.pop(ip, None)


def _nvr_view(ip: str, channel: int) -> Optional[str]:
    """What the NVR says of a channel: "beyond", "absent", "broken", "connected", or None (unknown)."""
    limit = _channel_limit.get(ip)
    if limit is not None and int(channel) > limit:
        return "beyond"
    return _nvr_states.get(ip, (0.0, {}))[1].get(int(channel))


def status(ip: str, channel: int) -> tuple[str, Optional[float]]:
    """(state, last try epoch or None) for Camera Selection.

    state: "ok" | "pending" | "no_signal" | "broken" | "absent" | "beyond" (see the module doc).
    """
    key = (ip, int(channel))
    with _lock:
        if key in _cache:
            return "ok", None
        view = _nvr_view(ip, channel)
        last = _failures[key][2] if key in _failures else None
        if view in ("beyond", "absent"):
            return view, last
        if view == "broken":
            return "broken", last
        if key in _failures:
            return "no_signal", last
        return "pending", None


def note(ip: str, channel: int) -> Optional[str]:
    """The sentence a tile shows for its state, where the state needs one."""
    state, _ = status(ip, channel)
    if state == "beyond":
        return f"This NVR has only {_channel_limit.get(ip)} channels"
    if state == "absent":
        return "No camera is set up on this channel"
    if state == "broken":
        return "A camera is set up here but is not answering"
    return None


def gave_up(ip: str, channel: int) -> bool:
    """The background has stopped trying this channel; only Retry / Retake picture will."""
    with _lock:
        return _failures.get((ip, int(channel)), (0, 0.0, 0.0))[0] >= MAX_AUTO_TRIES


def _record(ip: str, channel: int, ok: bool, now: float) -> None:
    key = (ip, int(channel))
    with _lock:
        if ok:
            _failures.pop(key, None)
            return
        fails = _failures.get(key, (0, 0.0, 0.0))[0]
        delay = min(RETRY_INTERVAL_S * 2 ** fails, MAX_BACKOFF_S)
        _failures[key] = (fails + 1, now + delay, now)


def retry_now(ip: str, channel: int) -> tuple[str, Optional[float]]:
    """Try a channel at once (the Retry button); the result counts like a background try."""
    from data_service.db.crud.camera_crud import read_username_password_by_ip
    username, password = read_username_password_by_ip(ip)
    if username is None:
        return status(ip, channel)
    refresh_nvr_view(ip, username, password, force=True)   # a camera may just have been set up
    if status(ip, channel)[0] in ("beyond", "absent"):
        return status(ip, channel)
    ok = capture(ip, channel, username, password) is not None
    _record(ip, channel, ok, time.time())
    return status(ip, channel)


def retake(ip: str, channel: int) -> bool:
    """A new picture now, replacing the kept one (the Retake picture button).

    On failure the old picture stays: a camera that briefly drops out should not
    lose the picture people draw zones on.
    """
    from data_service.db.crud.camera_crud import read_username_password_by_ip
    username, password = read_username_password_by_ip(ip)
    if username is None:
        return False
    ok = capture(ip, channel, username, password, replace=True) is not None
    if ok:
        _record(ip, channel, True, time.time())
    return ok


def _is_black(frame) -> bool:
    import cv2
    small = cv2.resize(frame, (160, 90), interpolation=cv2.INTER_AREA)
    grey = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    return float((grey > BLACK_LEVEL).mean()) < 0.01


MAX_CHANNEL = 255           # camera_channel.channel is a TINYINT UNSIGNED

THUMB_MAX_WIDTH = 1280


def grab_rtsp_frame(ip: str, channel: int, username: str, password: str) -> Optional[bytes]:
    """One JPEG from the channel's main stream over RTSP, at most THUMB_MAX_WIDTH wide, or None.

    The main stream, not the sub-stream: a Dahua sub-stream is commonly 704x576 —
    the whole 16:9 view squeezed sideways — so in the page's 16:9 tile it came out
    cropped top and bottom, unlike the same camera on Live Display. Shrinking the
    main-stream frame keeps the real proportions for any camera.
    """
    import cv2
    from data_service.configs.search_camera_config import RTSP_URL

    url = RTSP_URL.format(ip=ip, port=554, channel=channel,
                          username=urllib.parse.quote(username or "", safe=""),
                          password=urllib.parse.quote(password or "", safe=""))
    os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS",
                          f"rtsp_transport;tcp|stimeout;{RTSP_TIMEOUT_S * 1_000_000}")
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    try:
        if not cap.isOpened():
            return None
        for _ in range(10):  # the first frames after connecting can be empty
            ok, frame = cap.read()
            if ok and frame is not None:
                if _is_black(frame):
                    return None
                h, w = frame.shape[:2]
                if w > THUMB_MAX_WIDTH:
                    frame = cv2.resize(frame, (THUMB_MAX_WIDTH, round(h * THUMB_MAX_WIDTH / w)),
                                       interpolation=cv2.INTER_AREA)
                ok, jpeg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
                return jpeg.tobytes() if ok else None
        return None
    finally:
        cap.release()


def capture(ip: str, channel: int, username: str, password: str, replace: bool = False) -> Optional[bytes]:
    """RTSP first, snapshot.cgi as a fallback; caches what it gets."""
    jpeg = None
    try:
        jpeg = grab_rtsp_frame(ip, channel, username, password)
    except Exception as e:
        logger.warning(f"[snapshot] RTSP frame {ip} ch{channel} failed: {e}")
    if not jpeg:
        try:
            from data_service.utils.search_camera_utils import get_image_from_camera_digest_auth
            jpeg = get_image_from_camera_digest_auth(ip, channel, username, password, timeout=5.0)
            if jpeg:
                import cv2, numpy as np
                frame = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
                if frame is None or _is_black(frame):
                    jpeg = None
        except Exception as e:
            logger.warning(f"[snapshot] snapshot.cgi {ip} ch{channel} failed: {e}")
    if jpeg:
        put(ip, channel, jpeg, replace=replace)
    return jpeg


def ensure_camera_channels(ip: str, channels) -> int:
    """List channels of an NVR that are not in camera_channel yet; returns how many were added."""
    from data_service.db.crud.camera_crud import ensure_camera_channels as _ensure
    return _ensure(ip, channels)


def refresh_nvr_view(ip: str, username: str, password: str, force: bool = False) -> None:
    """Read the NVR's channel count once, and what it says of each channel every STATES_TTL_S."""
    from data_service.utils.search_camera_utils import get_nvr_channel_count, get_nvr_channel_states
    with _lock:
        have_count = ip in _channel_limit
        fresh = time.time() - _nvr_states.get(ip, (0.0, {}))[0] < STATES_TTL_S
    if not have_count:
        count = get_nvr_channel_count(ip, username, password)
        if count:
            with _lock:
                _channel_limit[ip] = count
    if force or not fresh:
        states = get_nvr_channel_states(ip, username, password)
        with _lock:
            before = _nvr_states.get(ip, (0.0, {}))[1]
            _nvr_states[ip] = (time.time(), states or {})
        if states and states != before:
            kinds: dict[str, int] = {}
            for v in states.values():
                kinds[v] = kinds.get(v, 0) + 1
            logger.info(f"[snapshot] {ip}: NVR reports channels {kinds}")


def _networks() -> dict[str, int]:
    """{ip: channel count} of every network."""
    from data_service.db.session import get_db
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT ip, MAX(channel_count) FROM camera_master GROUP BY ip")
        return {ip: int(n or 16) for ip, n in cur.fetchall()}
    finally:
        conn.close()


def _scanned_channels() -> list[tuple[str, int]]:
    from data_service.db.session import get_db
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT cc.ip, cc.channel FROM camera_channel cc "
                    "JOIN camera_master cm ON cm.ip = cc.ip WHERE cc.channel <= cm.channel_count "
                    "ORDER BY cc.ip, cc.channel")
        return [(ip, int(ch)) for ip, ch in cur.fetchall()]
    finally:
        conn.close()


def _prefetch_round(now: float | None = None) -> list[tuple[str, int]]:
    """One pass over every scanned channel without a picture; returns those still missing."""
    from data_service.db.crud.camera_crud import read_username_password_by_ip
    now = time.time() if now is None else now
    credentials: dict[str, tuple] = {}

    def creds(ip):
        if ip not in credentials:
            credentials[ip] = read_username_password_by_ip(ip)
        return credentials[ip]

    # Every network lists channels 1..its count (a network made before that choice,
    # or one whose count was raised, may lack rows), and the NVR is asked what it
    # knows of them.
    for ip, count in _networks().items():
        username, password = creds(ip)
        if username is None:
            continue
        try:
            ensure_camera_channels(ip, range(1, min(count, MAX_CHANNEL) + 1))
        except Exception as e:
            logger.warning(f"[snapshot] could not list {ip}'s channels: {e}")
        refresh_nvr_view(ip, username, password)
    targets = [t for t in _scanned_channels() if status(*t)[0] not in ("beyond", "absent")]

    for ip, channel in [t for t in targets if get(*t) is None]:
        with _lock:
            next_at = _failures.get((ip, channel), (0, 0.0, 0.0))[1]
        if now < next_at or gave_up(ip, channel):
            continue  # backing off, or left to the Retry button
        username, password = creds(ip)
        if username is None:
            continue
        t0 = time.time()
        ok = capture(ip, channel, username, password) is not None
        _record(ip, channel, ok, now)
        logger.info(f"[snapshot] {ip} ch{channel}: {'cached' if ok else 'no signal'} ({time.time() - t0:.1f}s)")
    return [t for t in targets if get(*t) is None]


def _prefetch_loop() -> None:
    while True:
        missing = []
        try:
            missing = _prefetch_round()
        except Exception as e:
            logger.warning(f"[snapshot] prefetch round failed: {e}")
        time.sleep(RETRY_INTERVAL_S if missing else IDLE_INTERVAL_S)


def start_prefetch() -> None:
    """Start the background thread once per process."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_prefetch_loop, name="snapshot-prefetch", daemon=True).start()
