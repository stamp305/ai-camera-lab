"""Live and playback camera sessions: the admin is the only one who says yes.

    Alitech server --(service key)--> POST /ai-camera/api/live/start
        admin: one random path per camera (MediaMTX API),
               one publish token for the branch, one read token for the viewer
        admin --live_start (Socket.IO, TLS)--> branch: ffmpeg NVR -> WHIP -> MediaMTX
    viewer browser --WHEP + read token--> MediaMTX --hook--> admin (mtx_auth) -> 200/401

Secure by design / PDPA:
  * Tokens are random, bound to one path and one action, and die with the session
    (<= 30 min). Paths are random (s/<32 hex>), never a branch or camera name.
  * Publish and read are WebRTC only (WHIP/WHEP, media always DTLS-SRTP), each with
    its own token, and a path takes one publisher once; the MediaMTX API only with this
    service's own secret from loopback. Everything else is refused.
  * Nothing is stored: MediaMTX records nothing, and this keeps no frames.
  * Every start, first view, end and refusal goes to the audit log, with the viewer
    named by the portal (user id, never an e-mail).
  * A session nobody watches is stopped: no reader for READER_GRACE seconds.
"""
from __future__ import annotations

import hmac
import logging
import os
import re
import secrets
import threading
import time
from typing import Optional

import requests
from socketio.exceptions import TimeoutError as SocketIOTimeoutError

from extensions import socketio
from services import audit_log
from services.dlc_service import update_clients, update_clients_lock

logger = logging.getLogger(__name__)

MTX_API = os.getenv("AICAM_MTX_API", "http://127.0.0.1:9997").rstrip("/")
MTX_API_USER = "admin-live"
MTX_API_SECRET = os.getenv("AICAM_MTX_API_SECRET", "")
WHEP_BASE = os.getenv("AICAM_WHEP_BASE", "http://127.0.0.1:8889").rstrip("/")
MEDIA_PORT = int(os.getenv("AICAM_MEDIA_PORT", "8889"))
SERVICE_KEY = os.getenv("AICAM_LIVE_SERVICE_KEY", "")

SESSION_SECONDS = 30 * 60
READER_GRACE = 45            # seconds with no viewer before a stream is stopped
FIRST_READ_GRACE = 60        # a stream nobody opened at all
MAX_PER_REQUEST = 16
BRANCH_TIMEOUT = 25          # ffprobe + ffmpeg start on the branch
_TOKEN_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_PATH_RE = re.compile(r"^s/([0-9a-f]{32})$")

_lock = threading.Lock()
_sessions: dict[str, dict] = {}
_janitor_started = False


class LiveError(Exception):
    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code, self.status = code, status


def _token(n: int = 40) -> str:
    return "".join(secrets.choice(_TOKEN_ALPHABET) for _ in range(n))


def _eq(a: str, b: str) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a.encode(), b.encode())


def service_key_ok(given: str) -> bool:
    return _eq(given or "", SERVICE_KEY)


def configured() -> bool:
    return bool(SERVICE_KEY and MTX_API_SECRET)


# ---- branches -------------------------------------------------------------------------------

def _sid_of(client_id: str) -> Optional[str]:
    with update_clients_lock:
        meta = update_clients.get(client_id)
        return meta.get("sid") if meta else None


def _call(client_id: str, event: str, data: dict, timeout: int = BRANCH_TIMEOUT) -> dict:
    sid = _sid_of(client_id)
    if not sid:
        raise LiveError("branch_offline", 409)
    try:
        result = socketio.call(event, data, to=sid, timeout=timeout)
    except SocketIOTimeoutError:
        raise LiveError("branch_timeout", 504)
    except Exception:
        raise LiveError("branch_error", 502)
    if not isinstance(result, dict):
        raise LiveError("branch_error", 502)
    return result


def online_branches() -> list[dict]:
    with update_clients_lock:
        rows = [(cid, dict(meta)) for cid, meta in update_clients.items()]
    out = []
    for cid, meta in rows:
        if not meta.get("sid"):
            continue
        store = (meta.get("info") or {}).get("store") or {}
        out.append({"client_id": cid, "name": str(store.get("name") or cid)[:120],
                    "region": str(store.get("region") or "")[:60], "province": str(store.get("province") or "")[:60],
                    "live": bool(meta.get("live_capable"))})
    return out


