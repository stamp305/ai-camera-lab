"""
AI Camera Frontend Server (Pure Python)
=======================================
Replaces the Next.js frontend with FastAPI + Jinja2 + Vanilla JS.
Serves all pages from HTML templates with static CSS/JS assets.
"""
import os
import re
import time
import base64
import hashlib
import hmac
import secrets as _pysecrets
import logging
import json as _json
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
import httpx
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import sys
logger = logging.getLogger(__name__)

# Resolve paths relative to this file
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")

# Add parent dir to sys.path so we can import data_service
sys.path.append(os.path.dirname(BASE_DIR))
try:
    from data_service.configs.secure_env import load_secure_env
    # Same overrides client_logic.load_secure_env() keeps: the compiled loader
    # assigns os.environ outright, and would aim a test run back at the
    # baked-in admin. Unset in the field.
    _overrides = {k: os.environ[k] for k in ('API_SERVER_HOST', 'API_SERVER_PORT', 'SOCKET_SERVER_HOST',
                                             'SOCKET_SERVER_PATH', 'SOCKET_SERVER_PORT') if os.environ.get(k)}
    load_secure_env()
    os.environ.update(_overrides)
except ImportError:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

sys.path.insert(0, BASE_DIR)

API_BASE_URL = os.getenv("NEXT_PUBLIC_API_BASE_URL", "")

# --- App Setup ---
# Swagger/ReDoc/OpenAPI disabled — this server is reachable from the store LAN,
# there is no reason to publish an interactive API map to anyone who can open a browser.
app = FastAPI(
    title="AI Camera Frontend Server",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# --- Floorplan image proxy (must be BEFORE the /static mount: the frontend's
# own static mount matches first and would 404 data_service-served images) ---
@app.api_route("/static/images/floorplans/{filename:path}", methods=["GET", "HEAD", "OPTIONS"])
async def proxy_floorplan_image(request: Request, filename: str):
    return await _forward_to_data_service(request, f"static/images/floorplans/{filename}")


_HOP_BY_HOP_HEADERS = {"host", "content-length", "connection", "keep-alive", "transfer-encoding", "upgrade"}


def _data_service_base_url() -> str:
    return f"http://127.0.0.1:{port_store.get_port('data_service', 8000)}"


async def _forward_to_data_service(request: Request, path: str) -> Response:
    target = f"{_data_service_base_url()}/{path}"
    if request.query_params:
        target += "?" + str(request.query_params)
    forward_headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP_HEADERS}
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        try:
            upstream = await client.request(request.method, target, headers=forward_headers)
        except httpx.HTTPError as exc:
            return Response(status_code=502, content=f"proxy error: {exc}".encode())
    resp_headers = {k: v for k, v in upstream.headers.items()
                    if k.lower() not in ("content-length", "content-encoding", "transfer-encoding")}
    return Response(content=upstream.content, status_code=upstream.status_code,
                    headers=resp_headers, media_type=upstream.headers.get("content-type"))

import assets_vault

# An update from the admin brings the pages as the plain templates/ and static/
# folders: the build farm has no DLC key, so it cannot seal them. The app used to
# delete those folders below and keep serving the assets.bin it was installed
# with, so no page change made after an install ever reached an .app branch.
# Seal the folders that arrived into a fresh assets.bin first, with this
# install's own key, then let them be removed as before.
if ".app/Contents/Resources/" in BASE_DIR + os.sep and os.path.isdir(TEMPLATES_DIR):
    try:
        _sealing = assets_vault.BUNDLE + ".new"
        assets_vault.pack(BASE_DIR, _sealing, assets_vault.key())
        with open(_sealing, "rb") as _f:
            _pages = sum(1 for n in assets_vault.unpack(_f.read(), assets_vault.key()) if n.startswith("templates/"))
        if _pages:
            os.replace(_sealing, assets_vault.BUNDLE)
            logger.info(f"[UI] Sealed the {_pages} updated pages into assets.bin")
        else:
            os.remove(_sealing)
    except Exception as e:
        logger.error(f"[UI] Could not seal the updated pages ({type(e).__name__}); keeping the installed ones")

