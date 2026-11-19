"""Alitech portal (lab copy): AI Camera > Security — CCTV Streaming, Alert & Log, Hardware.

    browser --(session cookie + CSRF)--> this server --(service key)--> AI Camera admin
    browser <--WebRTC (WHEP + one-session token)-- MediaMTX  (the admin checks every read)

Secure by design / PDPA:
  * Only an Alitech back-office admin (role=admin) sees the page or its API; the role
    is read from the account store on every request, so a change applies at once.
  * The admin service key never reaches a browser: the browser talks only to this
    server and gets back one stream URL + token per camera, valid for that session.
  * Session cookie: signed, HttpOnly, SameSite=Strict, 8 h; new on every sign-in.
    Every state-changing call needs the session's CSRF token and a same-origin Origin.
  * Sign-in: scrypt, same timing for unknown e-mails, 5 failures / 15 min per IP.
  * Every camera start/stop is written to data/audit.jsonl by user id (no e-mail).
  * Strict CSP: scripts and styles from this origin only; connect only to this origin
    and the media server; no framing.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app import demo_alerts, users

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE.parent / "data"
AUDIT_FILE = DATA_DIR / "audit.jsonl"

SESSION_SECRET = os.environ.get("ALITECH_LIVE_SESSION_SECRET", "")
SERVICE_KEY = os.environ.get("AICAM_LIVE_SERVICE_KEY", "")
ADMIN_URL = os.environ.get("AICAM_ADMIN_URL", "http://127.0.0.1:18002").rstrip("/")
WHEP_BASE = os.environ.get("AICAM_WHEP_BASE", "http://127.0.0.1:8889").rstrip("/")
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "false").lower() == "true"
if len(SESSION_SECRET) < 32 or len(SERVICE_KEY) < 32:
    raise SystemExit("ALITECH_LIVE_SESSION_SECRET / AICAM_LIVE_SERVICE_KEY missing (run media/run_media.sh first)")

_media = urlsplit(WHEP_BASE)
MEDIA_ORIGIN = f"{_media.scheme}://{_media.netloc}"
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; img-src 'self' data:; media-src 'self' blob:; "
       f"connect-src 'self' {MEDIA_ORIGIN}; frame-ancestors 'none'; base-uri 'none'; "
       "form-action 'self'; object-src 'none'")

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, session_cookie="alitech_live",
                   max_age=8 * 3600, same_site="strict", https_only=COOKIE_SECURE)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
# changes on every start: script/style links carry it, so browsers fetch the new files
templates.env.globals["asset_v"] = str(int(time.time()))

_CLIENT_RE = re.compile(r"^[A-Za-z0-9-]{4,40}$")
_SID_RE = re.compile(r"^[0-9a-f]{32}$")
_audit_lock = threading.Lock()
_fails: dict[str, list[float]] = {}
_fails_lock = threading.Lock()
MAX_FAILS, FAIL_WINDOW = 5, 15 * 60
MAX_PLAYBACK_SPAN = timedelta(hours=24)
SECURITY_PAGES = {"cctv": "cctv.html", "alerts": "alerts.html", "hardware": "hardware.html"}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["Content-Security-Policy"] = CSP
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    resp.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    if not request.url.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ---- session helpers ----------------------------------------------------------------------

def _csrf(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = request.session["csrf"] = secrets.token_urlsafe(32)
    return token


def _current_user(request: Request):
    uid = request.session.get("uid")
    return users.get(uid) if isinstance(uid, int) else None


def _require_admin_api(request: Request, unsafe: bool = False) -> dict:
    user = _current_user(request)
    if not user:
        raise HTTPException(401)
    if user["role"] != "admin":
        raise HTTPException(403)
    if unsafe:
        origin = request.headers.get("origin")
        if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
            raise HTTPException(403)
        if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), request.session.get("csrf", "") or "-"):
            raise HTTPException(403)
    return user


def _audit(user_id: int, action: str, **extra) -> None:
    entry = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"), "user_id": user_id, "action": action}
    entry.update({k: v for k, v in extra.items() if v is not None})
    with _audit_lock:
        DATA_DIR.mkdir(mode=0o700, exist_ok=True)
        fd = os.open(AUDIT_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (json.dumps(entry, ensure_ascii=False) + "\n").encode())
        finally:
            os.close(fd)


def _too_many_fails(ip: str) -> bool:
    now = time.time()
    with _fails_lock:
        recent = [t for t in _fails.get(ip, []) if now - t < FAIL_WINDOW]
        _fails[ip] = recent
        return len(recent) >= MAX_FAILS


def _note_fail(ip: str) -> None:
    with _fails_lock:
        _fails.setdefault(ip, []).append(time.time())


# ---- pages --------------------------------------------------------------------------------

HOME = "/my-products/ai-camera/security/cctv"


@app.get("/")
async def home(request: Request):
    return RedirectResponse(HOME if _current_user(request) else "/login", 303)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    if _current_user(request):
        return RedirectResponse(HOME, 303)
    return templates.TemplateResponse(request, "login.html", {"csrf_token": _csrf(request),
                                                              "error": error if error in ("bad", "rate", "session") else ""})


@app.post("/login")
async def login(request: Request, email: str = Form(""), password: str = Form(""), csrf_field: str = Form("", alias="_csrf")):
    ip = request.client.host if request.client else "-"
    if not secrets.compare_digest(csrf_field or "-", request.session.get("csrf", "") or "+"):
        return RedirectResponse("/login?error=session", 303)
    if _too_many_fails(ip):
        return RedirectResponse("/login?error=rate", 303)
    user = users.authenticate(email, password)
    if not user:
        _note_fail(ip)
        return RedirectResponse("/login?error=bad", 303)
    request.session.clear()                       # new session on every sign-in
    request.session.update({"uid": user["id"], "csrf": secrets.token_urlsafe(32), "at": int(time.time())})
    _audit(user["id"], "sign_in")
    return RedirectResponse(HOME, 303)


@app.post("/logout")
async def logout(request: Request, csrf_field: str = Form("", alias="_csrf")):
    user = _current_user(request)
    if user and secrets.compare_digest(csrf_field or "-", request.session.get("csrf", "") or "+"):
        _audit(user["id"], "sign_out")
        request.session.clear()
    return RedirectResponse("/login", 303)


@app.get("/my-products/ai-camera/cameras")
async def old_cameras_page():
    return RedirectResponse(HOME, 301)


@app.get("/my-products/ai-camera/security/{page}", response_class=HTMLResponse)
async def security_page(request: Request, page: str):
    if page not in SECURITY_PAGES:
        raise HTTPException(404)
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", 303)
    ctx = {"user": user, "csrf_token": _csrf(request), "active": page}
    if user["role"] != "admin":
        return templates.TemplateResponse(request, "no_permission.html", ctx, status_code=403)
    return templates.TemplateResponse(request, SECURITY_PAGES[page], ctx)


@app.get("/my-products/ai-camera/security/hardware/tuya-siren", response_class=HTMLResponse)
async def siren_page(request: Request):
    """Hardware Integration > Tuya > the siren's 5-step setup, inside the portal."""
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", 303)
    ctx = {"user": user, "csrf_token": _csrf(request), "active": "hardware"}
    if user["role"] != "admin":
        return templates.TemplateResponse(request, "no_permission.html", ctx, status_code=403)
    return templates.TemplateResponse(request, "siren.html", ctx)


