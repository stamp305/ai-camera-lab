from data_service.core.database.session import get_db
from data_service.core.schemas.auth_schemas import UserActivation
from data_service.core.logging_config import get_logger

logger = get_logger(__name__)


def read_data_by_serial_number(serial_number: str) -> dict:
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM users WHERE serial_number = %s", (serial_number,))
        return cursor.fetchone()
    except Exception as e:
        logger.exception("Error reading data by serial number", extra={"serial_number": serial_number})
        return None
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

def update_user_activation_success(user: UserActivation, device_key_hash: str, set_start_date: bool) -> bool:
    """Mark the user activated and store its new Device Key hash.

    Returns False when the row already holds a key by the time of the write (a
    concurrent Activation got there first) and leaves it untouched. Raises on
    failure: swallowing it would hand the Branch a key that was never stored.
    start_date is only written when set_start_date is True, so a Branch
    re-activating for its first key keeps the date it started.
    """
    conn = None
    cursor = None
    try:
        conn = get_db()
        cursor = conn.cursor()
        start_date_sql = ", start_date = NOW()" if set_start_date else ""
        # Same test as auth_service: refused only when activated AND a non-empty
        # key is on record ('' and NULL both count as no key; NULL is not 1).
        cursor.execute(
            f"UPDATE users SET device_key_hash = %s, is_activated = 1{start_date_sql} "
            "WHERE serial_number = %s "
            "AND (COALESCE(device_key_hash, '') = '' OR COALESCE(is_activated, 0) <> 1)",
            (device_key_hash, user.serial_number)
        )
        conn.commit()
        return cursor.rowcount > 0
    except Exception as e:
        logger.exception("Error updating user activation", extra={"serial_number": user.serial_number})
        raise
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

def read_device_key_hash(activation_code: str) -> dict:
    """Return {"device_key_hash": ...} for this Activation Code, or None when no user has it.

    Raises when the database is unreachable, so an outage is never read as an
    unknown Activation Code.
    """
    conn = None
    cursor = None
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT device_key_hash FROM users WHERE activation_code = %s", (activation_code,))
        return cursor.fetchone()
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

def add_device_key_hash_column_if_missing() -> None:
    """Create users.device_key_hash (nullable) when it does not exist yet.

    Matches deploy/migrations/20260924_add_users_device_key_hash.sql. The users
    table lives on this mysql.connector pool, not on the SQLAlchemy engine that
    branch_db._add_column_if_missing works on.
    """
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SHOW COLUMNS FROM users LIKE 'device_key_hash'")
        if not cursor.fetchall():
            cursor.execute("ALTER TABLE users ADD COLUMN device_key_hash CHAR(64) NULL")
            conn.commit()
    finally:
        cursor.close()
        conn.close()
