"""This machine's identity, kept in one encrypted file.

Replaces two plaintext files in App Support:
  - client_config.json  (client_id, mac, start_date, activateID)
  - device_info.json    (serial_number, mac_address)

and records whether the machine is activated. Everything is one JSON object,
Fernet-encrypted with the machine key (serial + MAC), written to STORE_NAME.
Copied to another machine it does not open; edited by hand it does not
parse — which is the point: "activated" used to be a DB flag and a plaintext
activateID, and flipping either one walked past activation without ever
asking the admin.

The Device Key itself is NOT in this file: the admin contract (§7) requires
the macOS Keychain — keyring.set_password("AICamera", "device_key", key).

The key cannot come from the file (the serial inside it would be needed to
open it), so it is always derived from the hardware: system_profiler for the
serial, mac_utils for the MAC. The derivation is the same one bootstrap uses
for .env and crypto_util uses for camera passwords.

Several processes share the file (supervisor, data_service, frontend_web), so
every write is a read-modify-write under an flock, then an atomic replace.

Never log a value from this module: device_key is a secret.

Keychain access control is tied to the app's code signature: every build must
be signed with the same certificate, or the new version cannot read the key
and every branch ends up needing the admin to clear it (contract §7).
"""
from __future__ import annotations

import base64
import contextlib
import fcntl
import functools
import hashlib
import json
import logging
import os
import re
import socket
import subprocess
import time
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

from cryptography.fernet import Fernet, InvalidToken

_log = logging.getLogger(__name__)

_DEFAULT_APP_SUPPORT = os.path.expanduser("~/Library/Application Support/AICamera")


def app_support_dir() -> str:
    return os.environ.get("AICAMERA_APP_SUPPORT_DIR") or _DEFAULT_APP_SUPPORT


# Deliberately says nothing about what is inside.
STORE_NAME = ".lcache"
LEGACY_CONFIG_NAME = "client_config.json"
LEGACY_DEVICE_INFO_NAME = "device_info.json"

KEYCHAIN_SERVICE = "AICamera"
KEYCHAIN_ACCOUNT = "device_key"

# Fields callers may read through load().
PUBLIC_FIELDS = ("client_id", "mac", "serial_number", "activateID", "start_date", "activated")

# An activate answer of 401/404/409 (or socket unknown_activation_code) means a
# person has to act on the admin side. Automatic retries stay off this long, so
# a lost key cannot loop 401 -> activate -> 409 into the server's 429 lockout.
HALT_SECONDS = 24 * 3600


def store_path() -> str:
    return os.path.join(app_support_dir(), STORE_NAME)


# --------------------------------------------------------------------------------------
# Hardware identity + machine key
# --------------------------------------------------------------------------------------
@functools.lru_cache(maxsize=1)
def hardware_serial() -> str:
    """Serial from system_profiler; hostname when there is none.

    Same answer as bootstrap._get_serial_number (it greps the same line and
    falls back to hostname), so the key matches the one .env was encrypted
    with. Retried because a transient failure here would derive the wrong key
    for the whole life of the process.
    """
    for _ in range(3):
        try:
            out = subprocess.check_output(
                ["system_profiler", "SPHardwareDataType"],
                text=True, stderr=subprocess.DEVNULL, timeout=15)
            m = re.search(r"Serial Number \(system\):\s+(\S+)", out)
            if m:
                return m.group(1)
            break  # answered, just no serial (VM) — retrying won't help
        except Exception:
            time.sleep(0.5)
    try:
        return subprocess.check_output(["hostname"], text=True).strip() or "Unknown"
    except Exception:
        return "Unknown"


@functools.lru_cache(maxsize=1)
def hardware_mac() -> str:
    from mac_utils import get_canonical_mac_address
    return get_canonical_mac_address()