def cameras(client_id: str) -> dict:
    result = _call(client_id, "live_cameras", {}, timeout=10)
    if not result.get("ok"):
        raise LiveError(result.get("error") or "branch_error", 502)
    return {"cameras": result.get("cameras") or [], "max_streams": result.get("max_streams"),
            "max_main": result.get("max_main"), "store": result.get("store") or {}}


SIREN_PATHS = {("GET", "/api/state"), ("GET", "/api/live"), ("GET", "/api/qr/status"),
               ("POST", "/api/qr"), ("POST", "/api/scan"), ("POST", "/api/ring"),
               ("POST", "/api/check"), ("POST", "/api/settings")}


# ---- LAN check ("is the browser on this branch's network?") ---------------------------------
# The branch reports {lan_ip, port} hourly (branch_net). For a check the admin hands the branch
# a one-use ticket (lan_ticket) and gives the portal the address + ticket + store code; the
# viewer's browser then calls the branch directly. Nothing here is persisted.

_NET_MAX_AGE = 3 * 3600          # older than this = the branch stopped reporting
_ORIGIN_RE = re.compile(r"^https?://[A-Za-z0-9.\-]+(:\d{1,5})?$")


def _ip(text: str):
    import ipaddress
    try:
        return ipaddress.ip_address((text or "").strip())
    except ValueError:
        return None


def set_net(client_id: str, sid: str, data, remote_addr: str) -> None:
    d = data if isinstance(data, dict) else {}
    ip = _ip(str(d.get("lan_ip") or ""))
    try:
        port = int(d.get("port"))
    except (TypeError, ValueError):
        return
    if ip is None or ip.version != 4 or not ip.is_private or ip.is_loopback or not 1024 <= port <= 65535:
        return
    with update_clients_lock:
        meta = update_clients.get(client_id)
        if meta and meta.get("sid") == sid:
            meta["net"] = {"lan_ip": str(ip), "port": port, "public_ip": remote_addr[:45], "at": time.time()}


def lan_ticket(client_id: str, viewer_ip: str, origin: str, viewer: str) -> dict:
    """Arm the branch's LAN check for one browser and tell the portal where to call."""
    if not _ORIGIN_RE.match(origin or ""):
        raise LiveError("bad_request")
    with update_clients_lock:
        meta = dict(update_clients.get(client_id) or {})
    net = meta.get("net") or {}
    if not meta.get("sid"):
        raise LiveError("branch_offline", 409)
    if not net or time.time() - net.get("at", 0) > _NET_MAX_AGE:
        raise LiveError("no_net", 404)
    code = str(((meta.get("info") or {}).get("store") or {}).get("code") or "")
    ticket = secrets.token_urlsafe(32)
    result = _call(client_id, "lan_ticket", {"ticket": ticket, "origin": origin}, timeout=8)
    if not result.get("ok"):
        raise LiveError(result.get("error") or "branch_error", 502)
    # Same public address = same way out to the internet. Only meaningful when both are public.
    a, b = _ip(viewer_ip), _ip(net.get("public_ip") or "")
    same_public = (a == b) if (a and b and a.is_global and b.is_global) else None
    audit_log.record("lan_check", client_id, None, "armed", viewer=viewer)
    return {"lan_ip": net["lan_ip"], "port": net["port"], "store_code": code, "ticket": ticket,
            "same_public": same_public, "reported_s": int(time.time() - net["at"])}


def siren_call(client_id: str, method: str, path: str, body: dict, viewer: str) -> dict:
    """Drive the branch's siren setup page (keys stay on the branch). Every change is audited."""
    if (method, path) not in SIREN_PATHS:
        raise LiveError("bad_request")
    result = _call(client_id, "siren_call", {"method": method, "path": path, "body": body}, timeout=45)
    if method == "POST":
        audit_log.record("siren_" + path.rsplit("/", 1)[-1], client_id, None,
                         "ok" if result.get("ok") else (result.get("error") or "failed"), viewer=viewer)
    if not result.get("ok"):
        raise LiveError(result.get("error") or "branch_error", 502)
    return {"status": result.get("status", 200), "body": result.get("body") or {}}


def record_spans(client_id: str, camera: int, channel: int, day: str) -> list:
    result = _call(client_id, "live_record_spans", {"camera": camera, "channel": channel, "day": day}, timeout=40)
    if not result.get("ok"):
        raise LiveError(result.get("error") or "branch_error", 502)
    return result.get("spans") or []


