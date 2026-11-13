# frontend/services/remote_tunnel.py
"""ท่อรีโมตที่วิ่งบน Socket.IO เส้นเดิม

เดิมท่อเป็น TCP พอร์ต 65434 แยกต่างหาก ซึ่งกลายเป็นภาระตอน deploy จริง:
ต้องเปิดพอร์ตที่สองที่ไฟร์วอลล์/NAT, ไม่มี TLS, reverse proxy ส่วนใหญ่ส่งต่อได้แต่
HTTP, และบริการ tunnel สาธารณะมักคิดเงินเฉพาะ TCP

เส้น Socket.IO ที่สาขาต่อค้างไว้อยู่แล้วผ่าน https ของจริง (SOCKET_SERVER_PATH=
/ai-camera/socket.io) จึงเอามาใช้เป็นท่อได้เลย — โปรโตคอลเฟรมเหมือนเดิมทุกอย่าง
(hello/req/head/data/end/err/abort) เปลี่ยนแค่ตัวขนส่ง

state machine อยู่ที่ frontend เพราะที่นี่คือที่ที่รู้จัก sid ของแต่ละสาขา
"""
from __future__ import annotations

import base64
import os
import queue
import secrets
import threading
import time
import uuid
from typing import Any, Dict, Iterator, List, Optional, Tuple

SESSION_TTL = 30 * 60
TOKEN_TTL = 60
HEAD_TIMEOUT = 30
IDLE_TIMEOUT = 60
# เพดานจำนวน request ที่ยังไม่จบพร้อมกันต่อเซสชัน — ไม่ใช่แค่ "สตรีม"
# ตั้ง 4 ไว้ตอนแรกโดยคิดถึงแต่ MJPEG แต่หน้าเลือกกล้องยิงรูปพรีวิวทีเดียว 17 ใบ
# ที่เหลือเลยโดนปฏิเสธ 502 ทันทีและหน้าจอว่างเปล่า
MAX_STREAMS = int(os.getenv("AI_CAMERA_ADMIN_REMOTE_MAX_INFLIGHT", "24"))

# เพดานจำนวนเซสชันรีโมตที่เปิดพร้อมกันทั้งระบบ
# ตัวที่กินทรัพยากรจริงคือสตรีมกล้อง (วัดแล้ว 4 สตรีมพร้อมกัน = 19 Mbps ผ่าน
# คอนเนกชันเดียว) ไม่ใช่จำนวนสาขาที่ต่อค้างไว้ — จำกัดตรงนี้จึงตรงจุดกว่า
MAX_SESSIONS = int(os.getenv("AI_CAMERA_ADMIN_REMOTE_MAX_SESSIONS", "5"))

# heartbeat ของ "ท่อ" โดยเฉพาะ — แยกจาก ping ของ Socket.IO
# Socket.IO ตั้ง ping_timeout ไว้ 60 วิเพื่อให้ทนสตรีมกล้องหนัก ๆ ผลคือกว่าจะรู้ว่า
# สาขาหลุดต้องรอ ~80 วิ (วัดจริงจากการตัดเน็ตที่สาขา) ซึ่งนานเกินไปสำหรับคนที่
# กำลังนั่งรีโมตอยู่ — heartbeat นี้ทำงานเฉพาะตอนมีเซสชันเปิด จึงรู้ตัวได้ใน ~12 วิ
# โดยไม่ต้องไปลด ping_timeout ที่จะกระทบความเสถียรของสตรีม
HEARTBEAT_INTERVAL = int(os.getenv("AI_CAMERA_ADMIN_REMOTE_HEARTBEAT", "5"))
HEARTBEAT_TIMEOUT = int(os.getenv("AI_CAMERA_ADMIN_REMOTE_HEARTBEAT_TIMEOUT", "12"))
QUEUE_MAXSIZE = 64

HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length", "host",
})

_sessions: Dict[str, "_Session"] = {}
_pending_tokens: Dict[str, Tuple[str, float]] = {}
_lock = threading.RLock()

