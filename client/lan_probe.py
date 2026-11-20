"""LAN check: tells the portal whether the browser setting up a device is on this shop's network.

    branch --branch_net (Socket.IO, hourly)--> admin keeps {lan_ip, port}
    portal asks admin for a check -> admin --lan_ticket--> branch keeps a one-use ticket (60 s)
    browser --POST http://<lan_ip>:<port>/lan-check {store_code, ticket}--> this server
            -> 200 only when the ticket is one we were given AND the store code is ours

Only a browser that can reach this machine's LAN address gets an answer, so "ok" means
"same network". Secure by design:
  * one path, one method; no data comes back except ok / rejected
  * a ticket is random, single use, lives 60 s and only works from the origin the admin named
  * the store code must match this shop's (store.id), else the request is rejected
  * CORS + Private Network Access answer only that origin; bodies are capped at 1 KB
  * rate limited; nothing is logged (no IPs or codes in the shop-readable log)
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import os
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional
from urllib.parse import urlsplit

PORT = int(os.getenv("AICAM_LAN_PROBE_PORT", "8768"))
TICKET_TTL = 60
MAX_TICKETS = 20
RATE_PER_MIN = 30
REPORT_EVERY = 3600          # seconds between branch_net reports to the admin
_TICKET_RE = re.compile(r"^[A-Za-z0-9_-]{32,64}$")
_ORIGIN_RE = re.compile(r"^https?://[A-Za-z0-9.\-]+(:\d{1,5})?$")

_lock = threading.Lock()
_tickets: dict[str, tuple[float, str]] = {}      # ticket -> (expires, origin)
_hits: list[float] = []
_started = threading.Event()
_wake = threading.Event()
_store_code: Callable[[], str] = lambda: ""


def _now() -> float:
    return time.monotonic()


def _prune() -> None:
    now = _now()
    for t in [t for t, (exp, _) in _tickets.items() if exp < now]:
        _tickets.pop(t, None)


def add_ticket(data) -> dict:
    """Socket.IO 'lan_ticket' from the admin: remember one ticket for one origin."""
    d = data if isinstance(data, dict) else {}
    ticket, origin = str(d.get("ticket") or ""), str(d.get("origin") or "")
    if not _TICKET_RE.match(ticket) or not _ORIGIN_RE.match(origin):
        return {"ok": False, "error": "bad_request"}
    if not _started.is_set():
        return {"ok": False, "error": "probe_down"}
    with _lock:
        _prune()
        while len(_tickets) >= MAX_TICKETS:
            _tickets.pop(next(iter(_tickets)))
        _tickets[ticket] = (_now() + TICKET_TTL, origin)
    return {"ok": True}


def _origins() -> set[str]:
    with _lock:
        _prune()
        return {o for _, o in _tickets.values()}


def _take(ticket: str, origin: str) -> bool:
    """Single use: a matching ticket is removed whether or not the rest of the check passes."""
    with _lock:
        _prune()
        for t in list(_tickets):
            if hmac.compare_digest(t.encode(), ticket.encode()):
                _, want = _tickets.pop(t)
                return hmac.compare_digest(want.encode(), origin.encode())
    return False


def _rate_ok() -> bool:
    now = _now()
    with _lock:
        _hits[:] = [h for h in _hits if now - h < 60]
        if len(_hits) >= RATE_PER_MIN:
            return False
        _hits.append(now)
        return True


class _Handler(BaseHTTPRequestHandler):
    server_version = "lan-probe"
    sys_version = ""

    def log_message(self, *args):  # no IPs, paths or codes in any log
        pass

    def _send(self, code: int, body: Optional[dict], origin: str = "", preflight: bool = False) -> None:
        raw = json.dumps(body).encode() if body is not None else b""
        self.send_response(code)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        if preflight:
            self.send_header("Access-Control-Allow-Methods", "POST")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Access-Control-Max-Age", "60")
        if raw:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if raw:
            self.wfile.write(raw)

    def do_OPTIONS(self):
        origin = self.headers.get("Origin", "")
        if self.path != "/lan-check" or not _rate_ok() or origin not in _origins():
            return self._send(403, None)
        self._send(204, None, origin, preflight=True)

    def do_POST(self):
        origin = self.headers.get("Origin", "")
        if self.path != "/lan-check" or not _rate_ok():
            return self._send(403, {"ok": False, "error": "rejected"})
        allow = origin if origin in _origins() else ""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if not 0 < n <= 1024:
            return self._send(400, {"ok": False, "error": "bad_request"}, allow)
        try:
            body = json.loads(self.rfile.read(n))
        except ValueError:
            return self._send(400, {"ok": False, "error": "bad_request"}, allow)
        if not isinstance(body, dict):
            return self._send(400, {"ok": False, "error": "bad_request"}, allow)
        ticket, code = str(body.get("ticket") or ""), str(body.get("store_code") or "")
        if not _TICKET_RE.match(ticket) or not _take(ticket, origin):
            return self._send(403, {"ok": False, "error": "rejected"}, allow)
        mine = _store_code() or ""
        if not mine or not code or not hmac.compare_digest(mine.encode(), code.encode()):
            return self._send(403, {"ok": False, "error": "store_mismatch"}, allow)
        self._send(200, {"ok": True}, allow)

    def do_GET(self):
        self._send(404, None)


def lan_ip(admin_url: str = "") -> str:
    """This machine's address on the shop LAN: the interface that routes towards the admin.
    UDP connect sends nothing; it only picks the route."""
    host = urlsplit(admin_url).hostname if admin_url else None
    for target in [h for h in (host, "192.0.2.1") if h]:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((target, 9))
                ip = s.getsockname()[0]
            if ipaddress.ip_address(ip).is_private and not ipaddress.ip_address(ip).is_loopback:
                return ip
        except (OSError, ValueError):
            continue
    return ""


def net_info(admin_url: str = "") -> dict:
    ip = lan_ip(admin_url)
    return {"lan_ip": ip, "port": PORT} if ip and _started.is_set() else {}


def start(store_code: Callable[[], str]) -> bool:
    """Serve /lan-check on every interface (it must be reachable from the LAN). Once."""
    global _store_code
    _store_code = store_code
    if _started.is_set():
        return True
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", PORT), _Handler)
    except OSError:
        return False
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name="lan-probe", daemon=True).start()
    _started.set()
    return True


def wake() -> None:
    """Report now (called once the admin has accepted the identify)."""
    _wake.set()


def start_reporter(emit: Callable[[dict], None], connected: Callable[[], bool], admin_url: str) -> None:
    """Every REPORT_EVERY seconds (and on wake()), tell the admin our LAN address."""
    def loop():
        while True:
            _wake.wait(REPORT_EVERY)
            _wake.clear()
            try:
                info = net_info(admin_url)
                if info and connected():
                    emit(info)
            except Exception:
                pass
    threading.Thread(target=loop, name="lan-probe-report", daemon=True).start()
