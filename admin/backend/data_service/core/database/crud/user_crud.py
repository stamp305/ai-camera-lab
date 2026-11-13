from typing import List, Optional

from data_service.core.database.session import get_db
from data_service.core.schemas.user_schemas import UserCreateRequest, UserReadResponse
from data_service.core.database.crud.package_crud import read_package_id_by_name


# =============================================================
# User Management
# =============================================================
def create_user_in_db(group_id: int, activation_code: str, serial_number: str, mac_address: str, branch_name: Optional[str] = None):
    """
    Create a new user in the database.
    """
    conn = None
    cursor = None
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO users (group_id, activation_code, serial_number, mac_address, branch_name)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (group_id, activation_code, serial_number, mac_address, branch_name)
        )
        conn.commit()
        cursor.close()
        return {"message": "User created successfully"}
    except Exception as e:
        raise Exception(f"Error creating user: {e}")
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

def read_all_users_from_db() -> List:
    """
    Reads all user data from the database.
    """
    conn = None
    cursor = None
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM users")
        rows = cursor.fetchall()
        # Device Key hashes stay in the database: this read feeds the admin UI
        # listing and sync_branches_from_users().
        for row in rows:
            row.pop("device_key_hash", None)
        return rows
    except Exception as e:
        print(f"Error reading all user data: {e}")
        # Must propagate, not return []: sync_branches_from_users() treats an
        # empty list as "no users exist" and deletes every branch to match —
        # swallowing a DB-unreachable error here wiped the whole table.
        raise
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

def delete_user_in_db(activation_code: str):
    """
    Deletes a user from the database by activation code.
    """
    conn = None
    cursor = None
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM users WHERE activation_code = %s",
            (activation_code,)
        )
        conn.commit()
        if cursor.rowcount == 0:
            raise Exception("User not found")
        return {"message": "User deleted successfully"}
    except Exception as e:
        raise Exception(f"Error deleting user: {e}")
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

def update_user_group_in_db(activation_code: str, group_id: int):
    """
    Updates the group of a user identified by activation code.
    """
    conn = None
    cursor = None
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET group_id = %s WHERE activation_code = %s",
            (group_id, activation_code)
        )
        conn.commit()
        return {"message": "User group updated successfully"}
    except Exception as e:
        raise Exception(f"Error updating user group: {e}")
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

def update_user_info_in_db(activation_code: str, group_id: int, branch_name: Optional[str] = None) -> None:
    """Updates a user's group and branch name in the database."""
    from data_service.core.database.session import get_db
    import mysql.connector
    conn = get_db()
    if conn is None:
        raise Exception("Could not connect to database")
    try:
        cursor = conn.cursor()
        query = """
            UPDATE users
            SET group_id = %s, branch_name = %s
            WHERE activation_code = %s
        """
        cursor.execute(query, (group_id, branch_name, activation_code))
        conn.commit()
    except mysql.connector.Error as e:
        print(f"Error updating user info: {e}")
        if conn:
            conn.rollback()
        raise e
    finally:
        if conn and conn.is_connected():
            cursor.close()
            conn.close()

def update_user_package_and_version_in_db(activation_code: str, package: str, version: str):
    """
    Updates the package of a user identified by activation code.

    Args:
        activation_code (str): The activation code of the user.
        package (str): The new package to set.

    Returns:
        dict: A message indicating success or failure.
    """
    conn = None
    cursor = None
    try:
        conn = get_db()
        cursor = conn.cursor()

        package_id = read_package_id_by_name(package)
        if package_id == -1:
            raise Exception(f"Package '{package}' not found")
        
        cursor.execute(
            "UPDATE users SET package_id = %s, version = %s WHERE activation_code = %s",
            (package_id, version, activation_code)
        )
        conn.commit()
        return {"message": "User package updated successfully"}
    except Exception as e:
        raise Exception(f"Error updating user package: {e}")
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

