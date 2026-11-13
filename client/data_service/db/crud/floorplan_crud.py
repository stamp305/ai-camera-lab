import json
import time
from typing import List, Optional

from data_service.db.session import get_db
from data_service.schemas.floorplan_schemas import (
    CameraCalibrationInput,
    CameraCalibrationOutput,
    FloorplanInput,
    FloorplanWithCamerasOutput,
)

# ============================================================
# Helpers
# ============================================================

def _load_json(raw, default=None):
    """Safely load JSON from MariaDB.

    MariaDB connector may return JSON data as: str, list, dict, or None.
    """
    if default is None:
        default = []

    if raw is None:
        return default

    if isinstance(raw, (list, dict)):
        return raw

    if isinstance(raw, str):
        if not raw.strip():
            return default
        try:
            return json.loads(raw)
        except Exception:
            return default

    return raw


def _load_matrix(raw) -> Optional[list]:
    """Load homography matrix from DB."""
    if raw is None:
        return None
    try:
        return _load_json(raw, None)
    except Exception:
        return None


def _load_points(raw) -> list:
    """Load src_points / dst_points / roi_zones."""
    return _load_json(raw, [])


def _dict_cursor(conn):
    """MariaDB dictionary cursor."""
    return conn.cursor(dictionary=True)


def _get_cam_name(row) -> str:
    """Get camera name safely."""
    try:
        if isinstance(row, dict):
            if row.get("camera_name"):
                return str(row["camera_name"])
            if row.get("id") is not None:
                return f"Camera {row['id']}"
        return "Camera"
    except Exception:
        return "Camera"


def _get_col(row, col_name: str, default=None):
    """Safely get a column from MariaDB dictionary row."""
    try:
        if isinstance(row, dict):
            value = row.get(col_name)
            if value is not None:
                return value
        return default
    except Exception:
        return default


def _build_camera_output(c) -> CameraCalibrationOutput:
    """Shared builder so all read paths produce identical output shape."""
    return CameraCalibrationOutput(
        id=c["id"],
        floorplan_id=c["floorplan_id"],
        camera_ip=_get_col(c, "camera_ip"),
        camera_channel=_get_col(c, "camera_channel"),
        camera_name=_get_cam_name(c),
        src_points=_load_points(c.get("src_points")),
        dst_points=_load_points(c.get("dst_points")),
        matrix=_load_matrix(c.get("matrix")),
        roi_zones=_load_points(c.get("roi_zones")),
        hotzone_floor=_load_points(c.get("hotzone_floor")),
    )


# ============================================================
# Floorplan
# ============================================================


