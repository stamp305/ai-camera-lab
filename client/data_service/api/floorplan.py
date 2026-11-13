import json
from typing import List, Optional
from pydantic import BaseModel, Field
from fastapi import Query

from data_service.db.crud.floorplan_crud import (
    create_camera_calibration,
    create_floorplan,
    delete_camera_calibration,
    delete_floorplan,
    read_camera_calibration_by_id,
    read_floorplan_by_id,
    read_floorplans,
    update_camera_calibration_hotzone_floor,
    update_camera_calibration_matrix,
    update_camera_calibration_points,
    update_floorplan,
)
from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from data_service.schemas.floorplan_schemas import (
    CameraCalibrationInput,
    CameraCalibrationOutput,
    FloorplanInput,
    HotzoneFloorInput,
    FloorplanWithCamerasOutput,
)


class TransformResponse(BaseModel):
    x: float
    y: float
    success: bool
from data_service.utils.floorplan_utils import (
    delete_floorplan_image_from_static_by_name,
    save_floorplan_image_to_static,
)
from data_service.utils.perspective import (
    apply_mapping,
    apply_mapping_inverse,
    clear_mapping_cache,
    fit_mapping,
    get_or_fit_mapping,
    mapping_from_matrix,
)
import numpy as np
import threading

import httpx

import port_store

router = APIRouter(redirect_slashes=False)


def _reload_floorplan_service() -> None:
    """Tell floorplan_service to re-read the floorplans now, in the background.

    It otherwise only re-reads when its 30 s cache is stale and someone asks it
    for a position, so a picture that stays a short while (or one changed while
    no camera runs) was never seen and its data had no picture on the admin.
    Failure is harmless: the cache refresh still picks the change up later.
    """
    def _post():
        try:
            port = port_store.get_port("floorplan_service", 8013)
            httpx.post(f"http://{port_store.API_HOST}:{port}/reload", timeout=3.0)
        except Exception as exc:
            print(f"⚠️ floorplan_service reload failed: {exc}")
    threading.Thread(target=_post, daemon=True).start()


