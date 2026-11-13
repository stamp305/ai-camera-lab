from data_service.db.session import get_db
from data_service.schemas.camera_schemas import (
    CameraConfigCreate,
    CameraConfigUpdate,
    CameraConfigOutput,
    CameraChannelOutput,
    CameraSelectInput,
)
from data_service.configs.crypto_util import encrypt_password, decrypt_password
from typing import List

# ----------- Camera Authentication Functions -----------
def create_camera_config(camera_data: CameraConfigCreate) -> dict:
    """
    Inserts ip, name, username, and encrypted password into the camera_master table.
    """
    try:
        conn = get_db()
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM camera_master WHERE ip = ? AND name = ?", (camera_data.ip, camera_data.name))
        if cursor.fetchone():
            print(f"❌ Camera with IP {camera_data.ip} and name {camera_data.name} already exists.")
            return {"message": "Camera with this IP and name already exists."}

        cursor.execute(
            "INSERT INTO camera_master (ip, name, username, password, channel_count) VALUES (?, ?, ?, ?, ?)",
            (camera_data.ip, camera_data.name, camera_data.username, encrypt_password(camera_data.password),
             camera_data.channel_count)
        )
        # The channel count belongs to the NVR, like its login: every network on this IP shares it.
        cursor.execute("UPDATE camera_master SET channel_count = ? WHERE ip = ?",
                       (camera_data.channel_count, camera_data.ip))
        conn.commit()
        new_id = cursor.lastrowid
        print(f"✅ Camera data created successfully: {camera_data}")
        return {
            "id": new_id,
            "ip": camera_data.ip,
            "name": camera_data.name,
            "username": camera_data.username,
            "channel_count": camera_data.channel_count,
        }
    except Exception as e:
        print(f"❌ DB error in create_camera_config: {e}")
        raise
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

def read_camera_configs() -> List[CameraConfigOutput] | dict:
    """
    Read ip, name, username from camera_master table.
    If selected is True, only return cameras that are selected.
    """
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT id, ip, name, username, channel_count FROM camera_master")
        rows = cursor.fetchall()
        if not rows:
            print("⚠️ No camera data found.")
            return []
        print(f"✅ Camera data retrieved: {rows}")
        return rows
    except Exception as e:
        print(f"❌ DB error in read_camera_config: {e}")
        raise
    finally:
        if cursor:
            cursor.close()
        if conn:        
            conn.close()

def read_camera_config_by_ip(ip: str) -> CameraConfigOutput | dict:
    """
    Read a camera authentication configuration by IP.
    """
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT ip, name, username, channel_count FROM camera_master WHERE ip = ?", (ip,))
        row = cursor.fetchone()
        if not row:
            print(f"⚠️ No camera data found for IP {ip}.")
            return None
        return row
    except Exception as e:
        print(f"❌ DB error in read_camera_config_by_ip: {e}")
        raise
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

def read_camera_config_by_id(camera_id: int):
    """
    Read a camera authentication configuration by ID.
    """
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT id, ip, name, username, channel_count FROM camera_master WHERE id = ?", (camera_id,))
        row = cursor.fetchone()
        if not row:
            print(f"⚠️ No camera data found for ID {camera_id}.")
            return None
        return row
    except Exception as e:
        print(f"❌ DB error in read_camera_config_by_id: {e}")
        raise
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def delete_camera_config_by_id(camera_id: int) -> dict:
    """
    Delete a camera authentication configuration by ID.
    Also cleans up camera_channel rows if no other camera_master row shares the
    same IP. Zone polygons stay, so reconnecting the NVR brings its zones back.
    """
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT ip FROM camera_master WHERE id = ?", (camera_id,))
        row = cursor.fetchone()
        if not row:
            return {"message": "Camera with this ID does not exist."}

        ip = row[0]
        cursor.execute("DELETE FROM camera_master WHERE id = ?", (camera_id,))

        # Check if any other camera_master rows still use this IP
        cursor.execute("SELECT COUNT(*) FROM camera_master WHERE ip = ?", (ip,))
        remaining = cursor.fetchone()[0]
        if remaining == 0:
            # Must delete camera_calibration first (FK: camera_calibration -> camera_channel)
            cursor.execute("DELETE FROM camera_calibration WHERE camera_ip = ?", (ip,))
            cursor.execute("DELETE FROM camera_channel WHERE ip = ?", (ip,))

        conn.commit()
        return {"message": "Camera authentication configuration deleted successfully!"}
    except Exception as e:
        print(f"❌ DB error in delete_camera_config: {e}")
        raise
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

