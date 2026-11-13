from passlib.hash import bcrypt
from fastapi import APIRouter, HTTPException
import requests
import os

from data_service.schemas.user_schemas import (
    UserLogin,
    UserActivation
)
from data_service.db.crud.account_crud import (
    update_account,
    read_username_password,
    read_credentials_changed,
)
from data_service.utils.validate_user_credentials import (
    validate_username_password,
    validate_activation_code
)
from data_service.configs.server_api_config import AUTH_APIS
import device_identity

router = APIRouter()

@router.get("/client-info")
async def api_get_client_info():
    """
    Device serial number + MAC address, shown on the activation screen.
    """
    return {
        "serial_number": device_identity.hardware_serial(),
        "mac_address": device_identity.hardware_mac(),
    }

@router.post("/login")
async def api_login(user: UserLogin):
    """
    User login endpoint
    Validates username and password.
    """
    if not user.username or not user.password:
        raise HTTPException(status_code=400, detail="Username and password are required")

    result = validate_username_password(user.username, user.password)
    if result is None:
        raise HTTPException(status_code=401, detail="Invalid username")
    if result is False:
        raise HTTPException(status_code=401, detail="Invalid password")
    if result is True:
        creds_changed = read_credentials_changed()
        activated_status = device_identity.is_activated()
        print(f"[DEBUG] Login: activated={activated_status} (type={type(activated_status)}), credentials_changed={creds_changed} (type={type(creds_changed)})")
        return {"id": 1, "is_activated": activated_status, "credentials_changed": creds_changed}

@router.put("/change-username-password")
async def api_change_username_password(user: UserLogin):
    """
    Change username and password endpoint.
    Validates and updates the username and password.
    """
    if not user.username or not user.password:
        raise HTTPException(status_code=400, detail="กรุณากรอกชื่อผู้ใช้และรหัสผ่าน")

    if read_credentials_changed():
        raise HTTPException(status_code=400, detail="ไม่สามารถเปลี่ยนชื่อผู้ใช้และรหัสผ่านได้อีก")

    current_username, current_password = read_username_password()
    if user.username == current_username and bcrypt.verify(user.password, current_password):
        raise HTTPException(status_code=400, detail="ชื่อผู้ใช้และรหัสผ่านใหม่ต้องไม่เหมือนกับของเดิม")

    hashed_password = bcrypt.hash(user.password)
    try:
        result = update_account(user.username, hashed_password)
        if not result:
            raise HTTPException(status_code=500, detail="ไม่สามารถอัปเดตชื่อผู้ใช้และรหัสผ่านได้")
        return {"message": "อัปเดตชื่อผู้ใช้และรหัสผ่านเรียบร้อยแล้ว"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error updating username and password: {e}")

@router.get("/activated-status")
async def api_get_activated_status():
    """
    Get account activated status.
    """
    status = device_identity.is_activated()
    creds_changed = read_credentials_changed()
    return {"is_activated": status, "credentials_changed": creds_changed}

@router.post("/activate")
async def api_activate_account(code: UserActivation):
    """
    Account activation endpoint.
    Validates the activation code.
    """
    if not code.activation_code:
        raise HTTPException(status_code=400, detail="Activation code is required")
    
    result, error = validate_activation_code(code.activation_code)
    if result is False:
        # Carry the upstream reason through: a bare "Activation service error"
        # is indistinguishable between a wrong URL, a timeout and a 500, and
        # sent every diagnosis of this straight to the logs.
        raise HTTPException(
            status_code=502,
            # error already names what failed (URL + reason, rate limit, HTTP status).
            detail=error or "Activation service error (ติดต่อเซิร์ฟเวอร์ยืนยันสิทธิ์ไม่ได้)",
        )
    if result is None:
        raise HTTPException(status_code=401, detail=error or "Invalid activation code")
    return result