def update_user_online_status_in_db(activation_code_list: List[str]):
    """
    Updates the online status of users identified by a list of activation codes.

    Args:
        activation_code_list (List[str]): List of activation codes of the users.
        is_online (bool): The online status to set (True for online, False for offline).

    Returns:
        dict: A message indicating success or failure.
    """
    conn = None
    cursor = None
    try:
        conn = get_db()
        cursor = conn.cursor()

        # One statement, one commit: every Branch reconnecting at once sends this
        # together, and a statement without WHERE locks rows in primary-key order,
        # so concurrent calls queue instead of deadlocking (1213), as the old
        # "all offline, then each online" pair did, which locked in two orders.
        if activation_code_list:
            marks = ", ".join(["%s"] * len(activation_code_list))
            cursor.execute(f"UPDATE users SET is_online = COALESCE(activation_code IN ({marks}), 0)",
                           tuple(activation_code_list))
        else:
            cursor.execute("UPDATE users SET is_online = 0")
        conn.commit()
        return {"message": "User online status updated successfully"}
    except Exception as e:
        raise Exception(f"Error updating user online status: {e}")
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

# =============================================================
# Service Management
# =============================================================
def create_service_for_user_in_db(activation_code: str, service_ids: List[int]):
    """
    Creates services for a user identified by activation code.

    Args:
        activation_code (str): The activation code of the user.
        service_ids (List[int]): List of service IDs to associate with the user.

    Returns:
        dict: A message indicating success or failure.
    """
    conn = None
    cursor = None
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True)

        # Get user ID from activation code
        cursor.execute(
            "SELECT id FROM users WHERE activation_code = %s",
            (activation_code,)
        )
        user = cursor.fetchone()
        if not user:
            raise Exception("User not found")
        user_id = user['id']
        

        # Delete existing services for the user to avoid duplicates
        cursor.execute(
            "DELETE FROM user_services WHERE user_id = %s",
            (user_id,)
        )

        # Insert services for the user
        for service_id in service_ids:
            cursor.execute(
                "INSERT INTO user_services (user_id, service_id) VALUES (%s, %s)",
                (user_id, service_id)
            )
        conn.commit()
        return {"message": "Services added to user successfully"}
    except Exception as e:
        raise Exception(f"Error creating services for user: {e}")
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

def read_user_services_from_db(activation_code: str) -> List[int]:
    """
    Reads service IDs associated with a user identified by activation code.

    Args:
        activation_code (str): The activation code of the user.

    Returns:
        List[int]: A list of service IDs associated with the user.
    """
    conn = None
    cursor = None
    try:
        conn = get_db()
        cursor = conn.cursor()

        # Get user ID from activation code
        cursor.execute(
            "SELECT id FROM users WHERE activation_code = %s",
            (activation_code,)
        )
        user = cursor.fetchone()
        if not user:
            raise Exception("User not found")

        user_id = user[0]

        # Get service names associated with the user
        cursor.execute(
            "SELECT s.name FROM services s "
            "JOIN user_services us ON s.id = us.service_id "
            "WHERE us.user_id = %s",
            (user_id,)
        )
        services = cursor.fetchall()
        return [service[0] for service in services]
    except Exception as e:
        raise Exception(f"Error reading services for user: {e}")
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

def delete_user_services_from_db(activation_code: str, service_id_list: List[int]):
    """
    Deletes services associated with a user identified by activation code.

    Args:
        activation_code (str): The activation code of the user.

    Returns:
        dict: A message indicating success or failure.
    """
    conn = None
    cursor = None
    try:
        conn = get_db()
        cursor = conn.cursor()

        # Get user ID from activation code
        cursor.execute(
            "SELECT id FROM users WHERE activation_code = %s",
            (activation_code,)
        )
        user = cursor.fetchone()
        if not user:
            raise Exception("User not found")

        user_id = user[0]

        # Delete services
        for service_id in service_id_list:
            cursor.execute(
                "DELETE FROM user_services WHERE user_id = %s AND service_id = %s",
                (user_id, service_id)
            )
        conn.commit()
        return {"message": "Services deleted from user successfully"}
    except Exception as e:
        raise Exception(f"Error deleting services for user: {e}")
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()