# ---------- Floorplan API Endpoints ----------
@router.post("")
@router.post("/")
def api_create_floorplan(
    name: str = Form(...),
    description: str = Form(""),
    floorplan_group: int = Form(1, alias="floorplanGroup"),
    image: UploadFile = File(...),
) -> dict:
    try:
        image_name = save_floorplan_image_to_static([image])[0]
        url = f"static/images/floorplans/{image_name}"
        floorplan_id = create_floorplan(
            FloorplanInput(name=name, description=description, image_url=url, floorplan_group=floorplan_group)
        )
        _reload_floorplan_service()

        return {"message": "Floorplan created successfully", "id": floorplan_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error creating floorplan: {str(e)}")

@router.get("")
@router.get("/", response_model=List[FloorplanWithCamerasOutput])
def api_read_floorplans():
    """
    Read all floorplan records, each with camera count and calibration status.
    """
    try:
        return read_floorplans()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error reading floorplans: {e}")


@router.get("/{floorplan_id}", response_model=FloorplanWithCamerasOutput)
def api_read_floorplan_by_id(floorplan_id: int):
    """
    Read a single floorplan record by ID, including its cameras.
    """
    try:
        result = read_floorplan_by_id(floorplan_id)
        if not result:
            raise HTTPException(status_code=404, detail=f"Floorplan with id {floorplan_id} not found")
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error reading floorplan: {e}")


@router.put("/{floorplan_id}")
def api_update_floorplan(
    floorplan_id: int,
    name: str = Form(...),
    description: str = Form(""),
    floorplan_group: int = Form(1, alias="floorplanGroup"),
    image: UploadFile | None = File(None),
):
    try:
        current = read_floorplan_by_id(floorplan_id)
        if not current:
            raise HTTPException(status_code=404, detail=f"Floorplan with id {floorplan_id} not found")

        new_image_url = current.image_url
        if image is not None:
            image_name = save_floorplan_image_to_static([image])[0]
            new_image_url = f"static/images/floorplans/{image_name}"

        # The current picture becomes previous_image_url and its file is kept; only
        # the picture that was previous before is no longer needed.
        dropped = update_floorplan(
            floorplan_id,
            FloorplanInput(name=name, description=description, image_url=new_image_url, floorplan_group=floorplan_group),
        )
        if dropped:
            delete_floorplan_image_from_static_by_name(dropped.split("/")[-1])
        _reload_floorplan_service()

        # คืนค่าเป็น imageUrl (camelCase) ตามที่ Frontend คาดหวัง
        return {"message": f"Floorplan with id {floorplan_id} updated successfully", "imageUrl": new_image_url}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error updating floorplan: {e}")


@router.delete("/{floorplan_id}")
def api_delete_floorplan(floorplan_id: int):
    """
    Delete a floorplan record by ID (cascades delete to its cameras) and remove its image file.
    """
    try:
        current = read_floorplan_by_id(floorplan_id)
        if not current:
            raise HTTPException(status_code=404, detail=f"Floorplan with id {floorplan_id} not found")

        # ลบ DB ก่อน เพื่อกันกรณีไฟล์ถูกลบแต่ DB ยังค้าง
        previous_image_url = delete_floorplan(floorplan_id)
        _reload_floorplan_service()

        # ลบไฟล์รูปหลังลบ DB สำเร็จ (รูปปัจจุบันและรูปก่อนหน้า) — fail แบบ silent ไม่ให้ API พัง
        for image_url in (current.image_url, previous_image_url):
            if not image_url:
                continue
            try:
                old_name = image_url.split("/")[-1]
                # กันกรณีเป็นค่าว่าง หรือไม่มีนามสกุล
                if old_name and "." in old_name:
                    delete_floorplan_image_from_static_by_name(old_name)
            except Exception as file_err:
                print(f"⚠️ Failed to delete floorplan image {image_url}: {file_err}")

        return {"message": f"Floorplan with id {floorplan_id} deleted successfully"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error deleting floorplan: {e}")

@router.get("/{floorplan_id}/cameras")
def api_get_camera_calibration_by_query(
    floorplan_id: int,
    ip: Optional[str] = Query(None),
    channel: Optional[int] = Query(None)
):
    """
    ค้นหา/ดึงข้อมูล Camera Calibration ของ Floorplan ตาม IP และ Channel
    """
    try:
        # 1. ดึง Floorplan พร้อมรายการกล้องทั้งหมดที่มี
        floorplan = read_floorplan_by_id(floorplan_id)
        if not floorplan:
            raise HTTPException(status_code=404, detail=f"Floorplan {floorplan_id} not found")

        # 2. ค้นหากล้องที่ตรงกับ ip หรือ channel (ถ้ามีการส่ง query params มา)
        cameras = floorplan.cameras or []
        for cam in cameras:
            match_ip = (ip is None or cam.camera_ip == ip)
            match_ch = (channel is None or str(cam.camera_channel) == str(channel))
            if match_ip and match_ch:
                return cam

        # หากไม่พบกล้องที่ตรงเงื่อนไข
        return None
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching camera calibration: {e}")
    
def _calib_ns(calibration_id: int) -> str:
    """Cache namespace for one saved calibration (see perspective module)."""
    return f"calib:{int(calibration_id)}"


def _clean_metrics(metrics: dict) -> dict:
    """Make fit metrics JSON-safe (NaN/inf -> None).

    per_point_error is already sanitized at fit time; only the summary
    floats can still be non-finite on degenerate inputs, and a bare NaN
    breaks strict JSON parsers on the frontend.
    """
    import math
    clean = dict(metrics or {})
    for k, v in list(clean.items()):
        if isinstance(v, float) and not math.isfinite(v):
            clean[k] = None
    return clean


def calculate_homography(src_points: list, dst_points: list) -> tuple:
    """
    Compute homography matrix and its inverse.
    Returns: (matrix, inverse_matrix) as lists

    H is fit with USAC_MAGSAC + least-squares refit on inliers (see
    data_service.utils.perspective). The inverse is fit directly on the
    swapped points, which is more stable than inverting H numerically —
    TPS-corrected mappings cannot be inverted in closed form at all.
    """
    if len(src_points) < 4 or len(dst_points) < 4:
        raise ValueError("Homography calculation requires at least 4 point pairs.")

    fwd = fit_mapping(src_points, dst_points, camera_lens=False)  # only its H is used
    if fwd is None:
        raise ValueError("Could not compute homography matrix with the provided points.")

    inv = fit_mapping(dst_points, src_points, camera_lens=False)
    if inv is not None:
        H_inv = inv.H.tolist()
    else:
        # Degenerate swap (e.g. dst collinear): fall back to numeric inverse.
        try:
            H_inv = np.linalg.inv(np.asarray(fwd.H, dtype=np.float64)).tolist()
        except Exception:
            raise ValueError("Could not compute homography matrix with the provided points.")

    return fwd.H.tolist(), H_inv


# ---------- Camera Calibration API Endpoints ----------
@router.post("/{floorplan_id}/cameras")
def api_create_camera_calibration(floorplan_id: int, cam_data: CameraCalibrationInput):
    """
    Create a new camera calibration record under a floorplan.
    """
    try:
        if cam_data.floorplan_id != floorplan_id:
            raise HTTPException(status_code=400, detail="floorplanId mismatch between path and body")
        id = create_camera_calibration(cam_data)
        _reload_floorplan_service()
        return {"message": "Camera calibration created successfully", "id": id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error creating camera calibration: {e}")


@router.get("/cameras/{calibration_id}", response_model=CameraCalibrationOutput)
def api_read_camera_calibration_by_id(calibration_id: int):
    """
    Read a single camera calibration record by ID.
    """
    try:
        result = read_camera_calibration_by_id(calibration_id)
        if not result:
            raise HTTPException(status_code=404, detail=f"Camera calibration with id {calibration_id} not found")
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error reading camera calibration: {e}")


@router.put("/cameras/{calibration_id}/points")
def api_update_camera_calibration_points(calibration_id: int, cam_data: CameraCalibrationInput):
    """
    Update src_points/dst_points/roi_zones for a camera calibration record.
    """
    try:
        update_camera_calibration_points(calibration_id, cam_data)
        clear_mapping_cache(_calib_ns(calibration_id))
        return {"message": f"Camera calibration points with id {calibration_id} updated successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error updating camera calibration points: {e}")


@router.put("/cameras/{calibration_id}/hotzone-floor")
def api_update_hotzone_floor(calibration_id: int, body: HotzoneFloorInput):
    """Replace where this camera's hot zones sit on the floor plan (Calibrate Hot Zone).

    Only zones placed by hand are sent; a zone left out is drawn where the
    calibration maps it from the camera.
    """
    ids = [z.zone_id for z in body.hotzone_floor]
    if len(ids) != len(set(ids)):
        raise HTTPException(status_code=400, detail="Each zone can be placed only once")
    try:
        zones = [z.model_dump() for z in body.hotzone_floor]
        if not update_camera_calibration_hotzone_floor(calibration_id, zones):
            raise HTTPException(status_code=404, detail=f"Camera calibration with id {calibration_id} not found")
        _reload_floorplan_service()
        return {"message": "Hot zones on the floor plan saved", "count": len(zones)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error saving hot zones on the floor plan: {type(e).__name__}")


@router.post("/cameras/{calibration_id}/calibrate")
def api_calibrate_camera(calibration_id: int):
    """
    Fit the full mapping (global H + TPS residual correction when the points
    support it) from the record's stored src_points/dst_points and persist
    the H matrix in the database. The TPS part is never stored — it is refit
    from the points on every transform (see perspective module).
    """
    try:
        cam = read_camera_calibration_by_id(calibration_id)
        if not cam:
            raise HTTPException(status_code=404, detail=f"Camera calibration with id {calibration_id} not found")

        if not cam.src_points or not cam.dst_points or len(cam.src_points) < 4 or len(cam.dst_points) < 4:
            raise HTTPException(
                status_code=400,
                detail="At least 4 pairs of src_points and dst_points are required to compute calibration."
            )

        ns = _calib_ns(calibration_id)
        mapping = get_or_fit_mapping(cam.src_points, cam.dst_points, namespace=ns)
        if mapping is None:
            raise ValueError("Could not compute homography matrix with the provided points.")
        matrix = mapping.H.tolist()
        inv_mapping = get_or_fit_mapping(cam.dst_points, cam.src_points, namespace=ns, camera_lens=False)
        if inv_mapping is not None:
            inverse_matrix = inv_mapping.H.tolist()
        else:
            inverse_matrix = np.linalg.inv(np.asarray(mapping.H, dtype=np.float64)).tolist()
        update_camera_calibration_matrix(calibration_id, matrix, inverse_matrix)
        _reload_floorplan_service()

        return {
            "message": f"Camera calibration with id {calibration_id} calculated and updated successfully",
            "matrix": matrix,
            "inverse_matrix": inverse_matrix,
            "use_tps": mapping.use_tps,
            "metrics": _clean_metrics(mapping.metrics),
        }
    except HTTPException:
        raise
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error calibrating camera: {e}")


def apply_perspective_transform(x: float, y: float, matrix_list: list) -> tuple:
    """
    Apply perspective transform to a point using the homography matrix.
    Returns: (x, y, success)

    Legacy H-only entry point (kept for backward compatibility).
    """
    from data_service.utils.perspective import apply_perspective
    return apply_perspective(x, y, matrix_list)


class TransformRequest(BaseModel):
    x: float
    y: float
    direction: str  # 'cam_to_floor' or 'floor_to_cam'


class TransformPreviewRequest(BaseModel):
    src_points: List[List[float]]
    dst_points: List[List[float]]
    x: float
    y: float
    direction: str  # 'cam_to_floor' or 'floor_to_cam'


class FitPreviewRequest(BaseModel):
    src_points: List[List[float]]
    dst_points: List[List[float]]


class MapPointsRequest(BaseModel):
    src_points: List[List[float]]
    dst_points: List[List[float]]
    points: List[List[float]] = Field(max_length=1000)
    direction: str = "cam_to_floor"  # or 'floor_to_cam'


class MappedPoint(BaseModel):
    x: float
    y: float
    success: bool


@router.post("/map-points", response_model=List[MappedPoint])
def api_map_points(request: MapPointsRequest):
    """Map many points at once with the (unsaved) points given — Calibrate Hot Zone
    maps every corner of every hot zone to the floor plan with one request.
    Same model as transform-preview (lens + H + TPS when the points support it).
    """
    if len(request.src_points) < 4 or len(request.src_points) != len(request.dst_points):
        raise HTTPException(status_code=400, detail="At least 4 pairs of src_points and dst_points of the same length are required")
    if request.direction not in ("cam_to_floor", "floor_to_cam"):
        raise HTTPException(status_code=400, detail="direction must be 'cam_to_floor' or 'floor_to_cam'")
    if any(len(p) != 2 for p in request.points):
        raise HTTPException(status_code=400, detail="Each point must be [x, y]")
    try:
        mapping = get_or_fit_mapping(request.src_points, request.dst_points, namespace="preview")
        if mapping is None:
            raise HTTPException(status_code=400, detail="Could not compute mapping from the given points.")
        apply = apply_mapping if request.direction == "cam_to_floor" else apply_mapping_inverse
        out = []
        for x, y in request.points:
            mx, my, ok = apply(mapping, float(x), float(y))
            finite = ok and all(v == v and abs(v) != float("inf") for v in (mx, my))
            out.append(MappedPoint(x=mx if finite else 0.0, y=my if finite else 0.0, success=bool(finite)))
        return out
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error mapping points: {type(e).__name__}")


@router.post("/fit-preview")
def api_fit_preview(request: FitPreviewRequest):
    """Fit only (no save): H + quality metrics for live feedback while the
    user is still placing points. Mirrors cctv-reid POST /api/mapping/preview."""
    try:
        if not request.src_points or not request.dst_points or len(request.src_points) < 4 or len(request.dst_points) < 4:
            raise HTTPException(status_code=400, detail="At least 4 pairs of src_points and dst_points are required")
        if len(request.src_points) != len(request.dst_points):
            raise HTTPException(status_code=400, detail="src_points and dst_points must have same length")
        mapping = get_or_fit_mapping(request.src_points, request.dst_points,
                                      namespace="preview")
        if mapping is None:
            raise HTTPException(status_code=400, detail="Could not compute mapping from the given points.")
        return {"matrix": mapping.H.tolist(), "metrics": _clean_metrics(mapping.metrics)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fitting preview: {e}")


@router.post("/transform-preview", response_model=TransformResponse)
def api_transform_preview(request: TransformPreviewRequest):
    """
    Preview: แปลงพิกัดโดยใช้จุด src/dst ที่ส่งมาโดยยังไม่ต้อง save
    ใช้เพื่อ Test Mapping ใน Step 2 ก่อนกด Save.
    Uses the full mapping (lens + H + TPS when the points support it) so the
    preview matches what a saved calibration would compute.
    """
    try:
        if not request.src_points or not request.dst_points or len(request.src_points) < 4 or len(request.dst_points) < 4:
            raise HTTPException(status_code=400, detail="At least 4 pairs of src_points and dst_points are required for preview transform")
        if len(request.src_points) != len(request.dst_points):
            raise HTTPException(status_code=400, detail="src_points and dst_points must have same length")
        if request.direction not in ['cam_to_floor', 'floor_to_cam']:
            raise HTTPException(status_code=400, detail="direction must be 'cam_to_floor' or 'floor_to_cam'")
        mapping = get_or_fit_mapping(request.src_points, request.dst_points, namespace="preview")
        if mapping is None:
            raise HTTPException(status_code=400, detail="Transform failed with provided points")
        # floor_to_cam solves the camera -> floor model for the point: the
        # camera's lens stays on the camera's side, and both directions agree.
        apply = apply_mapping if request.direction == 'cam_to_floor' else apply_mapping_inverse
        x, y, success = apply(mapping, request.x, request.y)
        if not success:
            raise HTTPException(status_code=400, detail="Transform failed with provided points")
        return TransformResponse(x=x, y=y, success=success)
    except HTTPException:
        raise
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        import traceback
        print(f"ERROR transform-preview: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Error in transform preview: {e}")


@router.post("/cameras/{calibration_id}/transform", response_model=TransformResponse)
def api_transform_point(calibration_id: int, request: TransformRequest):
    """
    Transform a point using the saved calibration's full mapping (lens + H +
    TPS refit from the stored src/dst points; H-only for legacy rows).
    direction: 'cam_to_floor' | 'floor_to_cam'
    point: {x: float, y: float} (percent 0-100, same units as calibration)
    Returns: {x: float, y: float, success: bool}
    """
    try:
        cam = read_camera_calibration_by_id(calibration_id)
        if not cam:
            raise HTTPException(status_code=404, detail=f"Camera calibration with id {calibration_id} not found")

        print(f"DEBUG: cam.id = {cam.id}")
        print(f"DEBUG: cam.matrix = {cam.matrix}")
        print(f"DEBUG: request.direction = {request.direction}")
        print(f"DEBUG: request.x = {request.x}, request.y = {request.y}")

        if request.direction not in ['cam_to_floor', 'floor_to_cam']:
            raise HTTPException(status_code=400, detail="direction must be 'cam_to_floor' or 'floor_to_cam'")

        ns = _calib_ns(calibration_id)
        has_points = bool(cam.src_points) and bool(cam.dst_points)
        mapping = (get_or_fit_mapping(cam.src_points, cam.dst_points, namespace=ns)
                   if has_points else None)
        apply = apply_mapping
        if mapping is not None:
            # floor_to_cam solves the camera -> floor model for the point: the
            # camera's lens stays on the camera's side, and both directions agree.
            if request.direction == 'floor_to_cam':
                apply = apply_mapping_inverse
        elif request.direction == 'cam_to_floor':
            mapping = mapping_from_matrix(cam.matrix)
        elif cam.matrix is not None:
            # Legacy row without points: the numeric inverse of H (the old behavior).
            try:
                inv = np.linalg.inv(np.asarray(cam.matrix, dtype=np.float64)).tolist()
                mapping = mapping_from_matrix(inv)
            except Exception:
                mapping = None

        if mapping is None:
            raise HTTPException(status_code=400, detail="Camera not calibrated. Please calibrate first.")

        x, y, success = apply(mapping, request.x, request.y)

        print(f"DEBUG: result x={x}, y={y}, success={success}")

        return TransformResponse(x=x, y=y, success=success)
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        print(f"ERROR: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Error transforming point: {e}")


@router.get("/cameras/{calibration_id}/metrics")
def api_calibration_metrics(calibration_id: int):
    """Saved-calibration quality (CV errors, not train errors).

    404 when the record has no usable points or only a legacy matrix
    (mirrors cctv-reid GET /metrics/{channel_id}).
    """
    try:
        cam = read_camera_calibration_by_id(calibration_id)
        if not cam or not cam.src_points or not cam.dst_points:
            raise HTTPException(
                status_code=404,
                detail=f"Channel {calibration_id} has no stored points",
            )
        mapping = get_or_fit_mapping(
            cam.src_points, cam.dst_points, namespace=_calib_ns(calibration_id)
        )
        if mapping is None or not mapping.metrics.get("per_point_error"):
            raise HTTPException(
                status_code=404,
                detail=f"Channel {calibration_id} has a legacy matrix without stored points",
            )
        return _clean_metrics(mapping.metrics)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error reading calibration metrics: {e}")


@router.delete("/cameras/{calibration_id}")
def api_delete_camera_calibration(calibration_id: int):
    """
    Delete a camera calibration record by ID.
    """
    try:
        delete_camera_calibration(calibration_id)
        _reload_floorplan_service()
        clear_mapping_cache(_calib_ns(calibration_id))
        return {"message": f"Camera calibration with id {calibration_id} deleted successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error deleting camera calibration: {e}")