# The branch's siren setup, reached through the AI Camera admin (keys stay on the branch).
SIREN_GET = {"state", "live", "qr/status"}
SIREN_POST = {"qr", "scan", "ring", "check", "settings"}


@app.api_route("/api/siren/{client_id}/{path:path}", methods=["GET", "POST"])
async def api_siren(request: Request, client_id: str, path: str):
    unsafe = request.method == "POST"
    user = _require_admin_api(request, unsafe=unsafe)
    client_id = _client(client_id)
    if demo_alerts.is_demo_branch(client_id):
        return JSONResponse({"ok": False, "error": "demo_branch"}, status_code=409)
    if path not in (SIREN_POST if unsafe else SIREN_GET):
        raise HTTPException(404)
    body = await _json_body(request) if unsafe else {}
    if unsafe:
        _audit(user["id"], f"siren_{path}", branch=client_id)
    status, res = await _admin("POST", f"/{client_id}/siren", json={
        "method": request.method, "path": f"/api/{path}", "body": body, "viewer": f"alitech:{user['id']}"})
    if status != 200 or not res.get("ok"):
        return JSONResponse({"ok": False, "why": res.get("error") or f"admin_{status}"}, status_code=200)
    return JSONResponse(res.get("body") or {}, status_code=200)


# ---- camera API (browser -> here -> AI Camera admin) --------------------------------------