# The branch's pages are served from a separate origin (remote_origin.py), so
# script in them can never act as the admin page. That origin lets a browser in
# only with a viewer key, handed over once through an enter ticket: the ticket
# rides in the iframe URL (it can leak to history and logs), so it works once,
# for 60 seconds; the key it buys sits in an HttpOnly cookie for the session.
ENTER_TICKET_TTL = 60
_enter_tickets: Dict[str, Tuple[str, float]] = {}   # ticket -> (client_id, expires_at)
_viewer_keys: Dict[str, str] = {}                     # client_id -> viewer key


class RemoteError(RuntimeError):
    """เรียกสาขาไม่สำเร็จ (ไม่มีเซสชัน / timeout / สาขาตอบ err)"""


def strip_hop_by_hop(headers: Dict[str, str]) -> Dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in HOP_BY_HOP}


class _Pending:
    def __init__(self, req_id: str) -> None:
        self.id = req_id
        self.head: "queue.Queue[Any]" = queue.Queue(maxsize=1)
        # จำกัดคิว: MJPEG ผลิตเร็วกว่าที่แอดมินดูดได้ ถ้าไม่จำกัดคือ RAM บวม
        self.data: "queue.Queue[Any]" = queue.Queue(maxsize=QUEUE_MAXSIZE)
        self.closed = False


class _Session:
    def __init__(self, client_id: str, sid: str, emit) -> None:
        self.client_id = client_id
        self.sid = sid
        self._emit = emit
        self.expires_at = time.time() + SESSION_TTL
        self.pending: Dict[str, _Pending] = {}
        self.lock = threading.Lock()
        self.last_seen = time.time()          # ได้ยินเสียงสาขาครั้งล่าสุดเมื่อไหร่
        self.stop_heartbeat = threading.Event()

    def alive(self) -> bool:
        return time.time() < self.expires_at

    def send(self, frame: Dict[str, Any]) -> None:
        self._emit("remote_frame", frame, to=self.sid)

    def close(self) -> None:
        self.stop_heartbeat.set()
        with self.lock:
            pending = list(self.pending.values())
            self.pending.clear()
        for p in pending:
            p.closed = True
            _offer(p.head, RemoteError("remote session closed"))
            _offer(p.data, None)


def _offer(q: "queue.Queue[Any]", item: Any) -> bool:
    """ใส่คิวแบบไม่บล็อก — ห้ามบล็อกเธรดที่รับอีเวนต์ Socket.IO เด็ดขาด"""
    try:
        q.put_nowait(item)
        return True
    except queue.Full:
        return False


# --------------------------------------------------------------------------------------
# เปิด/ปิดเซสชัน
# --------------------------------------------------------------------------------------
class SessionLimitReached(RuntimeError):
    """เปิดเซสชันใหม่ไม่ได้เพราะเต็มเพดานแล้ว"""


def mint_token(client_id: str) -> Dict[str, Any]:
    # นับเฉพาะเซสชันที่ยังไม่หมดอายุ และไม่นับสาขาที่กำลังจะต่อกลับมาเอง
    with _lock:
        live = [cid for cid, sess in _sessions.items()
                if sess.alive() and cid != client_id]
    if len(live) >= MAX_SESSIONS:
        raise SessionLimitReached(
            f"มีเซสชันรีโมตเปิดอยู่ครบ {MAX_SESSIONS} รายการแล้ว "
            f"({', '.join(live)}) — ปิดสักตัวก่อน")

    token = secrets.token_urlsafe(32)
    with _lock:
        _pending_tokens[token] = (client_id, time.time() + TOKEN_TTL)
    return {"token": token, "transport": "socketio", "notify": True}


