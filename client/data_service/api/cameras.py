from typing import List
import threading
import time
from threading import Thread
import asyncio
import hashlib
import logging

from fastapi import APIRouter, HTTPException, Request, Response, BackgroundTasks, Query

logger = logging.getLogger(__name__)

from data_service.schemas.camera_schemas import (
    CameraConfigCreate,
    CameraConfigUpdate,
    CameraConfigOutput,
    CameraChannel,
    CameraChannelOutput,
    CameraSelectInput,
    CameraPlacement,
    CameraPlacementInput,
)
from data_service.db.crud.camera_crud import (
    create_camera_config,
    read_camera_configs,
    read_camera_config_by_ip,
    read_camera_config_by_id,
    delete_camera_config_by_id,
    update_camera_config_by_id,
    read_username_password_by_ip,
    read_scan_status_by_ip,
    read_camera_config_with_password,
    read_camera_channels,
    read_camera_channels_by_ip,
    read_channel_count_by_ip,
    highest_selected_channel,
    ensure_camera_channels,
    update_camera_channel_status,
)
from data_service.utils import snapshot_cache
from data_service.utils.search_camera_utils import (
    validate_username_password_for_camera,
    scan_camera_channels,
    scan_camera_channels_ffprobe,
    ping_ip,
    get_image_from_camera_digest_auth
)
from data_service.utils.data_api_utils import get_active_cameras
from data_service.configs.client_api_config import TRIGGER_APIS
import requests
from data_service.db.crud.camera_placement_crud import read_camera_placements, set_user_placement

router = APIRouter()


def _push_camera_config_to_all(rtsp_dict):
    try:
        resp = requests.post(TRIGGER_APIS["update_camera"], json=rtsp_dict, timeout=5)
        logger.info("Pushed capture config to camera_service: %s", resp.text)
    except Exception as e:
        logger.error("Failed to push capture config to camera_service: %s", e)


# ----------- Camera Configuration Endpoints -----------
@router.post("/configs", response_model=CameraConfigOutput | dict)
def api_create_camera_config(camera_data: CameraConfigCreate):
    """
    Create a new camera authentication configuration.
    Scan the camera channels and save the working channels to the database.
    """
    if not camera_data.ip or not camera_data.username or not camera_data.password:
        raise HTTPException(status_code=400, detail="ต้องระบุ IP, ชื่อผู้ใช้ และรหัสผ่าน")

    if not ping_ip(camera_data.ip):
        raise HTTPException(status_code=400, detail="ไม่สามารถเข้าถึง IP ของกล้องได้ กรุณาตรวจสอบ IP อีกครั้ง")

    if not validate_username_password_for_camera(camera_data.ip, camera_data.username, camera_data.password):
        raise HTTPException(status_code=400, detail="ชื่อผู้ใช้หรือรหัสผ่านของกล้องไม่ถูกต้อง")

    try:
        # Check if another camera_master row already has this IP (channels already scanned)
        existing = read_camera_config_by_ip(camera_data.ip)
        ip_already_scanned = existing is not None and read_scan_status_by_ip(camera_data.ip)
        # The channel count is the NVR's, shared by every network on this IP.
        if existing is not None and camera_data.channel_count < highest_selected_channel(camera_data.ip):
            raise HTTPException(status_code=400, detail=(
                f"ช่อง {highest_selected_channel(camera_data.ip)} ของ NVR นี้กำลังถูกใช้งานอยู่ "
                f"เลือกจำนวนช่องน้อยกว่านั้นไม่ได้"))

        result = create_camera_config(camera_data)
        if "message" in result and "already exists" in result["message"]:
            raise HTTPException(status_code=400, detail="มีกล้องที่ใช้ IP และชื่อนี้แล้ว")

        if result.get("ip") == camera_data.ip:
            ensure_camera_channels(camera_data.ip, range(1, camera_data.channel_count + 1))

        if result.get("ip") == camera_data.ip and not ip_already_scanned:
            logger.info(f"Starting camera channel scan for {camera_data.ip}")
            _start_channel_scan(camera_data.ip, camera_data.username, camera_data.password)
        elif ip_already_scanned:
            # Channels already discovered for this NVR; mark as scanned immediately
            from data_service.db.crud.camera_crud import update_scan_status
            update_scan_status(camera_data.ip, True)
            logger.info(f"IP {camera_data.ip} already scanned, skipping channel scan for {camera_data.name}")

        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating camera authentication configuration: {e}")
        raise HTTPException(status_code=500, detail=f"Error creating camera authentication configuration: {e}")

