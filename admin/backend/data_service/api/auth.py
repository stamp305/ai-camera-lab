# data_service/api/auth.py
"""Authentication API endpoints."""
from __future__ import annotations

from fastapi import APIRouter

from data_service.core.schemas.auth_schemas import DeviceVerification, UserActivation
from data_service.core.services import auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/activate")
def activate_user(user: UserActivation):
    """Activate a user."""
    return auth_service.activate_user(user)


@router.post("/verify-device")
def verify_device(request: DeviceVerification):
    """Tell the frontend whether a Device Key belongs to an Activation Code.

    Body: {"activation_code": str, "device_key": str (optional)}. An empty
    device_key counts as not presented.

    Always answers 200 {"verdict": <verdict>} for a well-formed body: this is a
    question the frontend asks, not an auth failure (500 only when the users
    table cannot be read). Exactly one verdict:
      ok                       code known, key on record, presented key matches
      invalid_device_key       code known and a key presented, but none on
                               record or it does not match; also a key on
                               record and none presented
      no_key                   code known, no key on record, none presented
      unknown_activation_code  no users row has this code
    """
    return auth_service.verify_device(request)
