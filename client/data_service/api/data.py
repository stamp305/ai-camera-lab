from fastapi import APIRouter

from data_service.utils.data_api_utils import get_active_cameras, get_inside_cameras

router = APIRouter()



# ---------- API Endpoints ----------
@router.get("/active-cameras-config")
async def get_active_cameras_config_endpoint():
    """
    Get the list of active cameras.
    """
    return get_active_cameras()


@router.get("/inside-cameras-config")
async def get_inside_cameras_config_endpoint():
    """
    Every camera judged inside the store, chosen or not (camera_service's after-hours watch).
    """
    return get_inside_cameras()
