# frontend/gate.py
"""Request gate: decides who may reach each HTTP entry point (ADR 0007).

Every request is one of four kinds, each with its own rule:
- activation:  the Activation proxy a Branch calls to receive its Device Key —
               from any IP, throttled by serial and by client IP
- branch_rest: the uploads a Branch makes (pipeline dummy-data, build farm zip)
- branch_feed: the traffic feeds a Branch sends (ADR 0009) — Device Key only,
               from any IP, with no IP fallback: no keyless client ever called them
- admin:       everything else, i.e. the admin UI pages and admin APIs

Access rules for these kinds live here and nowhere else, so the handlers never
decide who may call them. The Branch socket is answered by the Socket.IO
middleware in front of Flask and never reaches this hook; its connect and
identify handlers ask check_socket_connect and socket_identify_refusal instead.

Never log a Device Key, an Authorization header, or a verify-device error body
(a 422 echoes the request, key included).
"""
from __future__ import annotations

import os
import time
from typing import Optional, Tuple

import requests
from flask import abort, current_app, g, jsonify, request
from flask_socketio import ConnectionRefusedError

from config import Config
from services.api_client import api_post

# Anything not listed, including an unknown URL, is admin.
_KIND_BY_ENDPOINT = {
    "ai_camera.api_redirect_activate": "activation",   # POST /ai-camera/api/auth/activate
    "ai_camera.api_dummy_data_upload": "branch_rest",  # POST /ai-camera/api/dummy-data/upload
    "ai_camera.api_build_upload": "branch_rest",       # POST /ai-camera/api/build/upload
    "ai_camera.api_traffic_feed": "branch_feed",       # POST /ai-camera/api/traffic/<feed>
    "ai_camera.api_camera_alert_feed": "branch_feed",  # POST /ai-camera/api/camera-alert/<feed>
    "ai_camera.api_camera_placement": "branch_feed",   # POST /ai-camera/api/camera-placement
}

_VERDICTS = ("ok", "invalid_device_key", "no_key", "unknown_activation_code")

# Activation throttle: more than ACTIVATION_MAX_FAILURES failed guesses within
# ACTIVATION_WINDOW seconds, for one serial or one client IP, answers 429. Only
# the backend's refusals count; a 5xx or 503 is an outage, not a guess, and must
# not lock a Branch out after the backend recovers.
ACTIVATION_COUNTED_STATUSES = (401, 404, 409)
ACTIVATION_MAX_FAILURES = 5
ACTIVATION_WINDOW = 3600
_SWEEP_ABOVE = 1000
# ponytail: per-process memory, a restart resets it; move to a shared store if
# the frontend ever runs more than one process.
_activation_failures = {}  # "ip:<ip>" / "serial:<serial>" -> failure times
_clock = time.monotonic


def client_ip() -> str:
    """The first X-Forwarded-For entry, else the peer address.

    Trusting the header is an accepted risk (ADR 0007): if nginx appends to it
    rather than overwriting it, a caller can claim any IP, the office's included.
    """
    forwarded_for = request.headers.get("X-Forwarded-For", "")
    return forwarded_for.split(",")[0] if forwarded_for else request.remote_addr


def verify_device(activation_code: str, device_key: Optional[str] = None) -> Optional[str]:
    """Ask the backend whether device_key belongs to activation_code.

    Without a key, the verdict says whether the code is known and holds one.
    Returns one of _VERDICTS, or None when the backend gave no verdict: the
    request failed or timed out, answered other than 200, or its 200 body has
    no recognised verdict. None is never to be read as ok or as a bad key.
    """
    # Mock admin only: the Device Keys live in the store's MySQL users table,
    # which this copy does not reach. MOCK_TRUST_DEVICES=1 (run_mock_admin.sh)
    # takes any Branch at its word so a real machine can be pointed here.
    if os.getenv("MOCK_TRUST_DEVICES") == "1":
        return "ok" if device_key else "no_key"
    try:
        resp = api_post(
            "/auth/verify-device",
            {"activation_code": activation_code, "device_key": device_key},
            timeout=current_app.config["HTTP_TIMEOUT_SHORT"],
        )
    except requests.exceptions.RequestException as exc:
        current_app.logger.error(f"verify-device unreachable: {exc}")
        return None
    if resp.status_code != 200:
        current_app.logger.error(f"verify-device answered {resp.status_code}")
        return None
    try:
        body = resp.json()
    except ValueError:
        body = None
    verdict = body.get("verdict") if isinstance(body, dict) else None
    if verdict not in _VERDICTS:
        current_app.logger.error("verify-device answered 200 without a recognised verdict")
        return None
    return verdict