# Settings that belong to an NVR and are keyed by its IP. When a network's IP is
# edited they move with it; the *_logs tables are history and keep the IP the
# rows were recorded under.
_IP_KEYED_CONFIG_TABLES = (
    ("camera_channel", "ip"),
    ("camera_calibration", "camera_ip"),
    ("in_pass_polygon", "ip"),
    ("hotzone_polygon", "ip"),
    ("rate_polygon", "ip"),
    ("attendance_polygon", "ip"),
    ("in_pass_threshold", "ip"),   # created by the DLC (ai_camera_db): may not exist yet
)


def read_camera_config_with_password(camera_id: int) -> dict | None:
    """{id, ip, name, username, password} with the password decrypted, or None."""
    conn = cursor = None
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT id, ip, name, username, password, channel_count FROM camera_master WHERE id = ?", (camera_id,))
        row = cursor.fetchone()
        if not row:
            return None
        row["password"] = decrypt_password(row["password"]) if row["password"] else ""
        return row
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def update_camera_config_by_id(camera_id: int, name: str, ip: str, username: str, password: str,
                               channel_count: int | None = None) -> dict:
    """Save an edited network: name, IP and NVR credentials.

    The caller has already checked the credentials against the (new) IP.

    Credentials belong to the NVR, not to the row: the app reads them per IP
    (read_username_password_by_ip takes the first row), so every network on
    that IP gets the new username/password, or the services would keep using
    whichever row MariaDB returned first.

    When the IP changes and no other network still uses the old one, the NVR's
    settings (scanned channels, selection, calibration, zone polygons,
    in-pass thresholds) move to the new IP — it is the same device at a new
    address. If the new IP already belongs to another network, its settings are
    kept and every row of the old IP in those same tables (zone polygons and
    thresholds included) is deleted. If another
    network still uses the old IP, the settings stay with it and only this
    network's camera selection is released.

    channel_count (8, 16 or 32), when given, becomes the NVR's for every network on
    the new IP; the caller has already refused a count below a selected channel.

    Returns {"id", "ip", "name", "username", "channel_count", "needs_scan"}: needs_scan is True
    when the new IP has no scanned channels, so the caller should scan it.
    """
    conn = cursor = None
    try:
        conn = get_db()
        conn.autocommit = False
        cursor = conn.cursor()
        cursor.execute("SELECT ip FROM camera_master WHERE id = ?", (camera_id,))
        row = cursor.fetchone()
        if not row:
            return {"message": "Camera with this ID does not exist."}
        old_ip = row[0]

        cursor.execute("SELECT COUNT(*) FROM camera_master WHERE ip = ? AND name = ? AND id <> ?",
                       (ip, name, camera_id))
        if cursor.fetchone()[0]:
            return {"message": "Camera with this IP and name already exists."}

        cursor.execute("UPDATE camera_master SET name = ?, ip = ? WHERE id = ?", (name, ip, camera_id))
        cursor.execute("UPDATE camera_master SET username = ?, password = ? WHERE ip = ?",
                       (username, encrypt_password(password), ip))

        if ip != old_ip:
            cursor.execute("SELECT COUNT(*) FROM camera_master WHERE ip = ?", (old_ip,))
            old_still_used = cursor.fetchone()[0] > 0
            cursor.execute("SELECT COUNT(*) FROM camera_channel WHERE ip = ?", (ip,))
            new_ip_known = cursor.fetchone()[0] > 0

            if old_still_used:
                # The NVR stays where it was for the other network; this network
                # just no longer has cameras selected there.
                cursor.execute("UPDATE camera_channel SET status = 0, camera_master_id = NULL "
                               "WHERE camera_master_id = ?", (camera_id,))
            else:
                # camera_calibration -> camera_channel(ip, channel) has no ON UPDATE
                # CASCADE, so neither side can change first with checks on. Both
                # change inside this one transaction.
                cursor.execute("SET FOREIGN_KEY_CHECKS = 0")
                try:
                    for table, column in _IP_KEYED_CONFIG_TABLES:
                        try:
                            if new_ip_known:
                                cursor.execute(f"DELETE FROM {table} WHERE {column} = ?", (old_ip,))
                            else:
                                cursor.execute(f"UPDATE {table} SET {column} = ? WHERE {column} = ?", (ip, old_ip))
                        except Exception as e:
                            if "doesn't exist" not in str(e):
                                raise
                finally:
                    cursor.execute("SET FOREIGN_KEY_CHECKS = 1")

        if channel_count is not None:
            cursor.execute("UPDATE camera_master SET channel_count = ? WHERE ip = ?", (channel_count, ip))
        cursor.execute("SELECT channel_count FROM camera_master WHERE id = ?", (camera_id,))
        channel_count = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM camera_channel WHERE ip = ?", (ip,))
        needs_scan = cursor.fetchone()[0] == 0
        if needs_scan:
            cursor.execute("UPDATE camera_master SET is_scanned = 0 WHERE ip = ?", (ip,))
        else:
            cursor.execute("UPDATE camera_master SET is_scanned = 1 WHERE id = ?", (camera_id,))

        conn.commit()
        print(f"✅ Network {camera_id} updated: {old_ip} -> {ip} ({name})")
        return {"id": camera_id, "ip": ip, "name": name, "username": username,
                "channel_count": channel_count, "needs_scan": needs_scan}
    except Exception as e:
        if conn:
            conn.rollback()
        print(f"❌ DB error in update_camera_config: {e}")
        raise
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

