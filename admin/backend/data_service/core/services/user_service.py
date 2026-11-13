# data_service/core/services/user_service.py
"""Business logic for user management."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from data_service.core.exceptions import (
    NotFoundError,
    DuplicateError,
    ValidationError,
    DatabaseError,
)
from data_service.core.database.crud.user_crud import (
    create_user_in_db,
    read_all_users_from_db,
    delete_user_in_db,
    update_user_package_and_version_in_db,
    update_user_online_status_in_db,
    create_service_for_user_in_db,
    read_user_services_from_db,
)
from data_service.core.database.crud.group_crud import (
    create_group_in_db,
    read_group_id_by_name,
)
from data_service.core.database.crud.service_crud import (
    read_service_folder_by_ids,
)
from data_service.core.utils.user_util import (
    group_users_by_group,
    generate_activation_code,
)
from data_service.core.utils.update_util import (
    send_update_client_by_service_id,
)

logger = logging.getLogger(__name__)


def create_user(
    activation_code: str,
    serial_number: str,
    mac_address: str,
    group_name: str,
    branch_name: str | None = None
) -> Dict[str, str]:
    """Create a new user with validation."""
    # Validate required fields
    if not all([activation_code, serial_number, mac_address, group_name]):
        raise ValidationError("All fields are required", "user")
    
    # Check for duplicates
    existing_users = read_all_users_from_db()
    for existing_user in existing_users:
        if existing_user['activation_code'] == activation_code:
            raise DuplicateError("User", "activation_code", activation_code)
        if existing_user['serial_number'] == serial_number:
            raise DuplicateError("User", "serial_number", serial_number)
        if existing_user['mac_address'] == mac_address:
            raise DuplicateError("User", "mac_address", mac_address)
    
    # Get or create group
    group_id = read_group_id_by_name(group_name)
    if group_id == -1:
        try:
            create_group_in_db(group_name)
            group_id = read_group_id_by_name(group_name)
        except Exception as e:
            logger.error(f"Failed to create group '{group_name}': {e}")
            raise DatabaseError("create group", e)
    
    # Create the user
    try:
        create_user_in_db(group_id, activation_code, serial_number, mac_address, branch_name)
        logger.info(f"Created user with activation_code={activation_code}")
        return {"message": "User created successfully"}
    except Exception as e:
        logger.error(f"Failed to create user: {e}")
        raise DatabaseError("create user", e)

from typing import Optional
def update_user_info(activation_code: str, group_name: str, branch_name: Optional[str] = None) -> Dict[str, str]:
    """Update a user's group and branch."""
    from data_service.core.database.crud.user_crud import update_user_info_in_db
    
    # Get or create group
    group_id = read_group_id_by_name(group_name)
    if group_id == -1:
        try:
            create_group_in_db(group_name)
            group_id = read_group_id_by_name(group_name)
        except Exception as e:
            logger.error(f"Failed to create group '{group_name}': {e}")
            raise DatabaseError("create group", e)
    
    try:
        update_user_info_in_db(activation_code, group_id, branch_name)
        logger.info(f"Updated info for user {activation_code}: group='{group_name}', branch='{branch_name}'")
        return {"message": f"User info updated successfully"}
    except Exception as e:
        logger.error(f"Failed to update user info: {e}")
        raise DatabaseError("update user info", e)


def get_all_users() -> List[Dict[str, Any]]:
    """Get all users grouped by group."""
    try:
        users = read_all_users_from_db()
        if not users:
            return []
        return group_users_by_group(users)
    except Exception as e:
        logger.error(f"Failed to retrieve users: {e}")
        raise DatabaseError("read users", e)


def delete_user(activation_code: str) -> Dict[str, str]:
    """Delete a user by activation code."""
    try:
        delete_user_in_db(activation_code)
        logger.info(f"Deleted user with activation_code={activation_code}")
        return {"message": "User deleted successfully"}
    except Exception as e:
        logger.error(f"Failed to delete user: {e}")
        raise DatabaseError("delete user", e)


def update_user_services(
    activation_code: str,
    service_ids: List[int],
    package: str,
    version: str
) -> Dict[str, str]:
    """Update services for a user."""
    try:
        logger.info(f"Updating services for user {activation_code}: package={package}, services={service_ids}")
        update_user_package_and_version_in_db(activation_code, package, version)
        create_service_for_user_in_db(activation_code, service_ids)
        update_result = send_update_client_by_service_id(activation_code, read_service_folder_by_ids(service_ids))
        
        response = {"message": "Services created successfully"}
        if update_result:
             response["update_status"] = update_result.get("status")
             response["update_message"] = update_result.get("message")
        
        return response
    except Exception as e:
        logger.error(f"Failed to update services for user {activation_code}: {e}")
        raise DatabaseError("update user services", e)


def get_user_services(activation_code: str) -> Dict[str, Any]:
    """Get services for a user."""
    try:
        services = read_user_services_from_db(activation_code)
        return {"services": services}
    except Exception as e:
        logger.error(f"Failed to get services for user {activation_code}: {e}")
        raise DatabaseError("read user services", e)


def update_online_status(client_list: List[str]) -> Dict[str, str]:
    """Update online status for users."""
    try:
        logger.info(f"Updating online status for users: {client_list}")
        update_user_online_status_in_db(client_list)
        return {"message": "User online status updated successfully"}
    except Exception as e:
        logger.error(f"Failed to update online status: {e}")
        raise DatabaseError("update online status", e)


def generate_new_activation_code() -> Dict[str, str]:
    """Generate a unique activation code."""
    activation_code = generate_activation_code()
    existing_users = read_all_users_from_db()
    existing_codes = {user['activation_code'] for user in existing_users}
    
    # Ensure uniqueness
    max_attempts = 100
    attempts = 0
    while activation_code in existing_codes and attempts < max_attempts:
        activation_code = generate_activation_code()
        attempts += 1
    
    if attempts >= max_attempts:
        raise DatabaseError("generate activation code", Exception("Could not generate unique code"))
    
    return {"activation_code": activation_code}
