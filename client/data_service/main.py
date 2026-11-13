import os
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from data_service.api import (
    users,
    data,
    store,
    cameras,
	floorplan,
)
from data_service.utils.init_account_config import init_account_config
from data_service.utils.log_picture_cleanup import cleanup_old_log_images, INTERVAL_SECONDS


@asynccontextmanager
async def lifespan(app: FastAPI):
    # One picture per camera channel, gathered in the background from app start
    # so Camera Selection opens with every picture already there.
    from data_service.utils.snapshot_cache import start_prefetch
    start_prefetch()
    # Each channel with a picture and no inside/outside yet is judged by the admin server, once.
    from data_service.utils import camera_placement
    camera_placement.start()
    # ...and its part outside the shop drawn as polygons: once, then each time its switch is pressed.
    from data_service.utils import camera_outside_area
    camera_outside_area.start()
    task = asyncio.create_task(_periodic_cleanup())
    yield
    task.cancel()


async def _periodic_cleanup():
    while True:
        cleanup_old_log_images()
        await asyncio.sleep(INTERVAL_SECONDS)


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

init_account_config()

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
if not os.path.exists(STATIC_DIR):
    os.makedirs(STATIC_DIR)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

app.include_router(users.router, prefix="/users", tags=["users"])
app.include_router(store.router, prefix="/store", tags=["store"])
app.include_router(cameras.router, prefix="/cameras", tags=["cameras"])
app.include_router(data.router, prefix="/data", tags=["data"])
app.include_router(floorplan.router, prefix="/floorplan", tags=["floorplan"])

@app.get("/health", tags=["health"])
async def health_check():
    return {"status": "ok"}