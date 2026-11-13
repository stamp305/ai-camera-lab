from typing import Optional

from data_service.db.session import get_db

def update_account(username: str, password: str) -> bool:
    """Update the username and password of the account. Assumes single account system."""
    try:
        conn = get_db()
        cursor = conn.cursor()

        cursor.execute("DELETE FROM account_config")
        cursor.execute(
            "INSERT INTO account_config (username, password, is_activated, credentials_changed) VALUES (?, ?, ?, ?)",
            (username, password, 1, 1)
        )
        conn.commit()
        return cursor.rowcount > 0
    except Exception as e:
        print(f"DB error creating account: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

def read_username_password() -> Optional[tuple]:
    """Read the current username and password from the database."""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT username, password FROM account_config")
        result = cursor.fetchone()
        return (result[0], result[1]) if result else None
    except Exception as e:
        print(f"DB error reading username/password: {e}")
        return None
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

def read_password_by_username(username: str) -> Optional[str]:
    """Fetch stored password hash for a given username."""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT password FROM account_config WHERE username = ?", (username,))
        result = cursor.fetchone()
        return result[0] if result else None
    except Exception as e:
        print(f"DB error reading password for {username}: {e}")
        return None
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

def read_activated_status() -> Optional[bool]:
    """Check if the account with the given username is activated."""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT is_activated FROM account_config")
        result = cursor.fetchone()
        return bool(result[0]) if result else None
    except Exception as e:
        print(f"DB error reading activation status: {e}")
        return None
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

def update_activated_status(is_activated: bool) -> bool:
    """Update the activation status of the account. Assumes single account system."""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("UPDATE account_config SET is_activated = ?", (int(is_activated),))
        conn.commit()
        return cursor.rowcount > 0
    except Exception as e:
        print(f"DB error updating activation status: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

def read_credentials_changed() -> Optional[bool]:
    """Check if the credentials have been changed from default."""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT credentials_changed FROM account_config")
        result = cursor.fetchone()
        return bool(result[0]) if result else None
    except Exception as e:
        print(f"DB error reading credentials_changed status: {e}")
        return None
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()