@router.get("/configs", response_model=List[CameraConfigOutput])
def api_read_camera_configs():
    """
    Read all camera authentication configurations.
    """
    try:
        return read_camera_configs()
    except Exception as e:
        logger.error(f"Error reading camera authentication configurations: {e}")
        raise HTTPException(status_code=500, detail=f"Error reading camera authentication configurations: {e}")

@router.get("/configs/{camera_id}", response_model=CameraConfigOutput | dict)
def api_read_camera_config_by_id(camera_id: int):
    """
    Read a camera authentication configuration by ID.
    """
    try:
        return read_camera_config_by_id(camera_id)
    except Exception as e:
        logger.error(f"Error reading camera authentication configuration by ID: {e}")
        raise HTTPException(status_code=500, detail=f"Error reading camera authentication configuration by ID: {e}")

@router.delete("/configs/{camera_id}")
def api_delete_camera_config_by_id(camera_id: int, bg: BackgroundTasks):
    """
    Delete a camera authentication configuration by ID.
    """
    try:
        result = delete_camera_config_by_id(camera_id)

        rtsp_dict = get_active_cameras()
        bg.add_task(_push_camera_config_to_all, rtsp_dict)

        return result
    except Exception as e:
        logger.error(f"Error deleting camera authentication configuration: {e}")
        raise HTTPException(status_code=500, detail=f"Error deleting camera authentication configuration: {e}")

@router.put("/configs/{camera_id}", response_model=CameraConfigOutput | dict)
def api_update_camera_config_by_id(camera_id: int, camera_data: CameraConfigUpdate, bg: BackgroundTasks):
    """
    Edit a network: name, IP address, username and password.

    An empty username or password keeps the stored one. Whenever the IP or the
    credentials change they are checked against the NVR first, exactly as when
    a network is created, so a typo cannot silently cut every camera off.
    """
    current = read_camera_config_with_password(camera_id)
    if current is None:
        raise HTTPException(status_code=404, detail="ไม่พบเครือข่ายนี้")

    name = (camera_data.name or "").strip()
    ip = (camera_data.ip or "").strip()
    username = camera_data.username if camera_data.username else current["username"]
    password = camera_data.password if camera_data.password else current["password"]
    if not name or not ip:
        raise HTTPException(status_code=400, detail="ต้องระบุชื่อเครือข่ายและ IP")

    nvr_changed = ip != current["ip"] or username != current["username"] or password != current["password"]
    if nvr_changed:
        if not ping_ip(ip):
            raise HTTPException(status_code=400, detail="ไม่สามารถเข้าถึง IP ของกล้องได้ กรุณาตรวจสอบ IP อีกครั้ง")
        if not validate_username_password_for_camera(ip, username, password):
            raise HTTPException(status_code=400, detail="ชื่อผู้ใช้หรือรหัสผ่านของกล้องไม่ถูกต้อง")

    new_count = camera_data.channel_count
    if new_count is not None and new_count < current["channel_count"]:
        in_use = highest_selected_channel(current["ip"])
        if in_use > new_count:
            raise HTTPException(status_code=400, detail=(
                f"ลดเหลือ {new_count} ช่องไม่ได้ เพราะช่อง {in_use} กำลังถูกใช้งานอยู่ "
                f"— ยกเลิกการเลือกช่องนั้นใน Camera Selection ก่อน"))

    try:
        result = update_camera_config_by_id(camera_id, name, ip, username, password, new_count)
    except Exception as e:
        logger.error(f"Error updating camera authentication configuration: {e}")
        raise HTTPException(status_code=500, detail=f"Error updating camera authentication configuration: {e}")
    if "already exists" in result.get("message", ""):
        raise HTTPException(status_code=400, detail="มีกล้องที่ใช้ IP และชื่อนี้แล้ว")
    if "message" in result:
        raise HTTPException(status_code=404, detail="ไม่พบเครือข่ายนี้")

    try:
        ensure_camera_channels(ip, range(1, result["channel_count"] + 1))   # a larger count shows its new tiles at once
    except Exception as e:
        logger.warning(f"[camera scan] could not list {ip}'s channels: {e}")

    if result.pop("needs_scan", False):
        logger.info(f"Starting camera channel scan for {ip} (network {camera_id} edited)")
        _start_channel_scan(ip, username, password)

    # Pictures taken with the old address or login are not this NVR's any more.
    if nvr_changed:
        snapshot_cache.forget_ip(current["ip"])
        snapshot_cache.forget_ip(ip)

    # The services hold the RTSP URLs (IP + credentials) of the selected cameras.
    bg.add_task(_push_camera_config_to_all, get_active_cameras())
    return result