def _bearer_token() -> str:
    """The token of `Authorization: Bearer <token>`, any case and spacing; "" if none."""
    parts = request.headers.get("Authorization", "").split(None, 1)
    return parts[1].strip() if len(parts) == 2 and parts[0].lower() == "bearer" else ""


def _check_device_key(code: str, key: str, kind: str):
    """Judge a Branch REST call or feed by its Device Key alone, from any IP.

    Only `ok` passes. Every other verdict answers 401 so the client re-activates,
    `no_key` included: that is a key presented after an admin cleared it. One
    header without the other is a failed key check too. No verdict means the
    backend is down: 503 tells the client to retry, where a 401 would send every
    Branch to re-activate and into 409.
    """
    verdict = verify_device(code, key) if code and key else "missing X-Activation-Code or Bearer key"
    if verdict is None:
        return jsonify({"error": "Upstream service unavailable"}), 503
    if verdict != "ok":
        current_app.logger.info(f"Refused {kind} request for {code or '-'} from IP {client_ip()}: {verdict}")
        return jsonify({"error": "invalid_device_key"}), 401
    return None


def _activation_keys():
    """Throttle keys for this Activation: always the client IP, plus the serial if sent."""
    keys = ["ip:" + client_ip()]
    body = request.get_json(silent=True)
    serial = body.get("serial_number") if isinstance(body, dict) else None
    if isinstance(serial, str) and serial.strip():
        keys.append("serial:" + serial.strip())
    return keys


def _recent_failures(key: str, now: float) -> int:
    stamps = [t for t in _activation_failures.get(key, ()) if now - t < ACTIVATION_WINDOW]
    if stamps:
        _activation_failures[key] = stamps
    else:
        _activation_failures.pop(key, None)
    return len(stamps)


def _check_activation():
    """Activation is open to every IP, throttled by serial and by client IP."""
    now = _clock()
    keys = _activation_keys()
    if any(_recent_failures(key, now) >= ACTIVATION_MAX_FAILURES for key in keys):
        current_app.logger.info(f"Throttled Activation from IP {client_ip()}")
        return jsonify({"error": "too_many_attempts"}), 429
    g.activation_keys = keys
    return None


def record_activation_result(response):
    """after_request hook: a refused Activation (401, 404, 409) counts toward the throttle."""
    keys = g.pop("activation_keys", None)
    if keys and response.status_code in ACTIVATION_COUNTED_STATUSES:
        now = _clock()
        if len(_activation_failures) > _SWEEP_ABOVE:
            for key in list(_activation_failures):
                _recent_failures(key, now)
        for key in keys:
            _activation_failures.setdefault(key, []).append(now)
    return response


_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _same_origin_or_none() -> bool:
    """False when a browser says a request that changes data came from another
    origin. Browsers send Origin on every cross-origin POST/PUT/DELETE (and on
    sendBeacon); Referer is the fallback. A request with neither is not from a
    browser page (curl, a script on this host) and is judged by IP alone."""
    if request.method in _SAFE_METHODS:
        return True
    own = Config.ADMIN_PUBLIC_ORIGIN or request.host_url.rstrip("/")
    origin = request.headers.get("Origin", "").strip()
    if origin:
        return origin == own
    referer = request.headers.get("Referer", "").strip()
    if referer:
        return referer == own or referer.startswith(own + "/")
    return True