def read_username_password_by_ip(ip: str) -> tuple[str, str]:
    """
    Read the username and password for a camera by IP.
    """
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT username, password FROM camera_master WHERE ip = ?", (ip,))
        row = cursor.fetchone()
        if not row:
            print(f"⚠️ No camera data found for IP {ip}.")
            return None, None
        return row['username'], decrypt_password(row['password'])
    except Exception as e:
        print(f"❌ DB error in read_username_password_by_ip: {e}")
        raise
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

DEFAULT_CHANNEL_COUNT = 16
CHANNEL_COUNT_CHOICES = (8, 16, 32)


def read_channel_count_by_ip(ip: str) -> int:
    """How many channels Camera Selection lists for this NVR (8, 16 or 32)."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT MAX(channel_count) FROM camera_master WHERE ip = ?", (ip,))
        row = cursor.fetchone()
        return int(row[0]) if row and row[0] else DEFAULT_CHANNEL_COUNT
    finally:
        conn.close()


def highest_selected_channel(ip: str, exclude_master_id: int | None = None) -> int:
    """The highest channel of this NVR that a network has selected, or 0."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT MAX(channel) FROM camera_channel WHERE ip = ? AND status = 1", (ip,))
        row = cursor.fetchone()
        return int(row[0]) if row and row[0] else 0
    finally:
        conn.close()


def read_scan_status_by_ip(ip: str) -> bool:
    """
    Read the scan status for a camera by IP.
    Status: 0 = not scanned/pending, 1 = scanned
    """
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT is_scanned FROM camera_master WHERE ip = ?", (ip,))
        row = cursor.fetchone()
        if not row:
            print(f"⚠️ No camera data found for IP {ip}.")
            return None
        return bool(row['is_scanned'])
    except Exception as e:
        print(f"❌ DB error in read_scan_status_by_ip: {e}")
        raise
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

def update_scan_status(ip: str, status: bool) -> dict:
    """
    Update the scan status for a camera by IP.
    Status: 0 = not scanned/pending, 1 = scanned
    """
    try:
        conn = get_db()
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM camera_master WHERE ip = ?", (ip,))
        if cursor.fetchone():
            cursor.execute(
                "UPDATE camera_master SET is_scanned = ? WHERE ip = ?",
                (status, ip))
            conn.commit()
            print(f"✅ Camera scan status updated successfully for IP {ip}.")
            return {"message": "Camera scan status updated successfully!"}
        else:
            return {"message": "Camera with this IP does not exist."}
    except Exception as e:
        print(f"❌ DB error in update_scan_status: {e}")
        raise
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