def register_session(client_id: str, sid: str, token: str, emit) -> bool:
    """สาขาส่ง hello กลับมาพร้อม token — จับคู่เป็นเซสชัน"""
    now = time.time()
    with _lock:
        for tok, (_cid, exp) in list(_pending_tokens.items()):
            if exp < now:
                del _pending_tokens[tok]
        entry = _pending_tokens.pop(token, None)
    if not entry or entry[0] != client_id:
        return False

    session = _Session(client_id, sid, emit)
    with _lock:
        old = _sessions.pop(client_id, None)
        _sessions[client_id] = session
    if old:
        old.close()

    threading.Thread(target=_heartbeat_loop, args=(session,), daemon=True).start()
    return True


def _heartbeat_loop(session: "_Session") -> None:
    """เคาะถามสาขาเป็นระยะ ถ้าเงียบเกินกำหนดถือว่าหลุดแล้วเก็บกวาดทันที"""
    while not session.stop_heartbeat.wait(HEARTBEAT_INTERVAL):
        with _lock:
            current = _sessions.get(session.client_id)
        if current is not session or not session.alive():
            return

        quiet = time.time() - session.last_seen
        if quiet > HEARTBEAT_TIMEOUT:
            close_session(session.client_id)
            try:
                from services import audit_log
                audit_log.record("remote_dropped", session.client_id,
                                 result="heartbeat_timeout", quiet_seconds=round(quiet, 1))
            except Exception:
                pass
            return

        try:
            session.send({"t": "ping"})
        except Exception:
            # ส่งไม่ออกแล้ว = คอนเนกชันตายไปแล้ว ไม่ต้องรอ timeout
            close_session(session.client_id)
            return


def close_session(client_id: str) -> bool:
    with _lock:
        session = _sessions.pop(client_id, None)
        _viewer_keys.pop(client_id, None)
        for tok, (cid, _exp) in list(_enter_tickets.items()):
            if cid == client_id:
                del _enter_tickets[tok]
    if not session:
        return False
    session.close()
    return True


def mint_enter_ticket(client_id: str) -> str:
    """A one-use ticket for the remote origin; a fresh viewer key comes with it,
    so a browser that entered before this open must enter again."""
    ticket = secrets.token_urlsafe(32)
    now = time.time()
    with _lock:
        for tok, (_cid, exp) in list(_enter_tickets.items()):
            if exp < now:
                del _enter_tickets[tok]
        _enter_tickets[ticket] = (client_id, now + ENTER_TICKET_TTL)
        _viewer_keys[client_id] = secrets.token_urlsafe(32)
    return ticket


def redeem_enter_ticket(client_id: str, ticket: str) -> Optional[str]:
    """The viewer key for a valid, unused ticket of this client's live session."""
    with _lock:
        entry = _enter_tickets.pop(ticket, None)
        if not entry or entry[0] != client_id or entry[1] < time.time():
            return None
        session = _sessions.get(client_id)
        if not session or not session.alive():
            return None
        return _viewer_keys.get(client_id)


def viewer_key_ok(client_id: str, key: str) -> bool:
    with _lock:
        expected = _viewer_keys.get(client_id)
        session = _sessions.get(client_id)
    return bool(expected and key and session and session.alive()
                and secrets.compare_digest(expected, key))


def session_status(client_id: str) -> Dict[str, Any]:
    with _lock:
        session = _sessions.get(client_id)
    if not session or not session.alive():
        return {"active": False}
    with session.lock:
        streams = len(session.pending)
    return {"active": True,
            "expires_in": int(session.expires_at - time.time()),
            "streams": streams}


def session_sid(client_id: str) -> Optional[str]:
    """sid ที่เซสชันนี้ผูกอยู่ — ใช้ตรวจว่ายังตรงกับคอนเนกชันปัจจุบันของสาขาไหม"""
    with _lock:
        session = _sessions.get(client_id)
    return session.sid if session else None


def active_sessions() -> List[str]:
    with _lock:
        return [cid for cid, s in _sessions.items() if s.alive()]