async def _admin(method: str, path: str, **kw):
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            r = await client.request(method, f"{ADMIN_URL}/ai-camera/api/live{path}",
                                     headers={"X-Live-Service-Key": SERVICE_KEY}, **kw)
    except httpx.HTTPError:
        raise HTTPException(502, "admin_unreachable")
    try:
        body = r.json()
    except ValueError:
        body = {"ok": False, "error": f"admin_{r.status_code}"}
    return r.status_code, body


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "bad_json")
    if not isinstance(body, dict):
        raise HTTPException(400, "bad_json")
    return body


def _client(client_id: str) -> str:
    if not _CLIENT_RE.match(client_id or ""):
        raise HTTPException(400)
    return client_id


@app.get("/api/live/branches")
async def api_branches(request: Request):
    """Online branches from the admin, plus DEMO branches (list only, no video)."""
    _require_admin_api(request)
    status, body = await _admin("GET", "/branches")
    if status == 200 and body.get("ok"):
        body["branches"] = (body.get("branches") or []) + demo_alerts.demo_branches()
    return JSONResponse(body, status_code=status)


@app.get("/api/live/{client_id}/cameras")
async def api_cameras(request: Request, client_id: str):
    _require_admin_api(request)
    if demo_alerts.is_demo_branch(client_id):
        return {"ok": True, "demo": True, "cameras": demo_alerts.demo_cameras(client_id), "max_streams": 16, "max_main": 2}
    status, body = await _admin("GET", f"/{_client(client_id)}/cameras")
    return JSONResponse(body, status_code=status)


@app.post("/api/live/{client_id}/start")
async def api_start(request: Request, client_id: str):
    user = _require_admin_api(request, unsafe=True)
    client_id = _client(client_id)
    if demo_alerts.is_demo_branch(client_id):
        return JSONResponse({"ok": False, "error": "demo_branch"}, status_code=409)
    body = await _json_body(request)
    mode = body.get("mode")
    payload: dict = {"mode": mode, "viewer": f"alitech:{user['id']}"}
    try:
        items = [{"camera": int(i["camera"]), "channel": int(i["channel"]), "subtype": 1 if int(i.get("subtype", 1)) else 0}
                 for i in (body.get("items") or [])][:16]
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400)
    if not items or mode not in ("live", "playback"):
        raise HTTPException(400)
    if mode == "playback":
        try:
            start = datetime.strptime(str(body.get("start")), "%Y-%m-%dT%H:%M:%S")
            end = datetime.strptime(str(body.get("end")), "%Y-%m-%dT%H:%M:%S")
        except (TypeError, ValueError):
            raise HTTPException(400)
        end = min(end, datetime.now())
        if end <= start or end - start > MAX_PLAYBACK_SPAN:
            raise HTTPException(400, "bad_time")
        quality = body.get("quality", "full")
        if quality not in ("full", "grid", "still"):
            raise HTTPException(400)
        try:
            speed = int(body.get("speed", 1))
        except (TypeError, ValueError):
            raise HTTPException(400)
        direction = body.get("direction", "fwd")
        if speed not in (1, 2, 5, 10) or direction not in ("fwd", "rev"):
            raise HTTPException(400)
        payload.update(start=start.strftime("%Y-%m-%dT%H:%M:%S"), end=end.strftime("%Y-%m-%dT%H:%M:%S"),
                       speed=speed, direction=direction, quality=quality)
    payload["items"] = items
    status, result = await _admin("POST", f"/{client_id}/start", json=payload)
    _audit(user["id"], f"{mode}_start", branch=client_id,
           cameras=[f"cam{i['camera']}/ch{i['channel']}" for i in items],
           span=f"{payload.get('start')}..{payload.get('end')}" if mode == "playback" else None,
           speed=f"{'-' if payload.get('direction') == 'rev' else ''}{payload.get('speed')}x" if mode == "playback" else None,
           result="ok" if result.get("ok") else result.get("error"))
    return JSONResponse(result, status_code=status)


@app.get("/api/live/{client_id}/record-days")
async def api_record_days(request: Request, client_id: str, camera: int, channel: int, month: str):
    _require_admin_api(request)
    if not re.match(r"^\d{4}-\d{2}$", month):
        raise HTTPException(400)
    if demo_alerts.is_demo_branch(client_id):
        return JSONResponse({"ok": False, "error": "demo_branch"}, status_code=409)
    status, body = await _admin("GET", f"/{_client(client_id)}/record-days",
                                params={"camera": camera, "channel": channel, "month": month})
    return JSONResponse(body, status_code=status)