def derive_machine_key(serial: Optional[str] = None, mac: Optional[str] = None) -> bytes:
    """PBKDF2 over serial + MAC — must stay byte-identical to bootstrap._derive_machine_key."""
    serial = serial if serial is not None else hardware_serial()
    mac = mac if mac is not None else hardware_mac()
    raw = serial.encode("utf-8") + mac.encode("utf-8") + b"_aicamera_machine_salt_v1"
    salt = hashlib.sha256(b"salt_v2_" + serial.encode("utf-8")).digest()
    derived = hashlib.pbkdf2_hmac("sha256", raw, salt, 100_000)
    return base64.urlsafe_b64encode(derived[:32])


@functools.lru_cache(maxsize=1)
def _cipher() -> Fernet:
    return Fernet(derive_machine_key())


# --------------------------------------------------------------------------------------
# File I/O
# --------------------------------------------------------------------------------------
class _Unreadable(Exception):
    """The store exists but this machine's key does not open it."""


@contextlib.contextmanager
def _locked():
    os.makedirs(app_support_dir(), exist_ok=True)
    with open(store_path() + ".lock", "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _read_raw() -> Optional[dict]:
    try:
        with open(store_path(), "rb") as f:
            token = f.read()
    except FileNotFoundError:
        return None
    try:
        data = json.loads(_cipher().decrypt(token.strip()).decode("utf-8"))
    except (InvalidToken, ValueError) as e:
        raise _Unreadable() from e
    return data if isinstance(data, dict) else {}


def _write_raw(data: dict) -> None:
    path = store_path()
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(_cipher().encrypt(json.dumps(data).encode("utf-8")))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _read_legacy_json(name: str) -> dict:
    try:
        with open(os.path.join(app_support_dir(), name)) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _fresh_state() -> dict:
    """First boot on this machine, or migration from the two plaintext files."""
    legacy = _read_legacy_json(LEGACY_CONFIG_NAME)
    activate_id = str(legacy.get("activateID") or "").strip()
    try:
        hostname = subprocess.check_output(["hostname"], text=True).strip()
    except Exception:
        hostname = socket.gethostname() or "Unknown"
    return {
        "client_id": legacy.get("client_id") or hostname,
        "mac": hardware_mac(),
        "serial_number": hardware_serial(),
        "activateID": activate_id,
        "start_date": legacy.get("start_date") or datetime.now().isoformat(),
        # The old supervisor treated "has an activateID" as activated. Keep
        # that for branches migrating in, so none of them drops back to the
        # activation page on the update that ships this file.
        "activated": bool(activate_id),
        "key_halt": None,
    }


def _retire_legacy_files() -> None:
    """Delete the plaintext files once their contents are safely in the store."""
    for name in (LEGACY_CONFIG_NAME, LEGACY_DEVICE_INFO_NAME):
        path = os.path.join(app_support_dir(), name)
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError as e:
                _log.warning(f"[IDENTITY] Could not remove {name}: {e}")


def _load_locked() -> dict:
    """Current state; creates/migrates the store if it does not exist yet. Caller holds the lock."""
    data = _read_raw()
    if data is None:
        data = _fresh_state()
        _write_raw(data)
        _retire_legacy_files()
        _log.info("[IDENTITY] Identity store created" +
                  (" (migrated activation)" if data["activateID"] else ""))
    return data


def _load() -> dict:
    with _locked():
        try:
            return _load_locked()
        except _Unreadable:
            # Serial or MAC changed (board swap, restored backup). Reading must
            # not destroy anything — a write is what quarantines it (update()).
            _log.error("[IDENTITY] Identity store cannot be opened with this machine's key "
                       "(serial/MAC changed?). Treating the machine as not activated.")
            return {}


# --------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------
def load() -> dict:
    """Public fields only (no device_key)."""
    data = _load()
    return {k: data.get(k) for k in PUBLIC_FIELDS}


def update(**fields) -> dict:
    """Merge ``fields`` into the store. Returns the public view."""
    with _locked():
        try:
            data = _load_locked()
        except _Unreadable:
            quarantined = f"{store_path()}.unreadable-{int(time.time())}"
            os.replace(store_path(), quarantined)
            _log.error(f"[IDENTITY] Moved unreadable identity store aside to {os.path.basename(quarantined)}")
            data = _load_locked()
        data.update(fields)
        _write_raw(data)
    return {k: data.get(k) for k in PUBLIC_FIELDS}


def activation_code() -> str:
    return str(_load().get("activateID") or "")


def is_activated() -> bool:
    data = _load()
    return bool(data.get("activated")) and bool(str(data.get("activateID") or "").strip())


def _keychain_service() -> str:
    # Tests point this elsewhere so they never touch a real branch key.
    return os.environ.get("AICAMERA_KEYCHAIN_SERVICE") or KEYCHAIN_SERVICE


def device_key() -> Optional[str]:
    """The Device Key from the Keychain; None when there is none (or it can't be read)."""
    try:
        import keyring
        return keyring.get_password(_keychain_service(), KEYCHAIN_ACCOUNT) or None
    except Exception as e:
        _log.error(f"[IDENTITY] Could not read Device Key from Keychain: {type(e).__name__}")
        return None


def save_device_key(key: str) -> None:
    import keyring
    keyring.set_password(_keychain_service(), KEYCHAIN_ACCOUNT, key)


def clear_device_key() -> None:
    try:
        import keyring
        from keyring.errors import PasswordDeleteError
        try:
            keyring.delete_password(_keychain_service(), KEYCHAIN_ACCOUNT)
        except PasswordDeleteError:
            pass  # nothing stored
    except Exception as e:
        _log.error(f"[IDENTITY] Could not delete Device Key from Keychain: {type(e).__name__}")


def record_activation(code: str, key: Optional[str]) -> None:
    """The admin accepted ``code`` (HTTP 200). Save the key — first — and the code.

    The key goes to the Keychain before anything else: the server never sends
    it again. A server that predates Device Keys answers 200 without one; the
    machine is still activated and picks a key up on a later boot.
    """
    if key:
        try:
            save_device_key(key)
        except Exception as e:
            # The server has accepted the code either way, so the machine is
            # activated; the missing key surfaces on the next boot's key request
            # (409 -> "contact admin") instead of trapping the branch on the
            # activation page.
            _log.error(f"[IDENTITY] Could not save Device Key to Keychain: {type(e).__name__}")
    update(activateID=code.strip(), activated=True, key_halt=None)


def registered_mac() -> Optional[str]:
    """The MAC the admin last accepted on activation; None until it has accepted one.

    Not always hardware_mac(): an older install registered the card it was
    online through while it was set up, which may be the Wi-Fi card. Only for
    talking to the admin — the machine key stays on hardware_mac().
    """
    return _load().get("registered_mac") or None


def remember_registered_mac(mac: str) -> None:
    if registered_mac() != mac:
        update(registered_mac=mac)


def halt_key_requests(reason: str) -> None:
    update(key_halt={"reason": reason, "at": time.time()})


def key_requests_halted() -> Optional[str]:
    """Reason automatic key requests are paused, or None when they may go ahead."""
    halt = _load().get("key_halt") or None
    if not halt:
        return None
    if time.time() - float(halt.get("at") or 0) >= HALT_SECONDS:
        return None
    return str(halt.get("reason") or "halted")


def _admin_hosts() -> set:
    hosts = {"app.advice.th"}
    for name in ("SOCKET_SERVER_HOST", "API_SERVER_HOST"):
        raw = os.getenv(name) or ""
        host = urlparse(raw if "://" in raw else f"http://{raw}").hostname
        if host:
            hosts.add(host.lower())
    return hosts


def admin_auth_headers(url: Optional[str] = None) -> dict:
    """X-Activation-Code + Authorization for a request to the admin server.

    Empty while there is no key yet (the admin falls back to its IP whitelist
    during the transition). With ``url``, headers are only returned when it
    points at the admin host — the build-farm upload URL arrives in a socket
    payload, and the bearer must not follow it anywhere else.
    """
    if url is not None:
        host = (urlparse(url).hostname or "").lower()
        if host not in _admin_hosts():
            return {}
    code = activation_code()
    key = device_key() if code else None
    if not key or not code:
        return {}
    return {"X-Activation-Code": code, "Authorization": f"Bearer {key}"}