# ----------- Camera Channel Endpoints -----------
@router.get("/channels", response_model=List[CameraChannel])
def api_read_camera_channels(status: int | None = None):
    """
    Get all camera channels.
    """
    try:
        return read_camera_channels(status)
    except Exception as e:
        logger.error(f"Error fetching camera channels: {e}")
        raise HTTPException(status_code=500, detail=f"Error fetching camera channels: {e}")

_SCAN_RETRY_S = 60
_scan_lock = threading.Lock()
_scans_running: set[str] = set()
_scan_started_at: dict[str, float] = {}


def _start_channel_scan(ip: str, username: str, password: str) -> bool:
    """Scan an NVR's channels in the background, never two at once per IP."""
    with _scan_lock:
        if ip in _scans_running:
            return False
        _scans_running.add(ip)
        _scan_started_at[ip] = time.time()

    def run():
        try:
            asyncio.run(scan_camera_channels_ffprobe(ip, username, password))
        except Exception as e:
            logger.error(f"[camera scan] {ip} failed: {e}")
        finally:
            with _scan_lock:
                _scans_running.discard(ip)

    Thread(target=run, daemon=True).start()
    return True


@router.get("/channels/{ip}", response_model=CameraChannelOutput)
def api_read_camera_channels_by_ip(ip: str):
    """
    Get camera channels by IP.

    A scan only ever ran when the network was created, so one that found no
    channel or was cut off by an app restart left the page on "Scanning..."
    for good. While someone is waiting on that, scan again — at most once a
    minute per NVR, and never while a scan of it is still running.
    """
    try:
        scan_status = read_scan_status_by_ip(ip)

        if not scan_status:
            with _scan_lock:
                due = ip not in _scans_running and time.time() - _scan_started_at.get(ip, 0) > _SCAN_RETRY_S
            if due:
                username, password = read_username_password_by_ip(ip)
                if username is not None and _start_channel_scan(ip, username, password):
                    logger.info(f"[camera scan] {ip} still not scanned; scanning again")
            return {"status": "pending", "data": []}
        
        # Channels 1..N, N chosen on Create network. A channel with no camera is still
        # a tile (so the grid is always N tiles); each carries its picture's state,
        # which says why it has none. One in use above N stays listed, or it could
        # not be unselected.
        count = read_channel_count_by_ip(ip)
        ensure_camera_channels(ip, range(1, count + 1))
        rows = read_camera_channels_by_ip(ip)
        data = [c for c in rows if c["channel"] <= count or (c.get("status") == 1 and c.get("network_name"))]
        data.sort(key=lambda c: c["channel"])
        for c in data:
            c["snapshot_state"], c["snapshot_last_try"] = snapshot_cache.status(ip, c["channel"])
            c["snapshot_gave_up"] = snapshot_cache.gave_up(ip, c["channel"])
            c["snapshot_note"] = snapshot_cache.note(ip, c["channel"])
        return {"status": "success", "data": data}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching camera channels by IP: {e}")
        raise HTTPException(status_code=500, detail=f"Error fetching camera channels by IP: {e}")

@router.patch("/channels/selection")
def api_update_camera_channel_selection(selected_camera_list: List[CameraSelectInput], bg: BackgroundTasks):
    try:
        if len(selected_camera_list) > 8:
            raise HTTPException(status_code=400, detail="Cannot select more than 8 cameras")
        result = update_camera_channel_status(selected_camera_list)

        rtsp_dict = get_active_cameras()
        bg.add_task(_push_camera_config_to_all, rtsp_dict)

        logger.info("Active camera selection published successfully")
        return result
    except Exception as e:
        logger.error(f"Error updating camera channel status: {e}")
        raise HTTPException(status_code=500, detail=f"Error updating camera channel status: {e}")