if assets_vault.available():
    # The .app ships pages and static files sealed in assets.bin, read from
    # memory (assets_vault.py). Opening it fails if any byte was changed —
    # then the UI does not start rather than serve an edited page.
    import mimetypes
    import jinja2

    @app.api_route("/static/{path:path}", methods=["GET", "HEAD"])
    async def packed_static(request: Request, path: str):
        data = assets_vault.read("static/" + path)
        if data is None:
            return Response(status_code=404)
        tag = assets_vault.etag(data)
        if request.headers.get("if-none-match") == tag:
            return Response(status_code=304, headers={"ETag": tag})
        media_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
        return Response(content=data, media_type=media_type, headers={"ETag": tag})

    templates = Jinja2Templates(env=jinja2.Environment(
        loader=jinja2.DictLoader(assets_vault.templates()), autoescape=True))
    # An .app updated from a version that shipped the plain folders still has
    # them. Only inside an .app: in a working copy those folders are the source.
    if ".app/Contents/Resources/" in BASE_DIR + os.sep:
        import shutil
        for _plain in (TEMPLATES_DIR, STATIC_DIR):
            shutil.rmtree(_plain, ignore_errors=True)
else:
    # Mount static files (CSS, JS, images)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    # Setup Jinja2 templates
    templates = Jinja2Templates(directory=TEMPLATES_DIR)
templates.env.globals['api_base_url'] = API_BASE_URL


# ============================================================
# Server-side session (signed HttpOnly cookie)
# ============================================================
# The UI used to gate pages with sessionStorage checks in JS only —
# anyone with curl (or F12 disabled) reached every page and the whole
# proxied data_service API. Sessions are now verified on the server:
# an HMAC-signed expiry token in an HttpOnly cookie, so the browser
# cannot read or forge it and it expires on its own.
SESSION_COOKIE_NAME = "aic_session"
SESSION_TTL_SECONDS = 24 * 60 * 60  # keep in sync with SESSION_DURATION_MS in static/js/app.js

_APP_SUPPORT_DIR = os.path.expanduser("~/Library/Application Support/AICamera")
_SESSION_KEY_FILE = os.getenv(
    "FRONTEND_SESSION_KEY_FILE",
    os.path.join(_APP_SUPPORT_DIR, ".frontend_session_key"),
)


def _load_session_secret() -> bytes:
    """Random ephemeral secret so restarting the program invalidates all sessions."""
    return _pysecrets.token_hex(32).encode()


_SESSION_SECRET = _load_session_secret()


def _session_epoch() -> str:
    """The running payload version, mixed into every session signature.

    The secret is deliberately persisted so an ordinary restart keeps people
    signed in. An update is not an ordinary restart: the app that comes back is
    different code, yet the cookie from before it stayed valid — so after an
    admin-pushed update the app opened a login page while any tab still open on
    the old screen went straight back in on a refresh, walking past that login.
    Binding the signature to the version means every cookie issued by the old
    version stops verifying the moment a new one is running, and both routes
    ask for a login again. Restarts on the same version are unaffected.
    """
    from data_service.configs.version_store import read_client_version
    # Unreadable version: a constant ("") rather than a random value, so a
    # transient read error cannot sign everyone out.
    return read_client_version(os.path.dirname(BASE_DIR), default="")


_SESSION_EPOCH = _session_epoch().encode()


def _sign(payload: bytes) -> bytes:
    return hmac.new(_SESSION_SECRET + b"|" + _SESSION_EPOCH, payload, hashlib.sha256).digest()


def create_session_token() -> str:
    exp = int(time.time()) + SESSION_TTL_SECONDS
    payload = str(exp).encode()
    return (
        base64.urlsafe_b64encode(payload).rstrip(b"=").decode()
        + "."
        + base64.urlsafe_b64encode(_sign(payload)).rstrip(b"=").decode()
    )


def verify_session_token(token: str) -> bool:
    try:
        payload_b64, sig_b64 = token.split(".")
        payload = base64.urlsafe_b64decode(payload_b64 + "==")
        sig = base64.urlsafe_b64decode(sig_b64 + "==")
        if not hmac.compare_digest(_sign(payload), sig):
            return False
        return int(payload.decode()) > time.time()
    except Exception:
        return False


def has_valid_session(request: Request) -> bool:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    return bool(token and verify_session_token(token))


# Paths reachable without a login session.
# - /login and /connection are pre-auth pages by design.
# - users/login is what establishes the session.
# - users/activated-status is a boolean probe used by /connection before login
#   (returns is_activated/credentials_changed flags only — no credential material).
# - updates/in-progress is polled from every page, including /login: an update
#   can land while nobody is signed in, and the notice has to come up anyway.
#   It answers a single boolean about this machine's own updater.
_PUBLIC_EXACT_PATHS = {"/login", "/connection", "/frontend-health", "/favicon.ico",
                       "/api/updates/in-progress", "/api/session"}
