from __future__ import annotations

import threading

from fastapi import APIRouter, File, Form, UploadFile

from data_service.core.exceptions import ValidationError
from data_service.core.services import dummy_data_service
from data_service.core.services.pipeline_service import (
    PIPELINE_PREPROCESS_ON_UPLOAD,
    preprocess_and_forward,
)

router = APIRouter(prefix="/dummy-data", tags=["dummy-data"])


def _preprocess_async(store_id: str) -> None:
    """Run preprocess + forward off the request thread; failures are logged."""
    try:
        result = preprocess_and_forward(store_id)
        import logging
        logging.getLogger(__name__).info(
            "store %s: preprocessed=%d forwarded=%d",
            store_id, result.get("preprocessed", 0), result.get("forwarded", 0),
        )
    except Exception:
        import logging
        logging.getLogger(__name__).exception("store %s: preprocess failed", store_id)


@router.post("/upload")
def upload_pipeline_zip(
    store_id: str = Form(...),
    file: UploadFile = File(...),
):
    saved = dummy_data_service.save_pipeline_zip(
        store_id=store_id,
        original_filename=file.filename or "",
        file_obj=file.file,
    )
    if PIPELINE_PREPROCESS_ON_UPLOAD:
        threading.Thread(target=_preprocess_async, args=(store_id,), daemon=True).start()
    return saved


@router.post("/{store_id}/preprocess")
def preprocess_store(store_id: str):
    """Manually preprocess (and forward) the latest snapshot for a store."""
    if not store_id:
        raise ValidationError("store_id is required", "store_id")
    result = preprocess_and_forward(store_id)
    return result