# ----------- Camera Placement (inside / outside the shop) -----------
@router.get("/placement", response_model=List[CameraPlacement])
def api_read_camera_placements(ip: str | None = None):
    """Every channel the server has judged or a person has set; a channel not listed is not judged yet."""
    try:
        return read_camera_placements(ip)
    except Exception as e:
        logger.error(f"Error reading camera placements: {e}")
        raise HTTPException(status_code=500, detail=f"Error reading camera placements: {e}")


@router.put("/placement/{ip}/{channel}", response_model=CameraPlacement)
def api_set_camera_placement(ip: str, channel: int, body: CameraPlacementInput):
    """A person sets inside or outside; it stays, whatever the server said or says later."""
    try:
        return set_user_placement(ip, channel, body.placement)
    except Exception as e:
        logger.error(f"Error saving camera placement: {e}")
        raise HTTPException(status_code=500, detail=f"Error saving camera placement: {e}")


@router.get("/outside-area")
def api_read_camera_outside_areas(ip: str | None = None):
    """Each channel's part of the picture outside the shop (polygons, 0..1) and whether it is
    waiting to be asked again after a flipped switch. A channel not listed is not asked yet."""
    from data_service.db.crud.camera_outside_area_crud import read_outside_areas
    try:
        return read_outside_areas(ip)
    except Exception as e:
        logger.error(f"Error reading camera outside areas: {type(e).__name__}")
        raise HTTPException(status_code=500, detail="Error reading camera outside areas")


@router.post("/image/{ip}/{channel}/retry")
def api_retry_camera_image(ip: str, channel: int):
    """Camera Selection's Retry button: try this channel's picture now."""
    state, last_try = snapshot_cache.retry_now(ip, channel)
    return {"snapshot_state": state, "snapshot_last_try": last_try,
            "snapshot_gave_up": snapshot_cache.gave_up(ip, channel),
            "snapshot_note": snapshot_cache.note(ip, channel)}


@router.post("/image/{ip}/{channel}/retake")
def api_retake_camera_image(ip: str, channel: int):
    """Retake picture (Camera Selection, Calibrate): a new kept picture now; the old one stays on failure."""
    if not snapshot_cache.retake(ip, channel):
        raise HTTPException(status_code=502, detail="Could not get a new picture from this camera")
    return {"snapshot_state": "ok"}


# --------------- Camera Image Endpoint -----------
@router.get("/image/{ip}/{channel}", response_model=bytes)
def api_get_camera_image(
    request: Request,
    ip: str,
    channel: int,
    cached: bool = Query(False, description="Return the picture kept since app start (Camera Selection)"),
):
    """Get the image from a specific camera channel."""
    try:
        if cached:
            # Kept since app start by the background prefetch (snapshot_cache);
            # re-captured only by Retake picture. Missing: 404 at once — the page shows "No signal"
            # and asks again later while the prefetch keeps trying. Not fetched
            # here, or opening the page would hit the NVR for every dead camera.
            image_bytes = snapshot_cache.get(ip, channel)
            if not image_bytes:
                raise HTTPException(status_code=404, detail="Image not ready yet")
            # no-cache + ETag: the browser asks every time (a 304 when unchanged),
            # so after a Retake a page reload shows the new picture, not its old copy.
            etag = '"' + hashlib.sha1(image_bytes).hexdigest() + '"'
            headers = {"Cache-Control": "private, no-cache", "ETag": etag}
            if request.headers.get("if-none-match") == etag:
                return Response(status_code=304, headers=headers)
            return Response(content=image_bytes, media_type="image/jpeg", headers=headers)

        username, password = read_username_password_by_ip(ip)
        image_bytes = get_image_from_camera_digest_auth(ip, channel, username, password)
        if not image_bytes:
            # snapshot.cgi often never answers on Dahua NVRs; one RTSP frame does.
            image_bytes = snapshot_cache.grab_rtsp_frame(ip, channel, username, password)
            if image_bytes:
                snapshot_cache.put(ip, channel, image_bytes)
        if not image_bytes:
            raise HTTPException(status_code=404, detail="Image not found")

        return Response(content=image_bytes, media_type="image/jpeg")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching camera image: {e}")
        raise HTTPException(status_code=500, detail=f"Error fetching camera image: {e}")