def create_floorplan(floorplan_data: FloorplanInput) -> int:
    conn = None
    cursor = None
    try:
        conn = get_db()
        cursor = conn.cursor()

        cursor.execute(
            "INSERT INTO floorplan (name, description, image_url, image_updated_at, floorplan_group) "
            "VALUES (%s, %s, %s, %s, %s)",
            (
                floorplan_data.name,
                floorplan_data.description or "",
                floorplan_data.image_url or "",
                time.time(),
                floorplan_data.floorplan_group or 1,
            ),
        )
        conn.commit()

        floorplan_id = cursor.lastrowid
        print(f"✅ Floorplan created successfully: {floorplan_data.name}")
        return floorplan_id

    except Exception as e:
        if conn:
            conn.rollback()
        print(f"❌ DB error in create_floorplan: {e}")
        raise

    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def read_floorplans() -> List[FloorplanWithCamerasOutput]:
    conn = None
    cursor = None
    try:
        conn = get_db()
        cursor = _dict_cursor(conn)

        cursor.execute(
            """
            SELECT id, name, description, image_url, floorplan_group, created_at, updated_at
            FROM floorplan
            ORDER BY id
            """
        )
        floorplan_data = cursor.fetchall()
        if not floorplan_data:
            return []

        cursor.execute(
            """
            SELECT id, floorplan_id, camera_ip, camera_channel,
                   src_points, dst_points, matrix, roi_zones, hotzone_floor
            FROM camera_calibration
            """
        )
        camera_data = cursor.fetchall()

        payload = []
        for row in floorplan_data:
            cams = [c for c in camera_data if c["floorplan_id"] == row["id"]]
            is_calib = any(_load_matrix(c.get("matrix")) is not None for c in cams)
            payload.append(
                FloorplanWithCamerasOutput(
                    id=row["id"],
                    name=row["name"],
                    description=row.get("description") or "",
                    image_url=row["image_url"],
                    floorplan_group=row.get("floorplan_group") or 1,
                    camera_count=len(cams),
                    is_calibrated=is_calib,
                    cameras=[_build_camera_output(c) for c in cams],
                )
            )
        return payload

    except Exception as e:
        print(f"❌ DB error in read_floorplans: {e}")
        raise

    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def read_floorplan_by_id(floorplan_id: int) -> FloorplanWithCamerasOutput | None:
    conn = None
    cursor = None
    try:
        conn = get_db()
        cursor = _dict_cursor(conn)

        cursor.execute(
            """
            SELECT id, name, description, image_url, floorplan_group, created_at, updated_at
            FROM floorplan
            WHERE id = %s
            """,
            (floorplan_id,),
        )
        row = cursor.fetchone()
        if not row:
            print(f"⚠️ No floorplan found with ID {floorplan_id}.")
            return None

        cursor.execute(
            """
            SELECT id, floorplan_id, camera_ip, camera_channel,
                   src_points, dst_points, matrix, roi_zones, hotzone_floor
            FROM camera_calibration
            WHERE floorplan_id = %s
            """,
            (floorplan_id,),
        )
        cams = cursor.fetchall()

        is_calib = any(_load_matrix(c.get("matrix")) is not None for c in cams)
        return FloorplanWithCamerasOutput(
            id=row["id"],
            name=row["name"],
            description=row.get("description") or "",
            image_url=row["image_url"],
            floorplan_group=row.get("floorplan_group") or 1,
            camera_count=len(cams),
            is_calibrated=is_calib,
            cameras=[_build_camera_output(c) for c in cams],
        )

    except Exception as e:
        print(f"❌ DB error in read_floorplan_by_id: {e}")
        raise

    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def update_floorplan(floorplan_id: int, floorplan_data: FloorplanInput) -> Optional[str]:
    """Update a floorplan; returns the image_url no longer referenced, if any.

    A new picture moves the current one to previous_image_url (it is kept, data
    recorded on it still needs it) and stamps image_updated_at. The picture that
    was previous before drops out and is returned so its file can be deleted. A
    name/group edit changes neither.
    """
    conn = None
    cursor = None
    try:
        conn = get_db()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT image_url, previous_image_url FROM floorplan WHERE id = %s",
            (floorplan_id,),
        )
        row = cursor.fetchone()
        old_image_url, old_previous = (row[0], row[1]) if row else (None, None)
        image_changed = bool(floorplan_data.image_url) and floorplan_data.image_url != old_image_url

        if image_changed:
            cursor.execute(
                """
                UPDATE floorplan
                SET name = %s, description = %s, image_url = %s,
                    previous_image_url = %s, image_updated_at = %s,
                    floorplan_group = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (
                    floorplan_data.name,
                    floorplan_data.description or "",
                    floorplan_data.image_url,
                    old_image_url or None,
                    time.time(),
                    floorplan_data.floorplan_group or 1,
                    floorplan_id,
                ),
            )
        else:
            cursor.execute(
                """
                UPDATE floorplan
                SET name = %s, description = %s, image_url = %s, floorplan_group = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (
                    floorplan_data.name,
                    floorplan_data.description or "",
                    floorplan_data.image_url,
                    floorplan_data.floorplan_group or 1,
                    floorplan_id,
                ),
            )
        conn.commit()
        print(f"✅ Floorplan with ID {floorplan_id} updated successfully.")
        return old_previous if image_changed else None

    except Exception as e:
        if conn:
            conn.rollback()
        print(f"❌ DB error in update_floorplan: {e}")
        raise

    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def delete_floorplan(floorplan_id: int) -> Optional[str]:
    """Delete a floorplan and its calibrations; returns its previous_image_url so
    the caller can delete that file too."""
    conn = None
    cursor = None
    try:
        conn = get_db()
        cursor = conn.cursor()

        cursor.execute("SELECT previous_image_url FROM floorplan WHERE id = %s", (floorplan_id,))
        row = cursor.fetchone()
        previous_image_url = row[0] if row else None

        # Delete calibration first because of FK relationship
        cursor.execute(
            "DELETE FROM camera_calibration WHERE floorplan_id = %s",
            (floorplan_id,),
        )
        cursor.execute("DELETE FROM floorplan WHERE id = %s", (floorplan_id,))
        conn.commit()
        print(f"✅ Floorplan with ID {floorplan_id} deleted successfully.")
        return previous_image_url

    except Exception as e:
        if conn:
            conn.rollback()
        print(f"❌ DB error in delete_floorplan: {e}")
        raise

    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


# ============================================================
# Camera Calibration
# ============================================================


