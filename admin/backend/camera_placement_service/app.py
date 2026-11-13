"""Camera Placement relay (mounted at /camera-placement-service).

Reached only through the frontend's Device Key gate, which puts the Branch the key
proved in X-Branch-Code. A Branch sends one still from one camera; the server owns the
prompt and the OpenRouter key, asks Jev whether the camera films inside or outside the
shop, and returns OpenRouter's answer exactly as it came. The Branch decides from it.
Contract for the Branch side: docs/client-contract.md
"""
from __future__ import annotations

import base64
import binascii
import io
import json
import logging
import os
import time

import requests
from fastapi import FastAPI, Header, Request
from fastapi.concurrency import run_in_threadpool  # asyncio.to_thread is 3.9+; the server runs 3.8
from fastapi.responses import JSONResponse, Response
from PIL import Image, UnidentifiedImageError

logger = logging.getLogger(__name__)

app = FastAPI(title="Camera placement service", docs_url=None, redoc_url=None, openapi_url=None)

URL = "https://openrouter.ai/api/alpha/decisions"
MODEL = os.getenv("CAMERA_PLACEMENT_MODEL", "typesafe/jev-1.14")
# Jev takes 280–1568 px per side (docs.typesafe.ai/images.md). Frames are scaled to fit;
# MAX_SIDE may be set lower to send smaller (cheaper) pictures.
MIN_SIDE, JEV_MAX_SIDE = 280, 1568
MAX_SIDE = int(os.getenv("CAMERA_PLACEMENT_MAX_SIDE", str(JEV_MAX_SIDE)))
assert MIN_SIDE <= MAX_SIDE <= JEV_MAX_SIDE, "CAMERA_PLACEMENT_MAX_SIDE must be 280–1568"
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_PIXELS = 40_000_000  # well above a 4K or 12 MP camera frame
# The key lives where every python_server service keeps its secrets.
SERVER_ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", ".env")
JEV_TIMEOUT = 25  # under the frontend's 30 s forward timeout

# Names no branch, so one prompt serves every shop. Tested on UNS037 (45 cameras) and
# AVI008 (13) on 29 Oct 2026; dropping the branch name changed no label.
DESCRIPTION = (
    "คุณคือผู้ตรวจภาพกล้องวงจรปิดของร้านขายคอมพิวเตอร์และอุปกรณ์ไอที "
    "งาน: ดูภาพ 1 ภาพจากกล้อง 1 ตัว แล้วตัดสินว่ากล้องตัวนี้ถ่าย \"ในร้าน\" หรือ \"นอกร้าน\" "
    "กติกา: ดูว่าพื้นที่ส่วนใหญ่ของภาพ (เกินครึ่ง) เป็นที่ไหน · เห็นถนนผ่านกระจกเล็ก ๆ แต่ส่วนใหญ่เป็นในร้าน = inside · "
    "ไม่สนใจข้อความเวลาและชื่อช่องบนภาพ · หลักฐานไม่พอ = uncertain")
QUESTIONS = {
    "label": {"type": "choice", "instructions": "กล้องตัวนี้ถ่ายในร้านหรือนอกร้าน (ดูพื้นที่ส่วนใหญ่ของภาพ)", "criteria": {
        "inside": "พื้นที่ภายในอาคารของร้าน ได้แก่ พื้นที่ขาย ชั้นวางสินค้า โต๊ะโชว์ เคาน์เตอร์แคชเชียร์ จุดรับซ่อม/บริการ ที่นั่งรอ ห้องเก็บสต็อก/หลังร้าน",
        "outside": "พื้นที่นอกอาคาร ได้แก่ ถนน ลานจอดรถ ทางเท้า หน้าร้านฝั่งนอกประตู รั้ว ต้นไม้ ท้องฟ้า",
        "uncertain": "ภาพมืด/เบลอ/ไม่มีสัญญาณ หรือเห็นทั้งในและนอกพอ ๆ กัน หรือหลักฐานไม่พอ"}},
    "area": {"type": "choice", "instructions": "พื้นที่ส่วนใหญ่ของภาพคือที่ไหน", "criteria": {
        "sales_floor": "พื้นที่ขาย ชั้นวางสินค้า โต๊ะโชว์",
        "cashier": "เคาน์เตอร์แคชเชียร์",
        "service": "จุดรับซ่อม/บริการ ที่นั่งรอ",
        "stock": "ห้องเก็บสต็อก/หลังร้าน",
        "road_parking": "ถนน ลานจอดรถ ทางเท้า",
        "storefront_outside": "หน้าร้านฝั่งนอกประตู รั้ว ต้นไม้",
        "no_signal": "ภาพมืด/เบลอ/ไม่มีสัญญาณ"}},
}


class BadPicture(ValueError):
    """The Branch sent something Jev cannot be asked about. Resending it fails the same way."""


class JevUnavailable(RuntimeError):
    """OpenRouter could not answer (our key, credit, rate limit, outage, timeout). Try later."""


