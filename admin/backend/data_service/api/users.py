# data_service/api/users.py
"""User API endpoints."""
from __future__ import annotations

from typing import List

from fastapi import APIRouter

from data_service.core.schemas.user_schemas import (
    UserCreateRequest,
    GroupedUsers,
    UserOnlineStatus,
    UserServiceCreateRequest,
)
from data_service.core.services import user_service

router = APIRouter(prefix="/users", tags=["users"])


# =============================================================
# User Endpoints
# =============================================================
@router.post("")
def create_user(user: UserCreateRequest):
    """Create a new user."""
    return user_service.create_user(
        activation_code=user.activation_code,
        serial_number=user.serial_number,
        mac_address=user.mac_address,
        group_name=user.group_name,
        branch_name=user.branch_name,
    )

from pydantic import BaseModel
from typing import Optional
class UserInfoUpdateRequest(BaseModel):
    group_name: str
    branch_name: Optional[str] = None

@router.put("/{activation_code}/info")
def update_user_info(activation_code: str, request: UserInfoUpdateRequest):
    """Update a user's info (group and branch)."""
    return user_service.update_user_info(activation_code, request.group_name, request.branch_name)


@router.get("", response_model=List[GroupedUsers])
def get_all_users():
    """Get all users grouped by group."""
    return user_service.get_all_users()


@router.delete("/{activation_code}")
def delete_user(activation_code: str):
    """Delete a user by activation code."""
    return user_service.delete_user(activation_code)


# =============================================================
# Service Endpoints
# =============================================================
@router.post("/{activation_code}/services")
def create_service_for_user(request: UserServiceCreateRequest, activation_code: str):
    """Create services for a user by activation code."""
    return user_service.update_user_services(
        activation_code=activation_code,
        service_ids=request.service_ids,
        package=request.package,
        version=request.version,
    )


@router.get("/{activation_code}/services")
def get_services_for_user(activation_code: str):
    """Get services for a user by activation code."""
    return user_service.get_user_services(activation_code)


# =============================================================
# Utility Endpoints
# =============================================================
@router.patch("/online")
def set_user_online(user: UserOnlineStatus):
    """Update online status for users."""
    return user_service.update_online_status(user.current_client_list)


@router.get("/activation-code")
def get_activation_code():
    """Generate a new unique activation code."""
    return user_service.generate_new_activation_code()