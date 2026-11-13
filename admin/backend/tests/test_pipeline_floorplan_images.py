"""Floorplan images in a pipeline zip: only real PNG/JPEG files count.

Run (from ai-camera-admin/backend/):
    venv/bin/python -m pytest -q tests/test_pipeline_floorplan_images.py
"""
import io
import zipfile

from data_service.core.services import pipeline_service as ps

PNG = b"\x89PNG\r\n\x1a\n" + b"png"
JPEG = b"\xff\xd8\xff" + b"jpg"


def zip_of(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return zipfile.ZipFile(io.BytesIO(buf.getvalue()))


def test_only_png_and_jpeg_files_are_taken_as_floorplan_images():
    zf = zip_of({
        "floorplan/images/a.png": PNG,
        "floorplan/images/b.jpg": JPEG,
        "floorplan/images/.DS_Store": b"\0\0\0\1Bud1",
        "floorplan/images/notes.png": b"not an image",
        "__MACOSX/floorplan/images/._a.png": b"\0\5\26\7",
        "floorplan/calibration.json": b"{}",
    })
    assert ps._extract_images(zf) == {"a.png": PNG, "b.jpg": JPEG}