def fit(jpeg: bytes) -> bytes:
    """Scale a JPEG so both sides are within what Jev takes; unchanged when it already fits."""
    try:
        img = Image.open(io.BytesIO(jpeg))  # lazy: reads the header only
    except (UnidentifiedImageError, OSError) as exc:
        raise BadPicture("jpeg is not a readable picture") from exc
    if img.format != "JPEG":
        raise BadPicture("jpeg must be a JPEG picture")
    w, h = img.size
    # A small file can declare a huge picture; check before decoding so it cannot eat the memory.
    if w * h > MAX_PIXELS:
        raise BadPicture(f"a {w}x{h} picture is over {MAX_PIXELS // 1_000_000} megapixels")
    try:
        img.load()
    except OSError as exc:
        raise BadPicture("jpeg is not a readable picture") from exc
    k = min(1.0, MAX_SIDE / max(w, h))
    if min(w, h) * k < MIN_SIDE:  # too small (or shrunk too far): grow the short side to 280
        k = MIN_SIDE / min(w, h)
    size = (round(w * k), round(h * k))
    if max(size) > JEV_MAX_SIDE:
        raise BadPicture(f"a {w}x{h} picture is too long and thin to fit 280–1568 px per side")
    if k == 1.0:
        return jpeg
    out = io.BytesIO()
    img.convert("RGB").resize(size, Image.LANCZOS).save(out, "JPEG", quality=90)
    return out.getvalue()


def parse(body) -> tuple:
    """(jpeg bytes, camera) from {"jpeg": base64, "camera": optional label}."""
    if not isinstance(body, dict) or not isinstance(body.get("jpeg"), str) or not body["jpeg"]:
        raise BadPicture("body must be {\"jpeg\": \"<base64>\"}")
    camera = body.get("camera")
    if camera is not None and (not isinstance(camera, str) or len(camera) > 64):
        raise BadPicture("camera must be text of at most 64 characters")
    try:
        jpeg = base64.b64decode(body["jpeg"], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise BadPicture("jpeg is not base64") from exc
    if len(jpeg) > MAX_IMAGE_BYTES:
        raise BadPicture("jpeg is over 2 MB")
    return jpeg, camera or ""


def openrouter_key() -> str:
    """OPENROUTER_API_KEY from the process env, else from python_server/.env (read per call, so a new key needs no restart).

    Only this one line is read: the rest of that file stays out of the backend.
    """
    key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if key:
        return key
    try:
        with open(SERVER_ENV, encoding="utf-8") as f:
            for line in f:
                name, sep, value = line.strip().partition("=")
                if sep and name.strip().replace("export ", "", 1).strip() == "OPENROUTER_API_KEY":
                    key = value.split(" #", 1)[0].strip().strip("\"'")
    except OSError:
        return ""
    return key


def ask_jev(jpeg: bytes) -> tuple:
    """(OpenRouter's answer byte for byte, its usage.cost). Raises JevUnavailable for anything but a 200 with JSON."""
    key = openrouter_key()
    if not key:
        raise JevUnavailable("OPENROUTER_API_KEY is not set (env or python_server/.env)")
    payload = {"model": MODEL, "state": {"description": DESCRIPTION, "camera_image": "cam"},
               "assets": {"cam": {"type": "image", "format": "jpg", "data_base64": base64.b64encode(jpeg).decode()}},
               "questions": QUESTIONS}
    try:
        r = requests.post(URL, json=payload, headers={"Authorization": f"Bearer {key}"}, timeout=JEV_TIMEOUT)
    except requests.exceptions.RequestException as exc:
        raise JevUnavailable(f"OpenRouter unreachable: {type(exc).__name__}") from exc
    if r.status_code != 200:
        # Never echo this to the Branch: a 401/402 here is about our key or credit.
        raise JevUnavailable(f"OpenRouter {r.status_code}: {r.text[:300]}")
    try:
        answer = json.loads(r.content)
    except ValueError as exc:
        raise JevUnavailable("OpenRouter answered 200 without JSON") from exc
    usage = answer.get("usage") if isinstance(answer, dict) else None
    return r.content, (usage or {}).get("cost")


def _judge(code: str, body) -> bytes:
    t = time.monotonic()
    camera = body.get("camera") if isinstance(body, dict) else None
    status, cost = "error", None
    try:
        jpeg, camera = parse(body)
        answer, cost = ask_jev(fit(jpeg))
        status = "ok"
        return answer
    except BadPicture:
        status = "bad_picture"
        raise
    except JevUnavailable as exc:
        status = "jev_unavailable"
        logger.error("camera-placement: %s", exc)
        raise
    finally:
        # One line per call: what a Branch asked and what it cost. Never the picture.
        logger.info("camera-placement branch=%s camera=%s status=%s cost=%s latency=%.2fs",
                    code, str(camera or "")[:64], status, cost, time.monotonic() - t)


@app.post("/judge")
async def judge(request: Request, x_branch_code: str = Header(default="")):
    code = x_branch_code.strip()
    if not code:
        return JSONResponse(status_code=400, content={"error": "missing X-Branch-Code"})
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "body is not JSON"})
    try:
        # In a worker thread: resizing and the OpenRouter call block.
        answer = await run_in_threadpool(_judge, code, body)
    except BadPicture as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    except JevUnavailable:
        return JSONResponse(status_code=503, content={"error": "jev_unavailable"})
    return Response(content=answer, media_type="application/json")