def check_request():
    """before_request hook: None lets the request through, else the refusal.

    A Branch REST call carrying either Device Key header is judged by its key,
    so an Authorization header that is not `Bearer <key>` fails the check rather
    than counting as keyless. A traffic feed is always judged by its key, so a
    keyless one is 401 from any IP. Every other request, keyless Branch REST
    included, needs its client IP on its kind's list, else 403.
    """
    kind = _KIND_BY_ENDPOINT.get(request.endpoint, "admin")
    code = request.headers.get("X-Activation-Code", "").strip()
    if kind == "activation":
        return _check_activation()
    if kind == "branch_feed":
        return _check_device_key(code, _bearer_token(), kind)
    if kind == "branch_rest":
        if code or request.headers.get("Authorization", "").strip() or Config.REQUIRE_DEVICE_KEY:
            return _check_device_key(code, _bearer_token(), kind)
    allowed = Config.OFFICE_IPS if kind == "admin" else Config.TRANSITION_IPS
    ip = client_ip()
    if ip not in allowed:
        current_app.logger.info(f"Blocked unauthorized {kind} request from IP: {ip}")
        abort(403)
    if kind == "admin" and not _same_origin_or_none():
        # A page on another origin (any site an office browser has open, or a
        # branch's UI on the remote origin) cannot change data here: the office IP
        # is the only credential, and that browser carries it everywhere.
        current_app.logger.info(f"Blocked cross-origin {request.method} {request.path} from IP: {ip}")
        abort(403)
    if kind == "branch_rest":
        # Readiness signal for the switch: these are the uploads it would start refusing.
        current_app.logger.info(f"Keyless branch_rest request passed by IP: {ip}")
    return None


def check_socket_connect(auth) -> Tuple[Optional[str], bool]:
    """Judge a Branch socket connect by its `auth`; refusing raises ConnectionRefusedError.

    Returns the Activation Code the connection proved, which its identify must
    then name, and whether it proved it with a Device Key. A connection without
    auth (the old client) gets (None, False): accepted provisionally and judged
    at identify. With a Device Key only `ok` gets in; without one only `no_key`,
    and an unknown code is told so. No verdict means the backend is down:
    Socket.IO's default rejection, with neither contract message, makes the
    client retry instead of dropping its key or stopping. A refused connect,
    unlike a disconnect after it, is retried: the client's connect() raises and
    its start_socket_client loop tries again.
    """
    auth = auth if isinstance(auth, dict) else {}
    code, key = auth.get("activation_code"), auth.get("device_key")
    if not key and Config.REQUIRE_DEVICE_KEY:
        current_app.logger.info(f"Refused keyless branch socket for {code or '-'} from IP {client_ip()}")
        raise ConnectionRefusedError("invalid_device_key")
    if not code and not key:
        return None, False
    verdict = verify_device(code, key or None) if code else "missing activation_code"
    if verdict is None:
        raise ConnectionRefusedError()
    if verdict == ("ok" if key else "no_key"):
        return code, bool(key)
    current_app.logger.info(f"Refused branch socket for {code or '-'} from IP {client_ip()}: {verdict}")
    if not key and verdict == "unknown_activation_code":
        raise ConnectionRefusedError("unknown_activation_code")
    raise ConnectionRefusedError("invalid_device_key")


def socket_identify_refusal(connected_code: Optional[str], keyed: bool, activate_id) -> Optional[str]:
    """Why a Branch socket may not identify as activate_id, or None when it may.

    A connection that proved a code may name only that code, so it cannot take
    over another Branch's connection. Without a Device Key, every identify also
    needs activate_id to have no key on record, so a keyless connection does not
    outlive its Branch receiving a key. Any other verdict and the caller
    disconnects it for good: the client does not reconnect after a disconnect
    the server started. The reason goes into the caller's log line.

    No verdict (backend down or overloaded) lets a keyless identify through
    while the switch is off, as before identify asked the backend at all.
    Refusing it would leave the Branch offline until someone restarts it.
    """
    if connected_code is not None and activate_id != connected_code:
        return f"connected as {connected_code}"
    if keyed:
        return None
    if Config.REQUIRE_DEVICE_KEY:
        return "no device key while REQUIRE_DEVICE_KEY is on"
    verdict = verify_device(activate_id)
    if verdict is None:
        current_app.logger.warning(f"verify-device gave no verdict; allowed keyless identify as {activate_id}")
        return None
    return None if verdict == "no_key" else f"keyless, verify-device said {verdict}"
