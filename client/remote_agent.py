# remote_agent.py
"""ฝั่ง client ของ remote browser — ให้แอดมินเปิดหน้าเว็บของเครื่องนี้ดูจากระยะไกล

แอดมิน emit "open_remote" มาทาง Socket.IO เดิม พร้อม token แล้วเราหมุน TCP กลับไป
หาพอร์ต 65434 ของแอดมิน จากนั้นทำตัวเป็นตัวแทนยิง HTTP เข้า frontend_web ของเครื่องนี้
(127.0.0.1:3000) แล้วส่ง response กลับลอดท่อเดิม

เป็น outbound connection ล้วน ไม่ต้องเปิดพอร์ตอะไรที่หน้าร้าน

ดูสเปก frame ที่ backend/remote_protocol.py ฝั่งแอดมิน
"""
from __future__ import annotations

import base64
import json
import logging
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from typing import Any, Dict, Optional, Set

import requests

logger = logging.getLogger(__name__)

# ยิงได้แค่ loopback และเฉพาะพอร์ตที่เป็นของแอปนี้ — กัน token ที่หลุดถูกใช้
# ไล่ scan วงแลนของลูกค้า (SSRF)
ALLOWED_HOST = "127.0.0.1"


def _frontend_port():
    """พอร์ตเว็บของเครื่องนี้ตามคอนฟิกจริง

    เคย fix ไว้ที่ 3000 ซึ่งพังทันทีถ้าสาขาไปเปลี่ยนพอร์ต frontend_web ในหน้า
    จัดการพอร์ต — รีโมตจะยิงไปพอร์ตเก่าที่ไม่มีใครฟังแล้ว
    """
    try:
        import port_store
        return port_store.get_port("frontend_web", 3000)
    except Exception as exc:
        logger.warning(f"[REMOTE] อ่านพอร์ต frontend_web ไม่ได้: {exc}")
    return 3000


DEFAULT_PORT = _frontend_port()


def _allowed_ports():
    """พอร์ตของบริการตัวเองตามตาราง service_ports (ผ่าน port_store)

    ต้องเปิดมากกว่าแค่ 3000 เพราะภาพกล้องไม่ได้วิ่งผ่านหน้าเว็บ — frontend_web
    proxy แบบ buffer ทั้งก้อน (main.py: resp.content) สตรีมจึงไม่มีทางผ่านมันได้
    ของจริงเบราว์เซอร์ยิงตรงไปที่พอร์ตของ camera_service
    """
    ports = {DEFAULT_PORT}
    try:
        import port_store
        for value in port_store.port_map().values():
            try:
                ports.add(int(str(value)))
            except (TypeError, ValueError):
                continue
    except Exception as exc:
        logger.warning(f"[REMOTE] อ่านตารางพอร์ตไม่ได้: {exc}")
    return ports


ALLOWED_PORTS = _allowed_ports()

# path พิเศษสำหรับยิงข้ามไปพอร์ตอื่นของเครื่องเดียวกัน: /__port/8003/<ของจริง>
PORT_PREFIX = "/__port/"

# path สำหรับอ่านไฟล์ในเครื่องสาขา (ไม่ได้ยิง HTTP ต่อ — อ่านจากดิสก์ตรง ๆ)
FS_PREFIX = "/__fs/"


def _allowed_roots():
    """โฟลเดอร์ที่แอดมินอ่านได้ผ่านรีโมต

    เคยเปิดทั้งเครื่อง ("/") ตอนทดสอบ แต่ปิดกลับแล้วเพราะไม่ได้ใช้จริง — token
    รีโมตหนึ่งใบไม่ควรอ่านได้ทุกไฟล์ในเครื่องลูกค้า (.env, คีย์, ไฟล์ส่วนตัว)

    เหลือเฉพาะที่จำเป็นกับงาน: โฟลเดอร์รูปสำหรับปุ่มอัปโหลด และโฟลเดอร์ของแอปเอง
    ขยายได้ด้วย env AI_CAMERA_REMOTE_FS_ROOTS (คั่นด้วย ":") ถ้าวันหลังต้องใช้
    """
    configured = os.getenv("AI_CAMERA_REMOTE_FS_ROOTS", "").strip()
    if configured:
        roots = [os.path.realpath(os.path.expanduser(p))
                 for p in configured.split(":") if p.strip()]
        return [r for r in roots if os.path.isdir(r)]

    app_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.realpath(app_dir),
        os.path.realpath(os.path.expanduser("~/Pictures")),
        os.path.realpath(os.path.expanduser("~/Desktop")),
        os.path.realpath(os.path.expanduser("~/Downloads")),
    ]
    return [r for r in candidates if os.path.isdir(r)]