# --------------------------------------------------------------------------------------
# ยิง request ลอดท่อ
# --------------------------------------------------------------------------------------
def open_request(client_id: str, method: str, path: str,
                 headers: Dict[str, str], body: bytes = b"",
                 req_id: Optional[str] = None
                 ) -> Tuple[int, Dict[str, str], Iterator[bytes]]:
    with _lock:
        session = _sessions.get(client_id)
    if not session or not session.alive():
        raise RemoteError(f"ยังไม่มีเซสชันรีโมตของ '{client_id}'")

    with session.lock:
        if len(session.pending) >= MAX_STREAMS:
            raise RemoteError(
                f"มี request ค้างอยู่ครบ {MAX_STREAMS} รายการแล้ว รอสักครู่")
        req_id = req_id or uuid.uuid4().hex
        pending = _Pending(req_id)
        session.pending[req_id] = pending

    try:
        session.send({
            "t": "req", "id": req_id, "method": method.upper(), "path": path,
            "headers": strip_hop_by_hop(headers),
            "body_b64": base64.b64encode(body).decode("ascii"),
        })
    except Exception as exc:
        _drop(session, req_id)
        raise RemoteError(f"ส่ง request ไม่สำเร็จ: {exc}") from exc

    try:
        head = pending.head.get(timeout=HEAD_TIMEOUT)
    except queue.Empty:
        _drop(session, req_id, abort=True)
        raise RemoteError(f"สาขาไม่ตอบ header ภายใน {HEAD_TIMEOUT} วินาที")

    if isinstance(head, RemoteError):
        _drop(session, req_id)
        raise head

    status = int(head.get("status", 502))
    resp_headers = strip_hop_by_hop(head.get("headers") or {})
    return status, resp_headers, _body_iter(session, pending)


def abort_request(client_id: str, req_id: str) -> bool:
    with _lock:
        session = _sessions.get(client_id)
    if not session:
        return False
    with session.lock:
        pending = session.pending.get(req_id)
    if not pending:
        return False
    pending.closed = True
    _offer(pending.data, None)
    _drop(session, req_id, abort=True)
    return True


def _body_iter(session: _Session, pending: _Pending) -> Iterator[bytes]:
    finished = False
    try:
        while True:
            try:
                chunk = pending.data.get(timeout=IDLE_TIMEOUT)
            except queue.Empty:
                break
            if chunk is None:
                finished = True
                break
            if isinstance(chunk, RemoteError):
                finished = True
                break
            yield chunk
    finally:
        _drop(session, pending.id, abort=not finished and not pending.closed)


def _drop(session: _Session, req_id: str, abort: bool = False) -> None:
    with session.lock:
        pending = session.pending.pop(req_id, None)
    if pending:
        pending.closed = True
    if abort:
        try:
            session.send({"t": "abort", "id": req_id})
        except Exception:
            pass


# --------------------------------------------------------------------------------------
# รับเฟรมจากสาขา
# --------------------------------------------------------------------------------------
def on_frame(client_id: str, frame: Dict[str, Any]) -> None:
    """เรียกจาก socket_handlers เมื่อสาขา emit remote_frame กลับมา"""
    kind = frame.get("t")

    with _lock:
        session = _sessions.get(client_id)
    if session is None:
        return

    # เฟรมอะไรก็ได้ถือว่าสาขายังมีชีวิต ไม่ใช่แค่ pong
    session.last_seen = time.time()

    if kind in ("pong", "hello"):
        return

    req_id = frame.get("id")
    with session.lock:
        pending = session.pending.get(req_id) if req_id else None
    if pending is None:
        return

    if kind == "head":
        _offer(pending.head, frame)
    elif kind == "data":
        chunk = base64.b64decode(frame.get("b64") or "")
        if not _offer(pending.data, chunk):
            # แอดมินดูดไม่ทัน — ตัดสตรีมทิ้งดีกว่าปล่อยให้บวม
            _drop(session, req_id, abort=True)
    elif kind == "end":
        _offer(pending.data, None)
    elif kind == "err":
        err = RemoteError(frame.get("msg") or "client error")
        if not _offer(pending.head, err):
            _offer(pending.data, err)
