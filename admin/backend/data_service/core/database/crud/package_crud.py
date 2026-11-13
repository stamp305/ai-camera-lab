from typing import List

from data_service.core.database.session import get_db

def read_all_packages() -> List:
    """
    Reads all package data from the database.
    """
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM packages")
        return cursor.fetchall()
    except Exception as e:
        print(f"Error reading all packages: {e}")
        return []
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

def create_package_in_db(name: str):
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO packages (name) VALUES (%s)", (name.capitalize(),))
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        raise e
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

def delete_package_in_db(package_id: int):
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM packages WHERE id = %s", (package_id,))
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        raise e
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

def read_package_id_by_name(package_name: str) -> int:
    """
    Reads the package ID for a given package name.

    Args:
        package_name (str): The name of the package.

    Returns:
        int: The ID of the package, or -1 if not found.
    """
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT id FROM packages WHERE name = %s", (package_name,))
        result = cursor.fetchone()
        return result['id'] if result else -1
    except Exception as e:
        print(f"Error reading package ID by name: {e}")
        return -1
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()