_PUBLIC_PREFIXES = ("/static/",)
_PUBLIC_PROXY_PATHS = {
    "/api/proxy/users/login",
    "/api/proxy/users/activated-status",
}


def _is_public_path(path: str) -> bool:
    if path in _PUBLIC_EXACT_PATHS or path in _PUBLIC_PROXY_PATHS:
        return True
    return any(path.startswith(p) for p in _PUBLIC_PREFIXES)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path

    # Login passes through; on upstream success the session cookie is set here.
    if path == "/api/proxy/users/login" and request.method == "POST":
        response = await call_next(request)
        if response.status_code == 200:
            response.set_cookie(
                SESSION_COOKIE_NAME,
                create_session_token(),
                max_age=SESSION_TTL_SECONDS,
                httponly=True,
                samesite="strict",
                path="/",
            )
        return response

    if _is_public_path(path) or has_valid_session(request):
        return await call_next(request)

    if path.startswith("/api/") or request.method != "GET":
        # The header tells the page this is the session ending (app.js then goes
        # to /login), not a 401 from data_service such as a wrong password.
        return JSONResponse(status_code=401, content={"detail": "Not authenticated"},
                            headers={"X-Session-Expired": "1"})
    return RedirectResponse(url="/login", status_code=303)


# ============================================================
# ROUTES: No-Navbar Pages (Login, Activation, Setup, Connection)
# ============================================================

import port_manager
from fastapi import Response
import httpx
from starlette.background import BackgroundTask
from fastapi.responses import StreamingResponse

# The camera lists camera_service reads carry each recorder's password in their RTSP
# addresses. No page needs them; a signed-in browser must not be able to read them either.
_PROXY_REFUSED = {"data/active-cameras-config", "data/inside-cameras-config"}


@app.api_route("/api/proxy/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_api(path: str, request: Request):
    """Proxy API requests to the backend data_service dynamically based on port config."""
    if "/".join(p for p in path.lower().split("/") if p) in _PROXY_REFUSED:
        return JSONResponse(status_code=404, content={"detail": "Not Found"})
    data_port = port_store.get_port("data_service", 8000)
    target_url = f"http://127.0.0.1:{data_port}/{path}"
    url = httpx.URL(target_url, query=request.url.query.encode("utf-8"))
    
    # Follow upstream redirects server-side so the browser is never handed an
    # absolute Location pointing straight at the loopback data_service port
    # (e.g. trailing-slash 307s like /store -> /store/), which would break
    # the same-origin guarantee. Buffering the body lets httpx replay it on
    # redirected requests.
    body = await request.body()
    async with httpx.AsyncClient(timeout=60.0) as client:
        req = client.build_request(
            request.method, url,
            headers={k:v for k,v in request.headers.items() if k.lower() not in ("host", "content-length")},
            content=body,
        )
        resp = await client.send(req, follow_redirects=True)

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers={k:v for k,v in resp.headers.items() if k.lower() not in ("content-length", "content-encoding", "transfer-encoding")},
    )


# ============================================================
# API: route to DLC services — /api/svc/<service>/<path>
# ============================================================
# Pages reach any delivered service (live views included) through the login
# session and the remote tunnel, without knowing its port. This process is
# spawned on its own and must not import client_logic: names, ports and the
# managed folder all come from port_store.

import anyio
from starlette.concurrency import run_in_threadpool

_SVC_NAME = re.compile(r"[a-z0-9_]+_service")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_REMOTE_STATUS_PORT = 8210  # remote_agent.STATUS_PORT; importing remote_agent reads the database
_SVC_TIMEOUT = 60.0  # seconds: connect, answer headers, and each body read except live views
_LIVE_TYPES = ("multipart/x-mixed-replace", "text/event-stream")
_HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailer", "transfer-encoding", "upgrade"}
_SVC_DROP_UP = _HOP_BY_HOP | {"host", "cookie", "authorization", "content-length"}
# date/server: the frontend's own server adds them; X-Svc-Missing only ever comes from this route.
_SVC_DROP_DOWN = _HOP_BY_HOP | {"set-cookie", "content-length", "date", "server", "x-svc-missing"}


def _svc_headers(headers, drop) -> dict:
    """headers minus `drop` and minus whatever their own Connection header names."""
    named = {h.strip().lower() for h in headers.get("connection", "").split(",")}
    return {k: v for k, v in headers.items() if k.lower() not in drop and k.lower() not in named}


