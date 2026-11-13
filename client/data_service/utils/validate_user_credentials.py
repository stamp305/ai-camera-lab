import logging
from typing import Optional, Dict, Any

import requests
from requests import RequestException
from passlib.hash import bcrypt

import device_identity
from mac_utils import card_macs

from data_service.configs.client_api_config import TRIGGER_APIS
from data_service.configs.server_api_config import AUTH_APIS
from data_service.schemas.user_schemas import Location
from data_service.db.crud.account_crud import (
    read_password_by_username,
    update_activated_status
)



REQUEST_TIMEOUT = 5
_log = logging.getLogger(__name__)


# ---------- System information ----------
# Same serial/MAC the machine key is derived from (device_identity), so what
# the admin registered and what opens the identity store never disagree.
def _get_serial_number() -> Optional[str]:
    return device_identity.hardware_serial()


def _get_mac_address() -> Optional[str]:
    return device_identity.hardware_mac()


def _clean_upstream_error(text: str, status: int) -> str:
    """Turn an upstream error body into something a store operator can act on.

    The auth service answers a bad activation code with 403 and a full Werkzeug
    HTML page (Cloudflare then injects its bot-detection script into it). Dumping
    that into a toast told the operator nothing and read like a network block,
    which is exactly the wrong diagnosis.
    """
    body = (text or "").strip()
    if body.startswith("<") or "<html" in body[:200].lower():
        if status == 403:
            return "เซิร์ฟเวอร์ปฏิเสธรหัสนี้ (403) — รหัสไม่ถูกต้อง หมดอายุ หรือถูกใช้ไปแล้ว"
        return f"เซิร์ฟเวอร์ตอบกลับผิดพลาด (HTTP {status})"
    return body[:300]


# ---------- Validation functions ----------
def validate_username_password(username: str, password: str) -> Optional[Dict[str, Any] | bool]:
    """
    Validate username and password against stored hash.
    Return:
        None -> Username not found
        False -> Password mismatch
        True -> Successful validation
    """
    password_hash = read_password_by_username(username)
    if password_hash is None:
        return None
    return bcrypt.verify(password, password_hash)


# Answers after which the admin has to act (contract §5) — never retried automatically.
# 403 is deliberately not here: the contract lists it under "wait and retry", and
# the admin that predates Device Keys answers 403 to every already-used code —
# halting on it would hold every branch's key back a day after the new admin ships.
_HALT_STATUSES = {401: "rejected", 404: "unknown_serial", 409: "device_key_exists"}


# Every refusal counts toward the admin's lockout (5 per hour per serial), so
# one activation never offers more MACs than this.
_MAX_MAC_TRIES = 3


def _activation_macs() -> list:
    """MACs to offer the admin, the likeliest first.

    The admin compares the MAC it registered exactly. Older installs registered
    whatever getmac returned while the branch was set up — the Wi-Fi card when it
    was online through Wi-Fi then — so the Ethernet card's MAC alone is refused
    there. The one the admin last accepted goes first, then this machine's
    canonical card, then its other burned-in cards.
    """
    macs = []
    for mac in (device_identity.registered_mac(), _get_mac_address(), *card_macs()):
        if mac and mac not in macs:
            macs.append(mac)
    return macs[:_MAX_MAC_TRIES]


def _is_mac_mismatch(response) -> bool:
    return response.status_code == 401 and "mac" in _error_code(response).lower()


def _post_activate_once(activation_code: str, mac_address: str):
    payload = {
        "activation_code": activation_code,
        "serial_number": _get_serial_number(),
        "mac_address": mac_address,
    }
    try:
        return requests.post(AUTH_APIS["activate"], json=payload, timeout=REQUEST_TIMEOUT, verify=False), None
    except RequestException as e:
        return None, e


def _post_activate(activation_code: str):
    """POST /api/auth/activate. Returns (response or None, network error or None, MAC sent last).

    Moves on to the next MAC only when the admin names the MAC as what is wrong.
    Any other answer ends the attempt as it is. The caller remembers the accepted
    MAC (_remember_mac) only after it has saved the key.

    Neither the request nor the response is logged: a 200 carries the device_key.
    """
    response, net_error, mac = None, None, None
    for mac in _activation_macs():
        response, net_error = _post_activate_once(activation_code, mac)
        if response is None or response.status_code == 200 or not _is_mac_mismatch(response):
            break
    return response, net_error, mac