@app.get("/api/live/{client_id}/record-spans")
async def api_record_spans(request: Request, client_id: str, camera: int, channel: int, day: str):
    _require_admin_api(request)
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
        raise HTTPException(400)
    if demo_alerts.is_demo_branch(client_id):
        return JSONResponse({"ok": False, "error": "demo_branch"}, status_code=409)
    status, body = await _admin("GET", f"/{_client(client_id)}/record-spans",
                                params={"camera": camera, "channel": channel, "day": day})
    return JSONResponse(body, status_code=status)


@app.post("/api/live/stop")
async def api_stop(request: Request):
    user = _require_admin_api(request, unsafe=True)
    body = await _json_body(request)
    sessions = [s for s in (body.get("sessions") or []) if isinstance(s, str) and _SID_RE.match(s)][:16]
    await asyncio.gather(*(_admin("POST", "/stop", json={"session": sid, "viewer": f"alitech:{user['id']}"})
                           for sid in sessions), return_exceptions=True)
    if sessions:
        _audit(user["id"], "stop", sessions=[s[:8] for s in sessions])
    return {"ok": True}


# ---- Alert & Log (DEMO rows until the admin stores real alerts) ---------------------------

@app.get("/api/alerts")
async def api_alerts(request: Request, kind: str, regions: str = "", branches: str = "", q: str = "",
                     offset: int = 0, limit: int = 20):
    """One page of alerts (newest first) for the table's infinite scroll."""
    _require_admin_api(request)
    if kind not in ("intrusion", "camera") or offset < 0 or not (1 <= limit <= 50) or len(q) > 100:
        raise HTTPException(400)
    picked = [r for r in regions.split(",") if r][:10]
    codes = [c for c in branches.split(",") if re.match(r"^[A-Z0-9]{2,10}$", c)][:200]
    page = demo_alerts.query(kind, picked, codes, q.strip().lower(), offset, limit)
    if any(r.get("client_id") == "__REAL__" for r in page["rows"]):
        # the test branch's rows open the first online branch (the lab Mac mini)
        status, body = await _admin("GET", "/branches")
        real = next((b["client_id"] for b in (body.get("branches") or []) if b.get("live")), None) if status == 200 else None
        page["rows"] = [{**r, "client_id": real} if r.get("client_id") == "__REAL__" else r for r in page["rows"]]
    return {"ok": True, "demo": True, **page,
            "regions": sorted({b["region"] for b in demo_alerts.ALL_BRANCHES}),
            "branches": [{"code": b["code"], "region": b["region"], "province": b["province"], "place": b["place"]}
                         for b in demo_alerts.ALL_BRANCHES]}


@app.get("/api/alerts/item/{row_id}")
async def api_alert_item(request: Request, row_id: str):
    """One alert, for the CCTV page opened from it (its banner) and the way back to it."""
    _require_admin_api(request)
    if not re.match(r"^[a-z]{2}\d{3}$", row_id):
        raise HTTPException(400)
    row = demo_alerts.find(row_id)
    if not row:
        raise HTTPException(404)
    if row.get("client_id") == "__REAL__":
        status, body = await _admin("GET", "/branches")
        real = next((b["client_id"] for b in (body.get("branches") or []) if b.get("live")), None) if status == 200 else None
        row = {**row, "client_id": real}
    return {"ok": True, "demo": True, "row": row}


@app.get("/api/alerts/image/{image_id}")
async def api_alert_image(request: Request, image_id: str):
    user = _require_admin_api(request)
    if not re.match(r"^[a-z]{2}\d{3}-(0|before|now)$", image_id):
        raise HTTPException(400)
    path = demo_alerts.image_file(image_id)
    if path is not None:                      # a real frame from the lab NVR
        _audit(user["id"], "alert_image_view", image=image_id, real=True)
        return Response(path.read_bytes(), media_type="image/jpeg", headers={"Cache-Control": "no-store"})
    svg = demo_alerts.image_svg(image_id)
    if svg is None:
        raise HTTPException(404)
    _audit(user["id"], "alert_image_view", image=image_id)
    return Response(svg, media_type="image/svg+xml",
                    headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)