def _svc_lookup(service: str):
    """The port to forward to, or the route's own answer. Blocking (database): run it off the event loop."""
    if (not _SVC_NAME.fullmatch(service)
            or service in port_store.CORE_SERVICES or service in port_store.SOCKET_PORTS
            or not any(os.path.exists(os.path.join(port_store.MANAGED_DIR, service, entry))
                       for entry in ("main.so", "main.py"))):
        # The header tells a page "not on this machine" apart from the service's own 404.
        return JSONResponse(status_code=404, content={"detail": f"{service} is not on this machine"},
                            headers={"X-Svc-Missing": "1"})
    ports = port_store.get_all_ports()  # every request: a port moved on the port page applies at once
    if not ports:
        # get_all_ports() answers {} when the table cannot be read; a running client always has core rows.
        return JSONResponse(status_code=503, content={"detail": "the port table cannot be read"})
    if service not in ports:
        return JSONResponse(status_code=404, content={"detail": f"{service} is not on this machine"},
                            headers={"X-Svc-Missing": "1"})
    port = ports[service]
    refused = {*port_store.CORE_SERVICES.values(), *port_store.SOCKET_PORTS.values(),
               port_store._db_settings()["port"], _REMOTE_STATUS_PORT}
    refused |= {p for name, p in ports.items() if name != service}
    if port in refused or not 1 <= port <= 65535:
        return JSONResponse(status_code=502, content={"detail": f"{service} has a port it may not use"})
    return port


async def _svc_body(upstream: httpx.Response, live: bool):
    """The upstream body as it arrives; each read bounded, except a live view's, which may pause."""
    chunks = upstream.aiter_raw()
    while True:
        with anyio.fail_after(None if live else _SVC_TIMEOUT):
            try:
                chunk = await chunks.__anext__()
            except StopAsyncIteration:
                return
        yield chunk


class _UpstreamStream(StreamingResponse):
    """Passes the upstream body on as it arrives; closes the upstream however the stream ends."""

    def __init__(self, upstream: httpx.Response, client: httpx.AsyncClient, **kwargs):
        live = upstream.headers.get("content-type", "").startswith(_LIVE_TYPES)
        super().__init__(_svc_body(upstream, live), **kwargs)
        self._upstream, self._client = upstream, client

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):  # still runs when the browser left mid-stream
                await self._upstream.aclose()
                await self._client.aclose()