def _picker_roots():
    """จุดตั้งต้นของตัวเลือกไฟล์ — เท่ากับขอบเขตที่อนุญาตพอดี ไม่มีอะไรเกินนั้น"""
    return list(ALLOWED_ROOTS)


ALLOWED_ROOTS = _allowed_roots()

# กันดูดไฟล์ใหญ่จนเน็ตร้านตัน
MAX_FILE_BYTES = 20 * 1024 * 1024


# งานไฟล์บางอย่าง (listdir/open) จะ "ค้าง" เมื่อ macOS TCC กันโฟลเดอร์ที่ต้องขอ
# สิทธิ์ (Desktop/Downloads/Documents) แล้วแอปยังไม่ได้รับอนุญาต — บนสาขาแอปเปิด
# จาก launchd จึงติดเสมอ ปล่อยให้ค้างจะกิน inflight slot ของท่อทิ้งไปเรื่อย ๆ
# จึงรันในเธรดแล้วตั้งเพดานเวลา ถ้าเกินให้ถือว่าถูกกันสิทธิ์
FS_OP_TIMEOUT = 5


class _FSBlocked(Exception):
    pass


def _fs_call(fn):
    box = {}
    def run():
        try:
            box["v"] = fn()
        except Exception as e:  # noqa: BLE001 — ส่งต่อชนิดเดิมให้ผู้เรียกจัดการ
            box["e"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(FS_OP_TIMEOUT)
    if t.is_alive():
        raise _FSBlocked()
    if "e" in box:
        raise box["e"]
    return box.get("v")


def _safe_path(raw):
    """คืน path จริงถ้าอยู่ใต้ root ที่อนุญาต ไม่งั้น None

    ใช้ realpath ก่อนเทียบ เพื่อกันทั้ง .. และ symlink ที่ชี้ออกนอกเขต
    """
    if not raw:
        return None
    target = os.path.realpath(os.path.expanduser(raw))
    for root in ALLOWED_ROOTS:
        # root "/" ต้องแยกเคส ไม่งั้นเทียบ prefix เป็น "//" แล้วไม่ตรงอะไรเลย
        prefix = root if root.endswith(os.sep) else root + os.sep
        if target == root or target.startswith(prefix):
            return target
    return None


def _resolve_target(path):
    """แปลง path ที่ได้จากแอดมินเป็น (url เต็ม, path ที่จะส่งต่อ) หรือ None ถ้าไม่อนุญาต"""
    port = DEFAULT_PORT
    if path.startswith(PORT_PREFIX):
        rest = path[len(PORT_PREFIX):]
        head, _, tail = rest.partition("/")
        try:
            port = int(head)
        except ValueError:
            return None
        path = "/" + tail
    if port not in ALLOWED_PORTS:
        logger.warning(f"[REMOTE] ปฏิเสธพอร์ต {port} ที่ไม่ได้อยู่ในรายการอนุญาต")
        return None
    return f"http://{ALLOWED_HOST}:{port}", path

# ก้อนใหญ่ขึ้นเพื่อลดจำนวนอีเวนต์ต่อวินาทีบน Socket.IO — สตรีมกล้องยิงถี่มาก
# แล้วเบียดจน request อื่นบนคอนเนกชันเดียวกันไม่ได้คิว
CHUNK_SIZE = 64 * 1024

# เพดานความเร็วของ "หนึ่งสตรีม" (ไบต์/วินาที)
# ท่อกับ ping ของ Socket.IO ใช้คอนเนกชันเดียวกัน ถ้าปล่อยให้กล้องยิงเต็มสปีด
# คิวส่งจะยาวจน pong ไปไม่ทัน แล้วเซิร์ฟเวอร์ตัดคอนเนกชันทิ้งทั้งเส้น
# ตอนนี้สตรีมภาพ "ทิ้งเฟรมเก่า" แล้ว ค่านี้จึงคุมแค่จังหวะส่ง ไม่ได้ทำให้ภาพล้าหลัง
# (เฟรมหนึ่งของกล้องจริง ~237 KB ค่านี้ต้องมากกว่านั้นหลายเท่าถึงจะได้หลาย fps)
# ปิดการหน่วงเป็นค่าเริ่มต้น — ส่งเท่าที่เส้นทางรับไหว ถ้าเจอปัญหาท่อหลุด
# ค่อยตั้งค่านี้ (ไบต์/วินาที) ทีหลัง
# เพดานต่อสตรีม: จำเป็นเพราะท่อกับสัญญาณควบคุมใช้คอนเนกชันเดียวกัน วัดจริงแล้ว
# 4 คนดูพร้อมกัน (21 Mbps) ทำให้ Socket.IO หลุดและหน้าเว็บค้าง 13 วินาที
# ตอนนี้สตรีม "ทิ้งเฟรมเก่า" แล้ว การจำกัดจึงลดแค่ fps ไม่ได้ทำให้ภาพล้าหลัง
STREAM_MAX_BPS = int(os.getenv("AI_CAMERA_REMOTE_STREAM_BPS", str(600 * 1024)))
CONNECT_TIMEOUT = 10
# read timeout ต้องไม่มี ไม่งั้น MJPEG โดนตัดกลางคัน
UPSTREAM_TIMEOUT = (CONNECT_TIMEOUT, None)

HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length",
    "host",
})