def create_camera_calibration(cam_data: CameraCalibrationInput) -> int:
    conn = None
    cursor = None
    try:
        conn = get_db()
        cursor = conn.cursor()

        roi_data = getattr(cam_data, "roi_zones", getattr(cam_data, "roi", []))

        cursor.execute(
            """
            INSERT INTO camera_calibration (
                floorplan_id, camera_ip, camera_channel,
                src_points, dst_points, matrix, roi_zones
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                src_points = VALUES(src_points),
                dst_points = VALUES(dst_points),
                roi_zones = VALUES(roi_zones)
            """,
            (
                cam_data.floorplan_id,
                cam_data.camera_ip,
                cam_data.camera_channel,
                json.dumps(cam_data.src_points),
                json.dumps(cam_data.dst_points),
                None,
                json.dumps(roi_data),
            ),
        )
        conn.commit()

        # On duplicate-key update, lastrowid can be 0 depending on driver —
        # fetch the actual calibration ID explicitly.
        cursor.execute(
            """
            SELECT id FROM camera_calibration
            WHERE floorplan_id = %s AND camera_ip = %s AND camera_channel = %s
            """,
            (cam_data.floorplan_id, cam_data.camera_ip, cam_data.camera_channel),
        )
        row = cursor.fetchone()
        calibration_id = row[0] if row else cursor.lastrowid

        print(f"✅ Camera calibration saved: {calibration_id}")
        return calibration_id

    except Exception as e:
        if conn:
            conn.rollback()
        print(f"❌ DB error in create_camera_calibration: {e}")
        raise

    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def read_camera_calibration_by_id(calibration_id: int) -> CameraCalibrationOutput | None:
    conn = None
    cursor = None
    try:
        conn = get_db()
        cursor = _dict_cursor(conn)

        cursor.execute(
            """
            SELECT id, floorplan_id, camera_ip, camera_channel,
                   src_points, dst_points, matrix, roi_zones, hotzone_floor
            FROM camera_calibration
            WHERE id = %s
            """,
            (calibration_id,),
        )
        row = cursor.fetchone()
        if not row:
            return None

        return CameraCalibrationOutput(
            id=row["id"],
            floorplan_id=row["floorplan_id"],
            camera_ip=row["camera_ip"],
            camera_channel=row["camera_channel"],
            camera_name=f"Cam {row['camera_ip']}:{row['camera_channel']}",
            src_points=_load_points(row.get("src_points")),
            dst_points=_load_points(row.get("dst_points")),
            matrix=_load_matrix(row.get("matrix")),
            roi_zones=_load_points(row.get("roi_zones")),
            hotzone_floor=_load_points(row.get("hotzone_floor")),
        )

    except Exception as e:
        print(f"❌ DB error in read_camera_calibration_by_id: {e}")
        raise

    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def update_camera_calibration_points(
    calibration_id: int, cam_data: CameraCalibrationInput
) -> None:
    conn = None
    cursor = None
    try:
        conn = get_db()
        cursor = conn.cursor()

        roi_data = getattr(cam_data, "roi_zones", getattr(cam_data, "roi", []))

        cursor.execute(
            """
            UPDATE camera_calibration
            SET src_points = %s, dst_points = %s, roi_zones = %s
            WHERE id = %s
            """,
            (
                json.dumps(cam_data.src_points),
                json.dumps(cam_data.dst_points),
                json.dumps(roi_data),
                calibration_id,
            ),
        )
        conn.commit()
        print(
            f"✅ Camera calibration points & ROI with ID {calibration_id} "
            f"updated successfully."
        )

    except Exception as e:
        if conn:
            conn.rollback()
        print(f"❌ DB error in update_camera_calibration_points: {e}")
        raise

    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def update_camera_calibration_matrix(
    calibration_id: int, matrix: list, inverse_matrix: list = None
) -> None:
    conn = None
    cursor = None
    try:
        conn = get_db()
        cursor = conn.cursor()

        cursor.execute(
            "UPDATE camera_calibration SET matrix = %s WHERE id = %s",
            (json.dumps(matrix), calibration_id),
        )
        conn.commit()
        print(f"✅ Camera calibration matrix with ID {calibration_id} updated successfully.")

    except Exception as e:
        if conn:
            conn.rollback()
        print(f"❌ DB error in update_camera_calibration_matrix: {e}")
        raise

    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def update_camera_calibration_hotzone_floor(calibration_id: int, zones: list) -> bool:
    """Replace the hand-placed hot zones of one calibration; False when it does not exist."""
    conn = None
    cursor = None
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE camera_calibration SET hotzone_floor = %s WHERE id = %s",
            (json.dumps(zones), calibration_id),
        )
        conn.commit()
        if cursor.rowcount:
            return True
        # rowcount is 0 also when the value did not change; tell that from a missing row.
        cursor.execute("SELECT 1 FROM camera_calibration WHERE id = %s", (calibration_id,))
        return cursor.fetchone() is not None

    except Exception as e:
        if conn:
            conn.rollback()
        print(f"❌ DB error in update_camera_calibration_hotzone_floor: {type(e).__name__}")
        raise

    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def delete_camera_calibration(calibration_id: int) -> None:
    conn = None
    cursor = None
    try:
        conn = get_db()
        cursor = conn.cursor()

        cursor.execute(
            "DELETE FROM camera_calibration WHERE id = %s",
            (calibration_id,),
        )
        conn.commit()
        print(f"✅ Camera calibration with ID {calibration_id} deleted successfully.")

    except Exception as e:
        if conn:
            conn.rollback()
        print(f"❌ DB error in delete_camera_calibration: {e}")
        raise

    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()