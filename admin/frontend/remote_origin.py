# frontend/remote_origin.py
"""A branch's web pages, served from an origin of their own.

The remote page frames a branch's UI so an admin can work in it. Served from
the admin's own origin, as the prototype did, every script in that UI would run
as the admin page: a branch Mac someone tampered with could then call any admin
API from inside the admin's browser — push an update to every branch included.
So the branch's pages come from here instead, a separate port (and in
production a separate host, AI_CAMERA_ADMIN_REMOTE_ORIGIN): the browser keeps
the two origins apart, and nothing here can reach the admin page or its APIs.

Who gets in:
  - only callers from the office IPs, like the admin UI (gate.py);
  - only a browser holding the viewer key of the branch's live session. The
    admin page gets a one-use enter ticket when it opens the session
    (blueprints/remote.py); /remote/<id>/__enter trades it for an HttpOnly,
    SameSite=Strict cookie scoped to that branch's path;
  - only framed by the admin page itself (CSP frame-ancestors).

Run beside the admin frontend in the same eventlet hub (app_ai_camera_frontend
starts it), because the tunnel state in services/remote_tunnel lives in that
process.
"""
from __future__ import annotations

import re
import uuid
from typing import Dict, Iterator, Tuple
from urllib.parse import urlsplit

from flask import Flask, Response, abort, redirect, request, stream_with_context

from config import Config
from gate import client_ip
from services import audit_log, remote_tunnel

_VIEWER_COOKIE = "aicam_rv"
_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length", "host",
})
# The branch's own framing rules would stop the admin page from showing it;
# this origin sets its own instead (see _security_headers).
_BLOCKED_RESPONSE_HEADERS = frozenset({
    "content-security-policy", "content-security-policy-report-only",
    "x-frame-options",
})

remote_app = Flask(__name__)


def _prefix_for(client_id: str) -> str:
    return f"/remote/{client_id}/"


def _admin_origin() -> str:
    """The admin page's origin, the only one allowed to frame this one."""
    if Config.ADMIN_PUBLIC_ORIGIN:
        return Config.ADMIN_PUBLIC_ORIGIN
    host = urlsplit(request.host_url).hostname or "localhost"
    return f"{request.scheme}://{host}:{Config.FRONTEND_PORT}"


def _security_headers(resp: Response) -> Response:
    resp.headers["Content-Security-Policy"] = f"frame-ancestors {_admin_origin()}"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


# --------------------------------------------------------------------------------------
# cookie: the branch's cookies get a per-branch name and path, so two branches
# (or this origin's viewer cookie) never collide
# --------------------------------------------------------------------------------------
def _cookie_prefix(client_id: str) -> str:
    return f"rc_{re.sub(r'[^A-Za-z0-9]', '', client_id)}_"


def _rewrite_set_cookie(value: str, client_id: str) -> str:
    name, _, rest = value.partition("=")
    parts = rest.split(";")
    attrs = [p for p in parts[1:] if not p.strip().lower().startswith(("path=", "domain="))]
    attrs.append(f"Path={_prefix_for(client_id)}")
    return f"{_cookie_prefix(client_id)}{name.strip()}={parts[0]}" + "".join(f";{a}" for a in attrs)


def _rewrite_cookie_header(client_id: str) -> str:
    """Only this branch's own cookies go back to it, under their own names."""
    prefix = _cookie_prefix(client_id)
    return "; ".join(f"{name[len(prefix):]}={value}"
                     for name, value in request.cookies.items() if name.startswith(prefix))


# --------------------------------------------------------------------------------------
# HTML: <base href> plus absolute paths moved under the branch's prefix
# --------------------------------------------------------------------------------------
_HEAD_RE = re.compile(rb"<head[^>]*>", re.IGNORECASE)
# href="/xxx" src='/xxx' action="/xxx" — not //host, which is another host
_ABS_ATTR_RE = re.compile(rb"""\b(href|src|action)=(["'])/(?!/)""", re.IGNORECASE)


