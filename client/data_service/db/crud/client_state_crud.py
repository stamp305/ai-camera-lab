"""Small named values the app keeps for itself (table client_state).

They used to be loose JSON files in ~/Library/Application Support/AICamera,
readable and editable by anyone on the Mac. The first one moved here is the
version the admin assigned to this branch.
"""
from typing import Optional

from data_service.db.session import get_db

ASSIGNED_VERSION = "assigned_version"


def get_state(name: str) -> Optional[str]:
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT value FROM client_state WHERE name = ?", (name,))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def set_state(name: str, value: Optional[str]) -> None:
    """Store a value; None removes it."""
    conn = get_db()
    try:
        cur = conn.cursor()
        if value is None:
            cur.execute("DELETE FROM client_state WHERE name = ?", (name,))
        else:
            cur.execute("INSERT INTO client_state (name, value) VALUES (?, ?) "
                        "ON DUPLICATE KEY UPDATE value = VALUES(value)", (name, value))
        conn.commit()
    finally:
        conn.close()
