from typing import List

from data_service.core.database.session import get_db

def create_group_in_db(group_name: str):
    """
    Create a new group in the database.
    """
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO groups (name) VALUES (%s)",
            (group_name,)
        )
        conn.commit()
        cursor.close()
        return {"message": "Group created successfully"}
    except Exception as e:
        raise Exception(f"Error creating group: {e}")
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

def read_all_groups() -> List:
    """
    Reads all group data from the database.
    """
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM groups")
        return cursor.fetchall()
    except Exception as e:
        print(f"Error reading all owner group: {e}")
        return []
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

def read_group_id_by_name(group_name: str) -> int:
    """
    Reads the group ID for a given group name.

    Args:
        group_name (str): The name of the group.

    Returns:
        int: The ID of the group, or -1 if not found.
    """
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM groups WHERE name = %s", (group_name,))
        result = cursor.fetchone()
        if result:
            return result[0]
        return -1
    except Exception as e:
        print(f"Error reading group ID by name: {e}")
        return -1
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()