def _rewrite_html(html: bytes, client_id: str) -> bytes:
    prefix = _prefix_for(client_id).encode()
    html = _ABS_ATTR_RE.sub(rb"\1=\2" + prefix, html)
    tag = f'<base href="{_prefix_for(client_id)}">'.encode()
    match = _HEAD_RE.search(html)
    if match:
        return html[: match.end()] + tag + html[match.end():]
    return tag + html


def _forward_request_headers(client_id: str) -> Dict[str, str]:
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in _HOP_BY_HOP and k.lower() != "cookie"}
    cookie = _rewrite_cookie_header(client_id)
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _response_headers(raw_headers: Dict[str, str], client_id: str) -> Tuple[list, str]:
    out = []
    content_type = next((v for k, v in raw_headers.items() if k.lower() == "content-type"), "")
    for key, value in raw_headers.items():
        low = key.lower()
        if low in _HOP_BY_HOP or low in _BLOCKED_RESPONSE_HEADERS:
            continue
        if low == "set-cookie":
            out.append((key, _rewrite_set_cookie(value, client_id)))
        elif low == "location":
            out.append((key, re.sub(r"^/(?!/)", _prefix_for(client_id), value)))
        else:
            out.append((key, value))
    return out, content_type


# --------------------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------------------
@remote_app.before_request
def _gate():
    if client_ip() not in Config.OFFICE_IPS:
        abort(403)


@remote_app.after_request
def _after(resp: Response) -> Response:
    return _security_headers(resp)


@remote_app.route("/remote/<client_id>/__enter")
def enter(client_id: str):
    """Trade the one-use ticket from the admin page for the viewer cookie."""
    if not _CLIENT_ID_RE.match(client_id):
        abort(404)
    key = remote_tunnel.redeem_enter_ticket(client_id, request.args.get("t", ""))
    if not key:
        audit_log.record("remote_enter", client_id, result="bad_ticket")
        abort(403)
    audit_log.record("remote_enter", client_id)
    resp = redirect(_prefix_for(client_id))
    resp.set_cookie(_VIEWER_COOKIE, key, path=_prefix_for(client_id), httponly=True,
                    samesite="Strict", secure=request.is_secure)
    return resp


@remote_app.route("/remote/<client_id>/", defaults={"subpath": ""},
                  methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
@remote_app.route("/remote/<client_id>/<path:subpath>",
                  methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
def proxy(client_id: str, subpath: str):
    """Every request of the branch's page goes down the Socket.IO tunnel."""
    if not _CLIENT_ID_RE.match(client_id):
        abort(404)
    if not remote_tunnel.viewer_key_ok(client_id, request.cookies.get(_VIEWER_COOKIE, "")):
        abort(403)
    if subpath.startswith("__fs/"):
        # Files read off the branch Mac: who read which one goes on record.
        audit_log.record("remote_file", client_id, "/" + subpath)

    query = request.query_string.decode()
    target = "/" + subpath + (f"?{query}" if query else "")
    stream_id = uuid.uuid4().hex
    try:
        status, resp_headers, stream = remote_tunnel.open_request(
            client_id, request.method, target,
            _forward_request_headers(client_id), request.get_data(), stream_id)
    except remote_tunnel.RemoteError as exc:
        return Response(f"ต่อท่อรีโมตไม่ได้: {exc}", status=502, mimetype="text/plain")

    headers, content_type = _response_headers(resp_headers, client_id)
    # HTML is read whole to put <base> in; pages are small, streams are not HTML.
    if content_type.startswith("text/html"):
        body = _rewrite_html(b"".join(stream), client_id)
        return Response(body, status=status, headers=headers, content_type=content_type)

    @stream_with_context
    def relay() -> Iterator[bytes]:
        try:
            for chunk in stream:
                yield chunk
        finally:
            # The viewer closed the tab or switched camera: stop the branch's source.
            remote_tunnel.abort_request(client_id, stream_id)

    return Response(relay(), status=status, headers=headers, content_type=content_type)