# ------------- Camera Channel Functions -----------
def create_camera_channel(ip: str, channels: list) -> dict:
    try:
        conn = get_db()
        cursor = conn.cursor()
        for channel in channels:
            cursor.execute(
                "INSERT INTO camera_channel (ip, channel, status) VALUES (?, ?, 0)",
                (ip, channel)
            )
        conn.commit()
        print(f"✅ Camera channels created successfully for IP {ip}: {channels}")
    except Exception as e:
        print(f"❌ DB error in create_camera_channel: {e}")
        raise
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

def ensure_camera_channels(ip: str, channels) -> int:
    """Add the channels that are not listed for this IP yet (status 0); returns how many were new."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT channel FROM camera_channel WHERE ip = ?", (ip,))
        have = {int(r[0]) for r in cursor.fetchall()}
        new = [int(c) for c in channels if int(c) not in have]
        for channel in new:
            cursor.execute("INSERT IGNORE INTO camera_channel (ip, channel, status) VALUES (?, ?, 0)", (ip, channel))
        conn.commit()
        return len(new)
    finally:
        conn.close()


def read_camera_channels(status) -> List[CameraChannelOutput] | dict:
    """
    Get all camera channels.
    """
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True)
        if status is None:
            query = """
            SELECT cc.ip, COALESCE(cm.name, '') AS network_name, cc.channel, cc.status
            FROM camera_channel cc
            LEFT JOIN camera_master cm ON cc.camera_master_id = cm.id
            """
            cursor.execute(query)
        else:
            query = """
            SELECT cc.ip, COALESCE(cm.name, '') AS network_name, cc.channel, cc.status
            FROM camera_channel cc
            LEFT JOIN camera_master cm ON cc.camera_master_id = cm.id
            WHERE cc.status = ?
            """
            cursor.execute(query, (status,))
        rows = cursor.fetchall()
        if not rows:
            print("⚠️ No camera channel data found.")
            return []
        return rows
    except Exception as e:
        print(f"❌ DB error in read_camera_channels: {e}")
        raise
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

def read_camera_channels_by_ip(ip: str) -> List[CameraChannelOutput] | dict:
    """
    Get camera channels by IP.
    """
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True)
        query = """
        SELECT cc.ip, COALESCE(cm.name, '') AS network_name, cc.channel, cc.status
        FROM camera_channel cc
        LEFT JOIN camera_master cm ON cc.camera_master_id = cm.id
        WHERE cc.ip = ?
        """
        cursor.execute(query, (ip,))
        rows = cursor.fetchall()
        if not rows:
            print(f"⚠️ No camera channel data found for IP {ip}.")
            return []
        return rows
    except Exception as e:
        print(f"❌ DB error in read_camera_channel_by_ip: {e}")
        raise
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

def update_camera_channel_status(camera_data: List[CameraSelectInput]):
    """
    Update the status of camera channels.
    Set status to 0 for unselected, 1 for selected.
    Also stamps camera_master_id based on the network_name provided.
    """
    try:
        conn = get_db()
        cursor = conn.cursor()

        cursor.execute("UPDATE camera_channel SET status = 0, camera_master_id = NULL")

        for camera in camera_data:
            # Look up camera_master.id from (ip, name)
            cursor.execute(
                "SELECT id FROM camera_master WHERE ip = ? AND LOWER(name) = LOWER(?)",
                (camera.ip, camera.network_name)
            )
            row = cursor.fetchone()
            camera_master_id = row[0] if row else None

            cursor.execute(
                "UPDATE camera_channel SET status = 1, camera_master_id = ? WHERE ip = ? AND channel = ?",
                (camera_master_id, camera.ip, camera.channel)
            )
        conn.commit()
        print(f"✅ Camera channel status updated successfully for cameras")
        return {"message": "Camera channel status updated successfully!"}
    except Exception as e:
        print(f"❌ DB error in update_camera_channel_status: {e}")
        raise