def record_days(client_id: str, camera: int, channel: int, month: str) -> list:
    result = _call(client_id, "live_record_days", {"camera": camera, "channel": channel, "month": month}, timeout=40)
    if not result.get("ok"):
        raise LiveError(result.get("error") or "branch_error", 502)
    return result.get("days") or []


# ---- MediaMTX API (loopback, own secret) ----------------------------------------------------

def _mtx(method: str, path: str, **kw):
    try:
        return requests.request(method, f"{MTX_API}{path}", auth=(MTX_API_USER, MTX_API_SECRET),
                                timeout=5, **kw)
    except requests.RequestException:
        logger.warning("[LIVE] media server unreachable")
        raise LiveError("media_server_down", 503)


def _mtx_add_path(path: str):
    r = _mtx("POST", f"/v3/config/paths/add/{path}", json={"record": False, "maxReaders": 4})
    if r.status_code >= 300:
        logger.warning(f"[LIVE] mediamtx add path failed: {r.status_code}")
        raise LiveError("media_server_error", 502)


def _mtx_remove_path(path: str):
    try:
        r = _mtx("DELETE", f"/v3/config/paths/delete/{path}")
        if r.status_code == 405:
            _mtx("POST", f"/v3/config/paths/delete/{path}")
    except (LiveError, Exception):
        logger.warning("[LIVE] mediamtx remove path failed")


def _mtx_readers(path: str) -> Optional[int]:
    try:
        r = _mtx("GET", f"/v3/paths/get/{path}")
        if r.status_code == 404:
            return 0
        return len(r.json().get("readers") or [])
    except Exception:
        return None


# ---- sessions -------------------------------------------------------------------------------

def start(client_id: str, items: list, mode: str, viewer: str,
          start_at: Optional[str] = None, end_at: Optional[str] = None,
          speed: int = 1, direction: str = "fwd", quality: str = "full") -> list[dict]:
    """Start one stream per {camera, channel, subtype}; answer what the viewer needs."""
    if not configured():
        raise LiveError("not_configured", 503)
    if mode not in ("live", "playback") or not isinstance(items, list) or not items:
        raise LiveError("bad_request")
    if len(items) > MAX_PER_REQUEST:
        raise LiveError("too_many")
    if speed not in (1, 2, 5, 10) or direction not in ("fwd", "rev") or quality not in ("full", "grid", "still"):
        raise LiveError("bad_request")
    _ensure_janitor()
    out = []
    for item in items:
        try:
            camera, channel = int(item["camera"]), int(item["channel"])
            subtype = 1 if int(item.get("subtype", 1)) else 0
        except Exception:
            raise LiveError("bad_request")
        sid = secrets.token_hex(16)
        path = f"s/{sid}"
        sess = {
            "sid": sid, "path": path, "client_id": client_id, "camera": camera, "channel": channel,
            "subtype": subtype, "mode": mode, "viewer": viewer, "start": start_at, "end": end_at,
            "publish_token": _token(), "read_token": _token(),
            "created": time.time(), "expires": time.time() + SESSION_SECONDS,
            "first_read": None, "last_reader": None, "published": False,
        }
        target = f"cam{camera}/ch{channel}/{'sub' if subtype else 'main'}"
        _mtx_add_path(path)
        with _lock:
            _sessions[sid] = sess
        try:
            result = _call(client_id, "live_start", {
                "sid": sid, "camera": camera, "channel": channel, "subtype": subtype, "mode": mode,
                "start": start_at, "end": end_at, "speed": speed, "direction": direction, "quality": quality,
                "media_port": MEDIA_PORT, "seconds": SESSION_SECONDS,
                "publish_token": sess["publish_token"],
            })
        except LiveError as exc:
            _forget(sid)
            audit_log.record(f"{mode}_start", client_id, target, exc.code, viewer=viewer)
            out.append({"camera": camera, "channel": channel, "ok": False, "error": exc.code})
            continue
        if not result.get("ok"):
            _forget(sid)
            audit_log.record(f"{mode}_start", client_id, target, result.get("error") or "refused", viewer=viewer)
            out.append({"camera": camera, "channel": channel, "ok": False, "error": result.get("error") or "refused"})
            continue
        audit_log.record(f"{mode}_start", client_id, target, "ok", viewer=viewer, session=sid[:8],
                         span=f"{start_at}..{end_at}" if mode == "playback" else None,
                         speed=f"{'-' if direction == 'rev' else ''}{speed}x" if mode == "playback" else None,
                         transcode=bool(result.get("transcode")))
        out.append({"camera": camera, "channel": channel, "ok": True, "session": sid,
                    "whep_url": f"{WHEP_BASE}/{path}/whep", "token": sess["read_token"],
                    "expires_in": SESSION_SECONDS})
    return out