_agent_lock = threading.Lock()
_agent: Optional["RemoteAgent"] = None

# callback ให้ menubar เอาไปขึ้น banner ว่ากำลังถูกรีโมต (ตั้งจาก client_logic)
on_state_change = None


# ไฟล์สถานะให้โพรเซสอื่น (menubar) รู้ว่ากำลังถูกรีโมตอยู่ — คนละโพรเซสกันเลย
# แชร์ตัวแปรตรง ๆ ไม่ได้
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "remote_session.json")


def _write_state(active: bool, detail: str) -> None:
    try:
        with open(STATE_FILE, "w") as fh:
            json.dump({"active": active, "detail": detail, "updated_at": time.time()}, fh)
    except OSError as exc:
        logger.error(f"[REMOTE] เขียนไฟล์สถานะไม่ได้: {exc}")


def _desktop_notification(title: str, message: str) -> None:
    """เด้ง notification ให้คนหน้าร้านรู้ตัว — เงียบ ๆ ไม่ได้"""
    if sys.platform != "darwin":
        return
    try:
        subprocess.Popen(
            ["osascript", "-e",
             f'display notification "{message}" with title "{title}"'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        logger.error(f"[REMOTE] เด้ง notification ไม่ได้: {exc}")


def _notify(active: bool, detail: str = "") -> None:
    logger.info(f"[REMOTE] {'เริ่ม' if active else 'จบ'}เซสชันรีโมต {detail}")
    _write_state(active, detail)
    if active:
        _desktop_notification("AI Camera", f"ผู้ดูแลระบบกำลังเข้าดูหน้าจอเครื่องนี้ {detail}")
    else:
        _desktop_notification("AI Camera", "การเข้าดูหน้าจอจากผู้ดูแลระบบสิ้นสุดแล้ว")
    if callable(on_state_change):
        try:
            on_state_change(active, detail)
        except Exception as exc:
            logger.error(f"[REMOTE] แจ้งสถานะไม่สำเร็จ: {exc}")


# --------------------------------------------------------------------------------------
# frame I/O (คู่กับ socket_utils ฝั่งแอดมิน)
# --------------------------------------------------------------------------------------
def _send(sock, msg: Dict[str, Any]) -> None:
    raw = json.dumps(msg).encode("utf-8")
    sock.sendall(struct.pack(">I", len(raw)) + raw)


def _recvall(sock, n: int) -> Optional[bytes]:
    buf = bytearray()
    while len(buf) < n:
        part = sock.recv(n - len(buf))
        if not part:
            return None
        buf.extend(part)
    return bytes(buf)


def _recv(sock) -> Optional[Dict[str, Any]]:
    header = _recvall(sock, 4)
    if not header:
        return None
    body = _recvall(sock, struct.unpack(">I", header)[0])
    if body is None:
        return None
    return json.loads(body.decode("utf-8"))


def _shutdown_raw_socket(resp) -> None:
    """ง้างเข้าไปปิด socket ข้างใต้ requests/urllib3 เพื่อปลุก read() ที่ค้าง

    ไล่ตามสายที่ requests ไม่ได้เปิดเป็น public API เลยต้อง getattr ทีละชั้นและยอม
    ให้พลาดได้เงียบ ๆ ถ้า urllib3 เปลี่ยนโครงสร้างภายในวันหลัง
    """
    for path in (("raw", "_connection", "sock"), ("raw", "_fp", "fp", "raw", "_sock")):
        obj = resp
        for attr in path:
            obj = getattr(obj, attr, None)
            if obj is None:
                break
        if obj is None:
            continue
        try:
            obj.shutdown(socket.SHUT_RDWR)
            return
        except OSError:
            return
        except Exception:
            continue


# ย่อภาพก่อนส่งผ่านรีโมต — เฟรมดิบของกล้องจริงใหญ่ ~250 KB ซึ่งเกินกำลังเน็ต
# ของร้านหลายที่ (วัดจริงบนเน็ตมือถือได้ ~20-50 KB/s = 0.2 fps ดูไม่รู้เรื่อง)
# ย่อเหลือ ~30-50 KB แล้วได้เฟรมถี่ขึ้นหลายเท่า ซึ่งคนดูกล้องต้องการมากกว่าความคม
# ปิดไว้ (0) — การย่อภาพทำให้หน่วงและไม่ใช่ต้นเหตุของอาการภาพไม่ขยับ
STREAM_MAX_WIDTH = int(os.getenv("AI_CAMERA_REMOTE_STREAM_WIDTH", "0"))
STREAM_QUALITY = int(os.getenv("AI_CAMERA_REMOTE_STREAM_QUALITY", "60"))


def _shrink_jpeg(raw):
    """ย่อ JPEG ถ้าทำได้ ไม่ได้ก็คืนของเดิม (ไม่ให้พังเพราะเรื่องนี้)"""
    if STREAM_MAX_WIDTH <= 0:
        return raw
    try:
        import io

        from PIL import Image

        img = Image.open(io.BytesIO(raw))
        if img.width <= STREAM_MAX_WIDTH:
            return raw
        height = int(img.height * STREAM_MAX_WIDTH / img.width)
        img = img.convert("RGB").resize((STREAM_MAX_WIDTH, height), Image.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=STREAM_QUALITY)
        return buf.getvalue()
    except Exception:
        return raw


def _repack_part(part, boundary):
    """แกะ JPEG ออกจาก multipart part หนึ่งอัน ย่อ แล้วประกอบกลับ"""
    start = part.find(b"\xff\xd8")
    if start < 0:
        return part
    jpeg = part[start:].rstrip(b"\r\n")
    small = _shrink_jpeg(jpeg)
    if small is jpeg:
        return part
    return (b"--" + boundary + b"\r\nContent-Type: image/jpeg\r\nContent-Length: "
            + str(len(small)).encode() + b"\r\n\r\n" + small + b"\r\n")


def _multipart_boundary(headers):
    """ดึง boundary ออกจาก Content-Type ถ้าเป็นสตรีมภาพแบบ multipart"""
    ctype = next((v for k, v in headers.items() if k.lower() == "content-type"), "")
    if "multipart/x-mixed-replace" not in ctype.lower():
        return None
    for piece in ctype.split(";"):
        piece = piece.strip()
        if piece.lower().startswith("boundary="):
            return piece.split("=", 1)[1].strip('"').encode()
    return b"frame"


class RemoteAgent:
    """หนึ่งเซสชันรีโมต = หนึ่ง TCP ไปหาแอดมิน"""

    def __init__(self, host: str, port: int, token: str, client_id: str,
                 sender=None) -> None:
        self.host = host
        self.port = port
        self.token = token
        self.client_id = client_id
        # ตัวขนส่งเฟรม: ถ้าไม่ระบุ = TCP ท่อแยก (ของเดิม)
        # ถ้าระบุ = ส่งผ่าน Socket.IO เส้นเดิมที่คุยกับแอดมินอยู่แล้ว
        self._sender = sender
        self.sock: Optional[socket.socket] = None
        self.write_lock = threading.Lock()
        self.aborted: Set[str] = set()
        # response ที่ยังเปิดค้างอยู่ ต่อ req_id — ต้องเก็บไว้เพื่อ "ปิดจากข้างนอก"
        # ตอนโดน abort ดูคำอธิบายใน _abort()
        self.active: Dict[str, Any] = {}
        self.stop_flag = threading.Event()

    # -- การส่ง ------------------------------------------------------------------
    def send(self, frame: Dict[str, Any]) -> bool:
        """ส่ง frame แบบล็อก — สอง MJPEG เขียนพร้อมกันแล้ว byte จะปนกันจนท่อพัง"""
        if self.stop_flag.is_set():
            return False
        if self._sender is not None:
            try:
                self._sender(frame)
                return True
            except Exception as exc:
                logger.warning(f"[REMOTE] ส่ง frame ทาง Socket.IO ไม่สำเร็จ: {exc}")
                self.stop()
                return False
        if self.sock is None:
            return False
        try:
            with self.write_lock:
                _send(self.sock, frame)
            return True
        except OSError as exc:
            logger.warning(f"[REMOTE] ส่ง frame ไม่สำเร็จ: {exc}")
            self.stop()
            return False

    # -- วงจรชีวิต ---------------------------------------------------------------
    def run(self) -> None:
        try:
            self.sock = socket.create_connection((self.host, self.port), CONNECT_TIMEOUT)
            self.sock.settimeout(None)
            _send(self.sock, {"t": "hello", "token": self.token, "client_id": self.client_id})
        except OSError as exc:
            logger.error(f"[REMOTE] ต่อ {self.host}:{self.port} ไม่ได้: {exc}")
            return

        _notify(True, f"จาก {self.host}")
        try:
            while not self.stop_flag.is_set():
                frame = _recv(self.sock)
                if frame is None:
                    break
                self.handle_frame(frame)
        except (OSError, ValueError) as exc:
            logger.warning(f"[REMOTE] ท่อขาด: {exc}")
        finally:
            self.stop()
            _notify(False)

    def handle_frame(self, frame: Dict[str, Any]) -> None:
        """แยกออกมาเพื่อให้ใช้ได้ทั้งท่อ TCP และ Socket.IO"""
        kind = frame.get("t")
        if kind == "req":
            threading.Thread(target=self._serve, args=(frame,), daemon=True).start()
        elif kind == "abort":
            self._abort(frame.get("id"))
        elif kind == "ping":
            self.send({"t": "pong"})

    def _abort(self, req_id: Optional[str]) -> None:
        """ผู้ชมปิด iframe / เปลี่ยนกล้อง — ต้องหยุดดูดต้นทางทันที

        ตั้งแฟล็กอย่างเดียวไม่พอ: เธรด _serve ค้างอยู่ใน iter_content() ซึ่งจะไม่คืน
        ค่าจนกว่าจะสะสมครบ 8KB สตรีมบิตเรตต่ำอาจกินเวลาเป็นนาที กล้องลูกค้าก็ถูกดูด
        ต่อไปทั้งที่ไม่มีใครดู

        resp.close() อย่างเดียวยังไม่พอ — มันแค่คืน connection เข้า pool ไม่ได้ปลุก
        read() ที่ค้างอยู่ ต้อง shutdown ตัว socket ข้างใต้ให้ read เด้ง exception จริง ๆ
        """
        if not req_id:
            return
        self.aborted.add(req_id)
        resp = self.active.get(req_id)
        if resp is None:
            return
        _shutdown_raw_socket(resp)
        try:
            resp.close()
        except Exception:
            pass

    def stop(self) -> None:
        self.stop_flag.set()
        for req_id in list(self.active):
            self._abort(req_id)
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    # -- อ่านไฟล์ในเครื่องตัวเอง ---------------------------------------------------
    def _serve_file(self, req_id, path):
        """/__fs/list?path=... และ /__fs/get?path=... — ส่งไฟล์ในเครื่องสาขากลับไป"""
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(path)
        op = parsed.path[len(FS_PREFIX):].strip("/")
        wanted = (parse_qs(parsed.query).get("path") or [""])[0]

        if op == "roots":
            self._send_json(req_id, {"roots": ALLOWED_ROOTS,
                                     "media_roots": _picker_roots()})
            return

        target = _safe_path(wanted)
        if target is None:
            self._send_json(req_id, {"error": "path นี้ไม่ได้อยู่ในเขตที่อนุญาต"}, 403)
            return

        if op == "list":
            try:
                if not _fs_call(lambda: os.path.isdir(target)):
                    self._send_json(req_id, {"error": "ไม่ใช่โฟลเดอร์"}, 400)
                    return
                names = _fs_call(lambda: sorted(os.listdir(target)))
            except _FSBlocked:
                self._send_json(req_id, {
                    "error": "เครื่องสาขายังไม่ได้ให้สิทธิ์เข้าถึงโฟลเดอร์นี้ "
                             "(System Settings > Privacy & Security > "
                             "Files and Folders / Full Disk Access)"}, 403)
                return
            except OSError as e:
                self._send_json(req_id, {"error": f"เปิดโฟลเดอร์ไม่ได้: {e}"}, 400)
                return
            items = []
            for name in names:
                full = os.path.join(target, name)
                try:
                    is_dir = _fs_call(lambda f=full: os.path.isdir(f))
                    size = 0 if is_dir else _fs_call(lambda f=full: os.path.getsize(f))
                    items.append({"name": name, "path": full,
                                  "is_dir": is_dir, "size": size})
                except (OSError, _FSBlocked):
                    continue
            self._send_json(req_id, {"path": target, "items": items})
            return

        if op == "get":
            try:
                if not _fs_call(lambda: os.path.isfile(target)):
                    self._send_json(req_id, {"error": "ไม่พบไฟล์"}, 404)
                    return
                size = _fs_call(lambda: os.path.getsize(target))
            except _FSBlocked:
                self._send_json(req_id, {
                    "error": "เครื่องสาขายังไม่ได้ให้สิทธิ์เข้าถึงไฟล์นี้ "
                             "(System Settings > Privacy & Security)"}, 403)
                return
            if size > MAX_FILE_BYTES:
                self._send_json(
                    req_id,
                    {"error": f"ไฟล์ใหญ่เกิน {MAX_FILE_BYTES // (1024*1024)} MB"}, 413)
                return
            name = os.path.basename(target)
            if not self.send({
                "t": "head", "id": req_id, "status": 200,
                "headers": {"Content-Type": "application/octet-stream",
                            "Content-Disposition": f'attachment; filename="{name}"'},
            }):
                return
            seq = 0
            with open(target, "rb") as fh:
                while not self.stop_flag.is_set() and req_id not in self.aborted:
                    chunk = fh.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    if not self.send({"t": "data", "id": req_id, "seq": seq,
                                      "b64": base64.b64encode(chunk).decode("ascii")}):
                        return
                    seq += 1
            self.send({"t": "end", "id": req_id})
            logger.info(f"[REMOTE] ส่งไฟล์ {target} ({size} ไบต์) ให้แอดมิน")
            return

        self._send_json(req_id, {"error": f"ไม่รู้จักคำสั่ง {op}"}, 400)

    def _send_json(self, req_id, payload, status=200):
        raw = json.dumps(payload, ensure_ascii=False).encode()
        if self.send({"t": "head", "id": req_id, "status": status,
                      "headers": {"Content-Type": "application/json; charset=utf-8"}}):
            self.send({"t": "data", "id": req_id, "seq": 0,
                       "b64": base64.b64encode(raw).decode("ascii")})
            self.send({"t": "end", "id": req_id})


    # -- ภาพกล้อง: เอาเฟรมล่าสุดเสมอ ---------------------------------------------
    def _pump_latest_frame(self, req_id, resp, boundary) -> None:
        """ดูดสตรีมเต็มสปีด แต่ส่งออกไปเฉพาะ "เฟรมล่าสุด" ที่มีอยู่ตอนนั้น

        ของเดิมส่งทุกไบต์แล้วหน่วงให้อยู่ในเพดาน ผลคือภาพที่แอดมินเห็นเป็นภาพใน
        อดีตที่ทยอยไหลตามคิว ยิ่งดูนานยิ่งดีเลย์สะสมจนดูเหมือน "ภาพค้าง"
        (วัดได้จริง: ต้นทาง 16 fps เหลือ 0.5 fps และช้ากว่าความจริงหลายวินาที)

        คนดูกล้องต้องการภาพ "ตอนนี้" ไม่ใช่ภาพครบทุกเฟรม เลยเปลี่ยนเป็นทิ้งเฟรม
        ที่ส่งไม่ทันแล้วเอาอันใหม่แทน — ภาพจะกระตุกตามความเร็วเน็ต แต่ไม่ล้าหลัง
        """
        sep = b"--" + boundary
        latest = [None]          # ช่องเดียว เฟรมใหม่ทับเฟรมเก่าเสมอ
        lock = threading.Lock()
        finished = threading.Event()
        dropped = [0]

        def reader():
            acc = b""
            try:
                for chunk in resp.iter_content(CHUNK_SIZE):
                    if self.stop_flag.is_set() or req_id in self.aborted:
                        break
                    if not chunk:
                        continue
                    acc += chunk
                    cut = acc.rfind(sep)
                    if cut <= 0:
                        continue
                    for part in reversed(acc[:cut].split(sep)):
                        if part.strip():
                            with lock:
                                if latest[0] is not None:
                                    dropped[0] += 1
                                latest[0] = sep + part
                            break
                    acc = acc[cut:]
            except requests.RequestException:
                pass
            finally:
                finished.set()

        threading.Thread(target=reader, daemon=True).start()

        seq = 0
        while not self.stop_flag.is_set() and req_id not in self.aborted:
            with lock:
                frame, latest[0] = latest[0], None
            if frame is None:
                if finished.is_set():
                    break
                time.sleep(0.02)
                continue
            frame = _repack_part(frame, boundary)
            if not self.send({"t": "data", "id": req_id, "seq": seq,
                              "b64": base64.b64encode(frame).decode("ascii")}):
                return
            seq += 1
            # เพดานความเร็ว: ระหว่างที่รอ ตัวอ่านยังทับเฟรมใหม่ให้เรื่อย ๆ
            # พอถึงคิวส่งรอบหน้าจึงได้ภาพล่าสุดเสมอ ไม่ใช่ภาพที่ค้างในคิว
            if STREAM_MAX_BPS > 0:
                time.sleep(min(len(frame) / STREAM_MAX_BPS, 1.0))

        if req_id not in self.aborted and not self.stop_flag.is_set():
            self.send({"t": "end", "id": req_id})
        logger.info(f"[REMOTE] req {req_id} สตรีมจบ ส่ง {seq} เฟรม ทิ้ง {dropped[0]} เฟรม")

    # -- ทำงานหนึ่ง request -------------------------------------------------------
    def _serve(self, frame: Dict[str, Any]) -> None:
        req_id = frame.get("id")
        path = frame.get("path") or "/"
        method = (frame.get("method") or "GET").upper()

        # path ต้องเป็น path ล้วน ห้ามเป็น URL เต็มที่ชี้ไปที่อื่น
        if not path.startswith("/") or path.startswith("//"):
            self.send({"t": "err", "id": req_id, "msg": "path ไม่ถูกต้อง"})
            return

        if path.startswith(FS_PREFIX):
            self._serve_file(req_id, path)
            return

        target = _resolve_target(path)
        if target is None:
            self.send({"t": "err", "id": req_id, "msg": "ปลายทางไม่อยู่ในรายการที่อนุญาต"})
            return
        upstream, path = target

        headers = {
            k: v for k, v in (frame.get("headers") or {}).items()
            if k.lower() not in HOP_BY_HOP
        }
        body = base64.b64decode(frame["body_b64"]) if frame.get("body_b64") else b""

        try:
            with requests.request(
                method,
                upstream + path,
                headers=headers,
                data=body or None,
                stream=True,            # หัวใจ: ได้ header แล้วส่งกลับทันที ไม่รอ body
                timeout=UPSTREAM_TIMEOUT,
                allow_redirects=False,  # ให้เบราว์เซอร์ของแอดมินตาม redirect เอง
            ) as resp:
                out_headers = {
                    k: v for k, v in resp.headers.items()
                    if k.lower() not in HOP_BY_HOP
                }
                if not self.send({
                    "t": "head", "id": req_id,
                    "status": resp.status_code, "headers": out_headers,
                }):
                    return

                self.active[req_id] = resp

                boundary = _multipart_boundary(out_headers)
                if boundary:
                    # ภาพกล้อง: ส่ง "เฟรมล่าสุด" เสมอ ทิ้งเฟรมที่ตกค้าง
                    self._pump_latest_frame(req_id, resp, boundary)
                    return

                seq = 0
                for chunk in resp.iter_content(CHUNK_SIZE):
                    if self.stop_flag.is_set() or req_id in self.aborted:
                        logger.info(f"[REMOTE] req {req_id} ถูก abort — หยุดดูดที่ chunk {seq}")
                        break
                    if not chunk:
                        continue
                    if not self.send({
                        "t": "data", "id": req_id, "seq": seq,
                        "b64": base64.b64encode(chunk).decode("ascii"),
                    }):
                        return
                    seq += 1
                else:
                    self.send({"t": "end", "id": req_id})
        except requests.RequestException as exc:
            # โดน abort ระหว่างอ่านก็โผล่มาทางนี้ (resp.close() ทำให้ read เด้ง)
            if req_id in self.aborted:
                logger.info(f"[REMOTE] req {req_id} ถูก abort — เลิกดูดต้นทาง")
            else:
                logger.warning(f"[REMOTE] req {req_id} {method} {path} ล้มเหลว: {exc}")
                self.send({"t": "err", "id": req_id, "msg": str(exc)})
        finally:
            self.active.pop(req_id, None)
            self.aborted.discard(req_id)


# --------------------------------------------------------------------------------------
# เซิร์ฟเวอร์สถานะเล็ก ๆ ให้หน้าเว็บของเครื่องนี้ถามได้ว่ากำลังถูกรีโมตอยู่ไหม
# --------------------------------------------------------------------------------------
# ตั้งใจไม่ใส่ไว้ใน ALLOWED_PORTS เพื่อไม่ให้แอดมินมุดท่อมาที่ตัวมันเองได้
#
# ต้องอยู่นอกช่วง port_store._FALLBACK_RANGE (เริ่มที่เลขถัดจาก KNOWN_SERVICE_PORTS
# ตัวสุดท้าย แล้วไล่ขึ้นไปถึง 8200) ไม่งั้นพอมี DLC service ใหม่ที่ยังไม่ได้ลงทะเบียน
# ใน KNOWN_SERVICE_PORTS มาขอพอร์ตจาก port_store มันจะไปแจกเลขเดียวกันนี้ซ้ำ
# (เกิดขึ้นจริงกับ age_gender_service ที่ได้ 8016 ไปตอนที่ยังไม่มีใครกันเลขนี้ไว้ ทำให้
# browser ยิง /remote-status ไปโดนมันแทน แล้วโดน CORS บล็อกเพราะไม่มี route นี้)
STATUS_PORT = 8210


def start_status_server(port: int = STATUS_PORT) -> None:
    """เปิด /remote-status บน loopback ให้ app.js ของหน้าเว็บ poll เอาไปขึ้นแถบเตือน

    ต้องเป็นเซิร์ฟเวอร์แยก เพราะหน้าเว็บ (frontend_web) เป็นคนละโพรเซสและคอมไพล์
    เป็น .so ไปแล้ว เพิ่ม route เข้าไปตรง ๆ ไม่ได้
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path != "/remote-status":
                self.send_response(404)
                self.end_headers()
                return
            payload = json.dumps({"active": is_active()}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            # หน้าเว็บอยู่คนละพอร์ต (3000) เลยต้องเปิด CORS ให้ fetch ได้
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(payload)

    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        logger.error(f"[REMOTE] เปิดพอร์ตสถานะ {port} ไม่ได้: {exc}")
        return
    threading.Thread(target=server.serve_forever, name="remote-status",
                     daemon=True).start()
    logger.info(f"[REMOTE] เซิร์ฟเวอร์สถานะที่ 127.0.0.1:{port}/remote-status")


# --------------------------------------------------------------------------------------
# ตัวรับคำสั่งจาก Socket.IO
# --------------------------------------------------------------------------------------
def start_socketio_session(emit_fn, token: str, client_id: str) -> None:
    """เปิดเซสชันรีโมตบน Socket.IO เส้นเดิม (ไม่ต้องเปิดพอร์ต/ท่อที่สอง)

    ใช้โปรโตคอลเฟรมชุดเดียวกับท่อ TCP ทุกประการ เปลี่ยนแค่ตัวขนส่ง — เส้นนี้
    วิ่งผ่าน https + nginx ที่มีอยู่แล้ว จึงไม่ต้องเปิดพอร์ตที่สองที่ไฟร์วอลล์
    """
    close_remote()

    agent = RemoteAgent("", 0, token, client_id, sender=emit_fn)
    with _agent_lock:
        global _agent
        _agent = agent

    # เฟรมแรกยังเป็น hello เหมือนเดิม แอดมินใช้ token ตรวจว่าเป็นเซสชันที่มันเปิดจริง
    agent.send({"t": "hello", "token": token, "client_id": client_id})
    _notify(True, "ผ่าน Socket.IO")


def on_remote_frame(frame: Any) -> None:
    """อีเวนต์ remote_frame จากแอดมิน — ส่งต่อให้เอเจนต์ที่เปิดอยู่"""
    if not isinstance(frame, dict):
        return
    with _agent_lock:
        agent = _agent
    if agent is None:
        return
    agent.handle_frame(frame)


def on_open_remote(data: Any, client_id: str = "") -> None:
    """handler ของอีเวนต์ "open_remote" — ผูกไว้ใน client_logic.setup_clients()"""
    if not isinstance(data, dict):
        return
    token = data.get("token")
    host = data.get("host")
    port = data.get("port")

    # โหมดใหม่: ใช้ Socket.IO เส้นเดิมเป็นท่อ ไม่ต้องหมุน TCP ออกไปอีกเส้น
    if data.get("transport") == "socketio":
        emit_fn = data.get("_emit")
        if not token or emit_fn is None:
            logger.warning("[REMOTE] open_remote (socketio) ข้อมูลไม่ครบ — ข้าม")
            return
        start_socketio_session(emit_fn, str(token), client_id)
        return

    if not token or not host or not port:
        logger.warning("[REMOTE] open_remote ข้อมูลไม่ครบ — ข้าม")
        return

    close_remote()   # อนุญาตทีละเซสชัน เปิดใหม่ทับของเก่าเสมอ

    agent = RemoteAgent(str(host), int(port), str(token), client_id)
    with _agent_lock:
        global _agent
        _agent = agent
    threading.Thread(target=agent.run, name="remote-agent", daemon=True).start()


def close_remote(data: Any = None) -> None:
    """ตัดสายรีโมต (แอดมินสั่ง หรือผู้ใช้กดจาก menubar)"""
    with _agent_lock:
        global _agent
        agent, _agent = _agent, None
    if agent:
        agent.stop()


def is_active() -> bool:
    with _agent_lock:
        return _agent is not None and not _agent.stop_flag.is_set()