@app.api_route("/api/svc/{service}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_svc(service: str, path: str, request: Request):
    # Forward the path and query exactly as the browser sent them (%2F, %3F, %25 stay encoded).
    raw_path = request.scope.get("raw_path") or request.url.path.encode()
    raw_query = request.scope.get("query_string", b"")
    parts = raw_path.split(b"/", 4)  # ["", "api", "svc", <service>, <rest>]; fewer when the name hid a %2F
    if len(parts) < 5 or _CONTROL_CHARS.search(path) or not (raw_path.isascii() and raw_query.isascii()):
        return JSONResponse(status_code=400, content={"detail": "bad characters in the path"})
    target = await run_in_threadpool(_svc_lookup, service)
    if isinstance(target, Response):
        return target
    port = target

    url = httpx.URL(f"http://127.0.0.1:{port}").copy_with(
        raw_path=b"/" + parts[4] + (b"?" + raw_query if raw_query else b""))
    headers = _svc_headers(request.headers, _SVC_DROP_UP)
    body = await request.body()
    # Reads are bounded in _svc_body instead, so a live view may pause for as long as it likes.
    client = httpx.AsyncClient(timeout=httpx.Timeout(_SVC_TIMEOUT, read=None))
    try:
        with anyio.fail_after(_SVC_TIMEOUT):
            upstream = await client.send(client.build_request(request.method, url, headers=headers, content=body),
                                         stream=True)
    except (httpx.HTTPError, TimeoutError):
        await client.aclose()
        return JSONResponse(status_code=502, content={"detail": f"{service} is not answering"})

    resp_headers = _svc_headers(upstream.headers, _SVC_DROP_DOWN)
    if 300 <= upstream.status_code < 400 and "location" in upstream.headers:
        # Not followed: a Location on the service's own port goes back through this route;
        # anything else would send the browser off this origin.
        loc = url.join(upstream.headers["location"])
        if loc.scheme != "http" or loc.host not in ("127.0.0.1", "localhost") or loc.port != port:
            await upstream.aclose()
            await client.aclose()
            return JSONResponse(status_code=502, content={"detail": f"{service} redirected off this machine"})
        resp_headers["location"] = f"/api/svc/{service}{loc.raw_path.decode('ascii')}"
    return _UpstreamStream(upstream, client, status_code=upstream.status_code, headers=resp_headers)





@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Browsers ask for this on their own; without it Safari draws a "1" beside the tab title."""
    from fastapi.responses import FileResponse
    return FileResponse(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "static", "images", "icons", "favicon-64.png"),
                        media_type="image/png")


@app.get("/", response_class=HTMLResponse)
async def root():
    """Root goes to login.

    It used to land on /system, so opening the app showed the port-management
    page for an instant before bouncing to the login form — the first thing a
    store saw on every boot was a settings screen it had no business being on.
    menubar_app.py was changed to open /login for the same reason; this makes
    the plain URL agree with it.
    """
    return RedirectResponse(url="/login")


@app.get("/connection", response_class=HTMLResponse)
async def connection_page(request: Request):
    """Server status check page - pings backend health endpoint."""
    return templates.TemplateResponse("connection.html", {"request": request})


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Login page with username/password form."""
    return templates.TemplateResponse("login.html", {"request": request})


@app.get("/activation", response_class=HTMLResponse)
async def activation_page(request: Request):
    """Activation code entry page."""
    return templates.TemplateResponse("activation.html", {"request": request})


@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    """Initial setup / password change page."""
    return templates.TemplateResponse("setup.html", {"request": request})


@app.get("/create-store", response_class=HTMLResponse)
async def create_store_page(request: Request):
    """Create store page."""
    return templates.TemplateResponse("create_store.html", {"request": request})


# ============================================================
# ROUTES: Navbar Pages (Main app pages with sidebar)
# ============================================================

@app.get("/landing", response_class=HTMLResponse)
async def landing_page(request: Request):
    """Landing page - shown after successful connection."""
    return templates.TemplateResponse("landing.html", {"request": request})


@app.get("/networks", response_class=HTMLResponse)
async def networks_page(request: Request):
    """CCTV network configuration page."""
    return templates.TemplateResponse("networks.html", {"request": request})


@app.get("/selector", response_class=HTMLResponse)
async def selector_page(request: Request):
    """Camera selector page."""
    return templates.TemplateResponse("selector.html", {"request": request})


@app.get("/store-info", response_class=HTMLResponse)
async def store_info_page(request: Request):
    """Store info page."""
    from data_service.configs.version_store import read_client_version
    version = read_client_version(os.path.dirname(BASE_DIR), default="Unknown")
    return templates.TemplateResponse("store_info.html", {"request": request, "app_version": version})


@app.get("/draw", response_class=HTMLResponse)
async def draw_page(request: Request):
    """Zone drawing page for in/out counting."""
    return templates.TemplateResponse("draw.html", {"request": request})


@app.get("/zone-draw", response_class=HTMLResponse)
async def zone_draw_page(request: Request):
    """Hot zone drawing page."""
    return templates.TemplateResponse("zone_draw.html", {"request": request})


@app.get("/rate", response_class=HTMLResponse)
async def rate_page(request: Request):
    """Rate page."""
    return templates.TemplateResponse("rate.html", {"request": request})


@app.get("/attendance", response_class=HTMLResponse)
async def attendance_page(request: Request):
    """Attendance page."""
    return templates.TemplateResponse("attendance.html", {"request": request})


@app.get("/display", response_class=HTMLResponse)
async def display_page(request: Request):
    """Display results / live stream page."""
    return templates.TemplateResponse("display.html", {"request": request})


@app.get("/system", response_class=HTMLResponse)
async def system_page(request: Request):
    """System settings page (port management, restart)."""
    return templates.TemplateResponse("system.html", {"request": request})


@app.get("/status", response_class=HTMLResponse)
async def status_page(request: Request):
    """Service status page — merged into /system, kept for redirects."""
    return RedirectResponse(url="/system")


@app.get("/floorplan", response_class=HTMLResponse)
async def floorplan_page(request: Request):
    """Floorplan management page - also proxies GET JSON for API compatibility."""
    accept = request.headers.get("accept", "")
    # JS fetch for API uses */* or application/json, browser navigation uses text/html
    if "text/html" not in accept:
        return await _forward_to_data_service(request, "floorplan")
    return templates.TemplateResponse("floorplan.html", {"request": request})


@app.get("/floorplan/calibrate", response_class=HTMLResponse)
async def floorplan_calibrate_page(request: Request):
    """Floorplan calibration page."""
    return templates.TemplateResponse("floorplan_calibrate.html", {"request": request})

# ============================================================
# API: System management (port config, restart)
# ============================================================

import subprocess
import signal

# Ports live in the store's database, not in a file next to the bundle.
sys.path.append(os.path.dirname(BASE_DIR))
import port_store

@app.get("/api/system/ports")
async def get_ports():
    """The services this branch owns, straight out of the port table.

    Nothing is filtered here any more. A row exists only because the app was
    installed with that service (the core three) or because the admin pushed
    its DLC folder down — so the table is already exactly the list this
    machine can run, with no defaults to second-guess.
    """
    config = port_store.get_config()
    # One log read for the whole table instead of one per service: the answer
    # to "why is this one down" comes from the supervisor's log, and re-reading
    # it sixteen times to render one page would be wasteful.
    outcomes = port_store._log_outcomes()
    status = {
        name: port_store.diagnose(name, row["port"], outcomes)
        for name, row in config["API_PORTS"].items()
    }
    return {
        "API_HOST": config["API_HOST"],
        "API_PORTS": {name: str(row["port"]) for name, row in config["API_PORTS"].items()},
        "SOCKET_PORTS": {name: str(row["port"]) for name, row in config["SOCKET_PORTS"].items()},
        "UPDATED_AT": {name: row["updated_at"]
                       for section in ("API_PORTS", "SOCKET_PORTS")
                       for name, row in config[section].items()},
        "STATUS": {name: {"state": s["state"], "detail": s["detail"], "process": s.get("process")}
                   for name, s in status.items()},
        # {พอร์ต: เจ้าของ} รวมบริการที่ยังไม่ได้รันอยู่ — หน้าเว็บใช้กันไม่ให้ตั้ง
        # พอร์ตซ้ำกับบริการอื่นในตาราง ซึ่ง lsof มองไม่เห็นตอนบริการนั้นยังไม่ขึ้น
        "PORT_OWNERS": {str(port): name for name, port in port_store.get_all_ports().items()},
    }

from pydantic import BaseModel

class PortUpdate(BaseModel):
    service: str
    port: str

import json as _json

@app.put("/api/system/ports")
async def update_ports(update: PortUpdate):
    """Save a new port for one service — the port page's บันทึก button."""
    svc = update.service
    try:
        port = int(str(update.port))
    except (TypeError, ValueError):
        return JSONResponse(status_code=400, content={"error": "พอร์ตต้องเป็นตัวเลข"})
    if not 1024 <= port <= 65535:
        return JSONResponse(status_code=400, content={"error": "พอร์ตต้องอยู่ระหว่าง 1024-65535"})

    current = port_store.get_all_ports()
    if svc not in current:
        return JSONResponse(status_code=404, content={"error": f"Port key '{svc}' not found"})
    # Two services on one port means whichever starts second dies with
    # "address already in use", and the page would have shown both as fine.
    clash = [name for name, value in current.items() if value == port and name != svc]
    if clash:
        return JSONResponse(status_code=409,
                            content={"error": f"พอร์ต {port} ใช้อยู่แล้วโดย {clash[0]}"})

    if not port_store.set_port(svc, port):
        return JSONResponse(status_code=500, content={"error": "บันทึกลงฐานข้อมูลไม่สำเร็จ"})
    return {"status": "ok", "message": f"Updated {svc} to port {port}"}

class PortAdd(BaseModel):
    type: str
    name: str
    port: str

@app.post("/api/system/ports/add")
async def add_port(add: PortAdd):
    if add.type not in ("API_PORTS", "SOCKET_PORTS"):
        return JSONResponse(status_code=400, content={"error": "type must be API_PORTS or SOCKET_PORTS"})
    try:
        port = int(str(add.port))
    except (TypeError, ValueError):
        return JSONResponse(status_code=400, content={"error": "พอร์ตต้องเป็นตัวเลข"})
    if add.name in port_store.get_all_ports():
        return JSONResponse(status_code=409, content={"error": f"'{add.name}' already exists"})
    type_ = "socket" if add.type == "SOCKET_PORTS" else "api"
    if not port_store.register(add.name, port, type_):
        return JSONResponse(status_code=500, content={"error": "บันทึกลงฐานข้อมูลไม่สำเร็จ"})
    return {"status": "ok", "message": f"Added {add.name} -> {port}"}


@app.delete("/api/system/ports/{name}")
async def delete_port(name: str):
    """Remove a service's port row — the port page's ลบ action."""
    if name in port_store.CORE_SERVICES:
        return JSONResponse(status_code=400, content={"error": "ไม่สามารถลบบริการหลักของระบบได้"})
    if not port_store.unregister(name, reason="removed from port settings page"):
        return JSONResponse(status_code=404, content={"error": f"ไม่พบ '{name}'"})
    return {"status": "ok", "message": f"Removed {name}"}

def _resolved_frontend_port():
    return port_store.get_port("frontend_web", 3000)


@app.post("/api/system/restart/{service_name}")
async def restart_system_service(service_name: str, request: Request):
    # Determine the new port after restart (read port config) for frontend_web
    import port_manager
    client_root = os.path.dirname(BASE_DIR)
    # marker="PORT_CHANGE": makes client_logic.py's watcher launch updater.py
    # in --port-change-restart mode, which (unlike the default --restart-only)
    # leaves AICAMERA_RESTARTED unset — that's what tells menubar_app.py's
    # launch_browser() to pop a fresh browser window once the frontend is
    # back up, instead of leaving the old tab to fend for itself.
    port_manager.request_restart(client_root, marker="PORT_CHANGE")
    _new_port = _resolved_frontend_port()
    _host = request.headers.get('host', '127.0.0.1:3000').split(':')[0]
    _new_url = f"http://{_host}:{_new_port}/login"

    # Let client_logic.py terminate this process gracefully.
    response = JSONResponse({"status": "restarting", "message": "กำลังรีสตาร์ท...", "new_url": _new_url})
    response.delete_cookie(SESSION_COOKIE_NAME, path="/", httponly=True, samesite="strict")
    return response


# ============================================================
# API: Session logout
# ============================================================

@app.api_route("/api/logout", methods=["GET", "POST"])
async def api_logout():
    """Clear the server session cookie (called by the navbar logout button)."""
    response = JSONResponse({"status": "ok"})
    response.delete_cookie(SESSION_COOKIE_NAME, path="/", httponly=True, samesite="strict")
    return response


# ============================================================
# API: Pending update check (admin center)
# ============================================================
# GET {ADMIN_BASE}/branches/bitbucket/pending?branch={branch_name}
# -> {current_version, pending_version}
# ADMIN_BASE comes from API_SERVER_HOST in .env (same host the client
# already talks to for updates). Any failure upstream is swallowed and
# reported as 404 so the UI hides the banner silently.

_ADMIN_BASE = (os.getenv("API_SERVER_HOST") or "http://localhost:3001").rstrip("/")


def _own_activation_code() -> str:
    """This machine's activation code, from the encrypted identity store."""
    try:
        import device_identity
        return device_identity.activation_code()
    except Exception:
        return ""


def _local_data_service_base() -> str:
    return f"http://127.0.0.1:{port_store.get_port('data_service', 8000)}"


def _version_key(version) -> tuple:
    """'V.1.8.4-E2E' -> (1, 8, 4); unparsable -> () so it never looks newer."""
    nums = re.findall(r"\d+", str(version or ""))
    return tuple(int(n) for n in nums[:4]) if nums else ()


async def _pending_from_identified(client):
    """{current_version, pending_version} from the version the admin assigned, or None when nothing is assigned.

    client_logic saves it in the database (client_state); this process has no
    database of its own, so it asks data_service.
    """
    try:
        resp = await client.get(f"{_local_data_service_base()}/store/assigned-version")
        assigned = resp.json().get("assigned_version") if resp.status_code == 200 else None
    except Exception:
        return None
    if not assigned:
        return None
    from data_service.configs.version_store import read_client_version
    return {"current_version": read_client_version(os.path.dirname(BASE_DIR), default=""),
            "pending_version": assigned}


@app.get("/api/updates/pending")
async def api_updates_pending():
    async with httpx.AsyncClient(timeout=10.0) as client:
        # Branch identity = store id (รหัสสาขา) from the local data service.
        branch = None
        try:
            resp = await client.get(f"{_local_data_service_base()}/store/")
            if resp.status_code == 200:
                body = resp.json()
                if isinstance(body, dict):
                    branch = body.get("id")
        except Exception:
            branch = None
        if not branch:
            # A branch that has not filled in its store details yet has no
            # store id, and this check used to give up there — so the '!' mark
            # could never appear on exactly the machines most likely to be
            # mid-setup. The activation code identifies the same branch to the
            # admin (it matches on activation_code / branch_name / serial), and
            # every installed app has one.
            branch = _own_activation_code()
        if not branch:
            return JSONResponse(status_code=404, content={"detail": "no branch"})

        try:
            # ADMIN_UPDATES_BASE optionally overrides the admin host (used by
            # tests); load_secure_env() would otherwise always win because it
            # rewrites API_SERVER_HOST at import time.
            admin_base = os.getenv("ADMIN_UPDATES_BASE") or _ADMIN_BASE
            import device_identity
            pending_url = f"{admin_base}/branches/bitbucket/pending"
            resp = await client.get(
                pending_url,
                params={"branch": branch},
                headers=device_identity.admin_auth_headers(pending_url),
            )
            data = resp.json() if resp.status_code == 200 else None
        except Exception:
            data = None
        if data is None:
            # Since Device Keys (ADR 0007) the admin treats this endpoint as an admin
            # page and answers 403 to shop IPs. The assigned version still reaches the
            # branch in the socket's 'identified' event, which client_logic saves.
            data = await _pending_from_identified(client)
    if data is None:
        return JSONResponse(status_code=404, content={"detail": "not available"})

    current = data.get("current_version")
    pending = data.get("pending_version")
    return {
        "branch": branch,
        "current_version": current,
        "pending_version": pending,
        # Any difference from the assigned version is something to act on:
        # these strings are not reliably numeric (V.OLD.0 keys to [0], so a
        # numeric ">" quietly reported "nothing pending" for every branch that
        # was actually behind).
        "has_pending": bool(pending) and pending != current,
    }


# ============================================================
# API: Update in progress (admin-triggered push)
# ============================================================
# The admin's force-update arrives over the client supervisor's Socket.IO
# connection, which the browser knows nothing about — so a store watching this
# page saw it go unresponsive with no explanation, while pressing Update
# yourself has always shown a full-screen "updating" notice. The supervisor
# writes the incoming payload into <app>/temp_update and the updater deletes
# that folder when it finishes, so its presence is exactly the window the
# notice should be up for, with nothing new to keep in sync.
_APP_ROOT = os.path.dirname(BASE_DIR)
_TEMP_UPDATE_DIR = os.path.join(_APP_ROOT, "temp_update")


@app.get("/api/session")
async def api_session(request: Request):
    """Whether the server still accepts this browser's session cookie.

    The login page keeps its own copy of the signed-in user in sessionStorage
    and used to trust it on its own. When the server session ends but that copy
    survives — an update invalidating the cookie is the obvious way — the page
    bounced to /landing, the middleware bounced it back to /login, and the tab
    flipped between the two forever. Public on purpose: it is the one thing an
    unauthenticated page must be able to ask.
    """
    return {"authenticated": has_valid_session(request)}


@app.get("/api/updates/in-progress")
async def api_update_in_progress():
    """True while an update payload is staged or being applied.

    Deliberately unauthenticated: it returns a single boolean about this
    machine's own updater, and the notice has to work on the login page too —
    an update can land while nobody is signed in.
    """
    return {"updating": os.path.isdir(_TEMP_UPDATE_DIR), "restarting": _restart_notice_up(),
            "starting": _startup_window_up()}


def _startup_window_up() -> bool:
    """True while the "Starting System" window (.loading_gui.py) is still on screen.

    After a restart the page waits for this to go, so it reloads after the window
    has said Ready and closed, not ahead of it. Every launcher (the .app, bootstrap.py,
    client_logic.py for a git clone) runs it by that file name.
    """
    try:
        return subprocess.run(["pgrep", "-f", r"\.loading_gui\.py"], capture_output=True,
                              timeout=3).returncode == 0
    except Exception:
        return False


def _restart_notice_up() -> bool:
    """True for a few minutes after the menu bar's Restart (runtime_paths.RESTART_NOTICE_FILE)."""
    try:
        import runtime_paths
        age = time.time() - os.path.getmtime(runtime_paths.RESTART_NOTICE_FILE)
        return 0 <= age < runtime_paths.RESTART_NOTICE_MAX_AGE
    except Exception:
        return False


# ============================================================
# API Health Check (for the frontend server itself)
# ============================================================

@app.get("/frontend-health")
async def frontend_health():
    """Health check for the frontend server."""
    return {"status": "ok", "service": "frontend_web"}

@app.get("/api/system/check-port/{port}")
async def check_port(port: int):
    """Who is holding this port, as three distinct answers.

    A plain connect() could only say "something answered", which reads the
    same whether the service is healthy or a completely unrelated program
    took the number. Asking the OS who owns the socket separates our own
    service (online) from a clash, and names the other program so the shop
    can see what to close.
    """
    return port_store.port_status(port)


# ============================================================
# Run directly for development
# ============================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=_resolved_frontend_port())
