"""Camera alert feeds (mounted at /camera-alert-service).

Reached only through the frontend's Device Key gate, which puts the Branch the key
proved in X-Branch-Code. The body never names the Branch.
"""
from __future__ import annotations

import base64
import binascii
import logging
import threading
import time
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Header, Request
from fastapi.concurrency import run_in_threadpool  # asyncio.to_thread is 3.9+; the server runs 3.8
from fastapi.responses import JSONResponse
from sqlalchemy import select

from camera_alert_service import alerts, notifier
from camera_alert_service.models import CameraHealth
from data_service.core.database.branch_db import Base, get_engine, get_session_factory
from data_service.core.models.branch import Branch

logger = logging.getLogger(__name__)

app = FastAPI(title="Camera alert service", docs_url=None, redoc_url=None, openapi_url=None)

# event_id -> when it was sent. A Branch retries an event the relay could not take,
# and could retry one that did go out; this keeps the second copy off LINE.
_SENT_EVENTS: dict = {}
_SENT_EVENTS_TTL = 24 * 3600
_sent_lock = threading.Lock()
# image_id -> when it was sent: a picture retried after a lost answer is not sent twice.
_SENT_IMAGES: dict = {}
MAX_IMAGE_BYTES = 2 * 1024 * 1024


def init(start_loop: bool = True) -> None:
    """Create the table and start the alert loop. Called by the backend's main()."""
    Base.metadata.create_all(bind=get_engine(), tables=[CameraHealth.__table__])
    if start_loop:
        notifier.start_alert_loop(get_session_factory())


def branch_name(session, code: str) -> Optional[str]:
    return session.scalar(select(Branch.branch_name).where(Branch.activation_code == code))


def _bad_request(exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": str(exc)})


async def _json(request: Request):
    try:
        return await request.json()
    except ValueError:
        raise alerts.ReportError("body is not JSON")


@app.post("/health")
async def health(request: Request, x_branch_code: str = Header(default="")):
    code = x_branch_code.strip()
    if not code:
        return JSONResponse(status_code=400, content={"error": "missing X-Branch-Code"})
    try:
        body = await _json(request)
        # In a worker thread for the same reason as LINE below: the database calls block.
        count = await run_in_threadpool(_record_health, code, body)
    except alerts.ReportError as exc:
        return _bad_request(exc)
    return {"ok": True, "cameras": count}


def _record_health(code: str, body) -> int:
    with get_session_factory()() as session:
        if isinstance(body, dict) and not alerts.store_label(body.get("store")):
            # No store info from the Branch: fall back to the name it identified with.
            name = branch_name(session, code)
            if name:
                body = {**body, "store": {"branch_name": name}}
        return alerts.apply_health(session, code, body, datetime.now())


def _event_label(code: str, body: dict) -> str:
    with get_session_factory()() as session:
        return alerts.store_label(body.get("store")) or branch_name(session, code) or "สาขา (ไม่ทราบชื่อ)"


@app.post("/event")
async def event(request: Request, x_branch_code: str = Header(default="")):
    code = x_branch_code.strip()
    if not code:
        return JSONResponse(status_code=400, content={"error": "missing X-Branch-Code"})
    try:
        body = await _json(request)
        if not isinstance(body, dict):
            raise alerts.ReportError("body must be a JSON object")
        label = await run_in_threadpool(_event_label, code, body)
        message = alerts.event_message(label, body)
    except alerts.ReportError as exc:
        return _bad_request(exc)

    event_id = body.get("event_id")
    key = f"{code}:{event_id}" if isinstance(event_id, str) and event_id else None
    now = time.time()
    with _sent_lock:
        for k in [k for k, t in _SENT_EVENTS.items() if now - t > _SENT_EVENTS_TTL]:
            del _SENT_EVENTS[k]
        if key and key in _SENT_EVENTS:
            return {"ok": True, "duplicate": True}
        if key:
            # Claimed before sending: a retry that arrives while this one is
            # still waiting on LINE must not send the message a second time.
            _SENT_EVENTS[key] = now
    # In a worker thread: the relay can take up to its 10 s timeout, and a
    # blocking call here would stall every other request of the admin backend.
    if not await run_in_threadpool(notifier.send_line, message, notifier.group_for(body.get("type"))):
        if key:
            with _sent_lock:
                _SENT_EVENTS.pop(key, None)
        # 502 so the Branch keeps the event and tries again.
        return JSONResponse(status_code=502, content={"error": "LINE relay unavailable"})
    return {"ok": True}


@app.post("/image")
async def image(request: Request, x_branch_code: str = Header(default="")):
    """One picture of an alert whose text /event already sent, to the same group as that text.

    The body is {event_id, type, camera, image_id, jpeg (base64)}. A picture that fails
    is the Branch's to retry; the text is never held back for it.
    """
    code = x_branch_code.strip()
    if not code:
        return JSONResponse(status_code=400, content={"error": "missing X-Branch-Code"})
    try:
        body = await _json(request)
        if not isinstance(body, dict):
            raise alerts.ReportError("body must be a JSON object")
        data = body.get("jpeg")
        image_id = body.get("image_id")
        if not isinstance(data, str) or not isinstance(image_id, str) or not image_id:
            raise alerts.ReportError("a picture needs jpeg and image_id")
        raw = base64.b64decode(data, validate=True)
    except alerts.ReportError as exc:
        return _bad_request(exc)
    except (binascii.Error, ValueError):
        return _bad_request(alerts.ReportError("jpeg is not base64"))
    if len(raw) > MAX_IMAGE_BYTES or raw[:2] != b"\xff\xd8":
        return _bad_request(alerts.ReportError("jpeg must be a JPEG of at most 2 MB"))
    key = f"{code}:{image_id}"
    now = time.time()
    with _sent_lock:
        for k in [k for k, t in _SENT_IMAGES.items() if now - t > _SENT_EVENTS_TTL]:
            del _SENT_IMAGES[k]
        if key in _SENT_IMAGES:
            return {"ok": True, "duplicate": True}
        _SENT_IMAGES[key] = now
    if not await run_in_threadpool(notifier.send_image, data, notifier.group_for(body.get("type"))):
        with _sent_lock:
            _SENT_IMAGES.pop(key, None)
        return JSONResponse(status_code=502, content={"error": "LINE relay unavailable"})
    return {"ok": True}