def _forget(sid: str) -> Optional[dict]:
    with _lock:
        sess = _sessions.pop(sid, None)
    if sess:
        _mtx_remove_path(sess["path"])
    return sess


def stop(sid: str, why: str, viewer: Optional[str] = None) -> bool:
    with _lock:
        sess = _sessions.get(sid)
        if not sess or (viewer is not None and sess["viewer"] != viewer):
            return False
    sess = _forget(sid)
    if not sess:
        return False
    # Tell the branch without waiting for its answer: a viewer leaving a 4x4 grid stops 16
    # streams and must not wait on 16 round trips. The path is already gone from MediaMTX,
    # and the branch also stops at its own deadline and on disconnect.
    branch_sid = _sid_of(sess["client_id"])
    if branch_sid:
        socketio.emit("live_stop", {"sid": sid}, to=branch_sid)
    watched = int(time.time() - sess["first_read"]) if sess["first_read"] else 0
    audit_log.record(f"{sess['mode']}_end", sess["client_id"],
                     f"cam{sess['camera']}/ch{sess['channel']}", why,
                     viewer=sess["viewer"], session=sid[:8], seconds=watched)
    return True


def stop_branch(client_id: str, why: str = "branch_disconnect"):
    with _lock:
        sids = [s for s, v in _sessions.items() if v["client_id"] == client_id]
    for sid in sids:
        stop(sid, why)


def mtx_auth(body: dict, remote_ip: str) -> bool:
    """MediaMTX asks before every publish, read and API call."""
    action = str(body.get("action") or "")
    protocol = str(body.get("protocol") or "")
    path = str(body.get("path") or "")
    if action == "api":
        # body["ip"] is who called the MediaMTX API (remote_ip is MediaMTX itself).
        return str(body.get("ip") or "") in ("127.0.0.1", "::1") and body.get("user") == MTX_API_USER \
            and _eq(str(body.get("password") or ""), MTX_API_SECRET)
    m = _PATH_RE.match(path)
    if not m:
        return False
    with _lock:
        sess = _sessions.get(m.group(1))
        if not sess or sess["expires"] < time.time():
            return False
        if action == "publish" and protocol == "webrtc":
            ok = _eq(str(body.get("token") or ""), sess["publish_token"]) and not sess["published"]
            if ok:
                sess["published"] = True
            return ok
        if action == "read" and protocol == "webrtc":
            given = str(body.get("token") or body.get("password") or "")
            if not _eq(given, sess["read_token"]):
                return False
            first = sess["first_read"] is None
            if first:
                sess["first_read"] = time.time()
            sess["last_reader"] = time.time()
            viewer, client_id, cam, ch = sess["viewer"], sess["client_id"], sess["camera"], sess["channel"]
            mode = sess["mode"]
        else:
            return False
    if first:
        audit_log.record(f"{mode}_view_start", client_id, f"cam{cam}/ch{ch}", "ok",
                         viewer=viewer, session=m.group(1)[:8])
    return True


def _ensure_janitor():
    global _janitor_started
    with _lock:
        if _janitor_started:
            return
        _janitor_started = True

    def loop():
        while True:
            socketio.sleep(5)
            now = time.time()
            with _lock:
                items = list(_sessions.items())
            for sid, sess in items:
                if sess["expires"] <= now:
                    stop(sid, "expired")
                    continue
                readers = _mtx_readers(sess["path"])
                if readers:
                    sess["last_reader"] = now
                    continue
                if readers is None:
                    continue
                idle_since = sess["last_reader"] or sess["created"]
                grace = READER_GRACE if sess["first_read"] else FIRST_READ_GRACE
                if now - idle_since > grace:
                    stop(sid, "no_viewer")

    socketio.start_background_task(loop)
