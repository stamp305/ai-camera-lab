# data_service/core/services/auth_service.py
"""Business logic for authentication."""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from typing import Any, Dict

from data_service.core.exceptions import NotFoundError, AuthenticationError, DatabaseError, DeviceKeyExistsError
from data_service.core.database.crud.auth_crud import (
    read_data_by_serial_number,
    read_device_key_hash,
    update_user_activation_success,
)
from data_service.core.schemas.auth_schemas import DeviceVerification, UserActivation

logger = logging.getLogger(__name__)


def _hash_device_key(device_key: str) -> str:
    """Only this SHA-256 hex digest of a Device Key is ever stored."""
    # surrogatepass: a hostile key with a lone surrogate hashes (and mismatches)
    # instead of raising; issued keys are ASCII, so their digest is unaffected.
    return hashlib.sha256(device_key.encode("utf-8", "surrogatepass")).hexdigest()


def activate_user(user: UserActivation) -> Dict[str, str]:
    """Activate a user with validation and issue its Device Key."""
    # Get user data
    user_data = read_data_by_serial_number(user.serial_number)

    if not user_data:
        logger.warning(f"Activation attempt for unknown serial_number: {user.serial_number}")
        raise NotFoundError("User", user.serial_number)

    # Validate activation code
    if user_data['activation_code'] != user.activation_code:
        logger.warning(
            f"Activation code mismatch for {user.serial_number}: "
            f"expected {user_data['activation_code']}, got {user.activation_code}"
        )
        raise AuthenticationError("Invalid activation code")

    # Validate MAC address
    if user_data['mac_address'] != user.mac_address:
        logger.warning(
            f"MAC address mismatch for {user.serial_number}: "
            f"expected {user_data['mac_address']}, got {user.mac_address}"
        )
        raise AuthenticationError("MAC address mismatch")

    # Once a Branch holds a key, only an admin clearing device_key_hash reopens
    # Activation (ADR 0007). The row is left as it is.
    if user_data.get('is_activated') == 1 and user_data.get('device_key_hash'):
        logger.warning(f"Activation refused for {user.serial_number}: already holds a device key")
        raise DeviceKeyExistsError(user.serial_number)

    # Issue the key, store only its hash. start_date is kept when already set, so
    # an old Branch re-activating for its first key keeps the date it started.
    device_key = secrets.token_urlsafe(32)
    try:
        stored = update_user_activation_success(
            user, _hash_device_key(device_key), set_start_date=not user_data.get('start_date')
        )
    except Exception as e:
        logger.error(f"Failed to activate user {user.serial_number}: {e}")
        raise DatabaseError("activate user", e)
    if not stored:  # a concurrent Activation stored its key between our read and write
        logger.warning(f"Activation refused for {user.serial_number}: already holds a device key")
        raise DeviceKeyExistsError(user.serial_number)

    logger.info(f"User activated successfully: {user.serial_number}")
    return {"message": "User activated successfully", "device_key": device_key}


def verify_device(request: DeviceVerification) -> Dict[str, Any]:
    """Return the verdict for an Activation Code and an optional Device Key."""
    try:
        row = read_device_key_hash(request.activation_code)
    except Exception as e:
        logger.error(f"Failed to read device key for {request.activation_code}: {e}")
        raise DatabaseError("verify device", e)

    if row is None:
        return {"verdict": "unknown_activation_code"}
    stored_hash = row.get('device_key_hash')
    if not request.device_key:  # an empty key counts as not presented
        return {"verdict": "invalid_device_key" if stored_hash else "no_key"}
    if stored_hash and hmac.compare_digest(_hash_device_key(request.device_key), stored_hash):
        return {"verdict": "ok"}
    return {"verdict": "invalid_device_key"}