def _remember_mac(mac) -> None:
    # Runs after the key is saved: a failed write here never costs the key or the activation.
    try:
        device_identity.remember_registered_mac(mac)
    except Exception as e:
        _log.error(f"[ACTIVATE] Could not remember the accepted MAC: {type(e).__name__}")


def _error_code(response) -> str:
    try:
        body = response.json()
        return str(body.get("error") or "") if isinstance(body, dict) else ""
    except ValueError:
        return ""


def _device_key_from(response) -> Optional[str]:
    try:
        body = response.json() if response.content else {}
    except ValueError:
        return None
    key = body.get("device_key") if isinstance(body, dict) else None
    return key if isinstance(key, str) and key else None


def validate_activation_code(activation_code: str) -> Optional[Dict[str, Any] | bool]:
    """Activate from the UI: validate the code with the auth service and, on
    success, store it with the Device Key the server hands back.

    Return contract (mirrors validate_username_password):
        dict  -> success payload {message}
        None  -> the admin has to act (401 / 403 / 404 / 409)
        False -> try again later (network / timeout / 429 / 5xx)
    """
    activation_code = activation_code.strip()
    response, net_error, mac = _post_activate(activation_code)
    if response is None:
        return False, f"ติดต่อ {AUTH_APIS['activate']} ไม่สำเร็จ: {net_error}"

    if response.status_code == 200:
        # Saved before anything else: the server never sends this key again.
        device_identity.record_activation(activation_code, _device_key_from(response))
        _remember_mac(mac)
        try:
            update_activated_status(is_activated = True)
            requests.post(TRIGGER_APIS["activate_success"], json={"activateID": activation_code}, timeout=5)
        except RequestException as e:
            print(f"Activation success callback failed (non-fatal): {e}")
        return {"message": "Account activated successfully"}, None

    if response.status_code == 409:
        return None, "เครื่องนี้เคย activate และได้รับ Device Key ไปแล้ว — กรุณาติดต่อแอดมินให้ล้าง key ก่อน activate ใหม่"
    if response.status_code == 404:
        return None, "เซิร์ฟเวอร์ไม่รู้จักเครื่องนี้ (serial ไม่ได้ลงทะเบียนไว้) — กรุณาติดต่อแอดมิน"
    if response.status_code in (401, 403):
        print(f"Activation rejected: status={response.status_code}")
        if response.status_code == 401:
            return None, "รหัสเปิดใช้งานหรือ MAC ของเครื่องไม่ตรงกับที่ลงทะเบียนไว้ — ตรวจสอบรหัสอีกครั้ง หรือติดต่อแอดมิน"
        return None, _clean_upstream_error(response.text, response.status_code)
    if response.status_code == 429:
        return False, "ลองผิดเกินกำหนด (5 ครั้งต่อชั่วโมง) — กรุณารอสักครู่แล้วลองใหม่"

    # Unexpected status (500, 502, ...): the server's problem, try again later.
    print(f"Unexpected activation response: status={response.status_code}")
    return False, _clean_upstream_error(response.text, response.status_code)


def refresh_device_key() -> str:
    """Get a Device Key for a machine that is activated but has none (contract §1.2).

    Covers branches activated before Device Keys existed, and the "delete key,
    activate again" step after the server rejects the key. Outcomes:
        "ok"      -> key stored
        "skipped" -> not activated, already has a key, or paused after a halt
        "halt"    -> the admin has to act; automatic requests paused for a day
        "retry"   -> network / 429 / 5xx; call again later
    """
    if not device_identity.is_activated() or device_identity.device_key():
        return "skipped"
    if device_identity.key_requests_halted():
        return "skipped"

    code = device_identity.activation_code()
    response, net_error, mac = _post_activate(code)
    if response is None:
        _log.warning(f"[ACTIVATE] Device Key request: network error ({type(net_error).__name__})")
        return "retry"
    if response.status_code == 200:
        key = _device_key_from(response)
        if key:
            device_identity.record_activation(code, key)
        _remember_mac(mac)
        if not key:
            # Server not on Device Keys yet; ask again next boot.
            _log.warning("[ACTIVATE] Device Key request: 200 without device_key (server not upgraded yet?)")
            return "retry"
        return "ok"
    reason = _HALT_STATUSES.get(response.status_code)
    if reason:
        device_identity.halt_key_requests(_error_code(response) or reason)
        _log.warning(f"[ACTIVATE] Device Key request refused: HTTP {response.status_code}")
        return "halt"
    _log.warning(f"[ACTIVATE] Device Key request failed: HTTP {response.status_code}")
    return "retry"
