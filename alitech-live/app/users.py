"""Portal accounts for the lab: a small JSON file, scrypt-hashed passwords.

PDPA: only the e-mail, a display name and the role are kept; the audit log names a
user by id. The file is 0600 under data/ (gitignored). Production uses the real
Alitech users table; this exists so the lab login behaves like it.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
from pathlib import Path
from typing import Optional

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
USERS_FILE = DATA_DIR / "users.json"
_lock = threading.Lock()
_N, _R, _P = 2 ** 15, 8, 1
PASSWORD_MIN = 12


def _load() -> list[dict]:
    if not USERS_FILE.exists():
        return []
    return json.loads(USERS_FILE.read_text("utf-8"))


def _save(users: list[dict]) -> None:
    DATA_DIR.mkdir(mode=0o700, exist_ok=True)
    tmp = USERS_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=1)
    os.replace(tmp, USERS_FILE)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, maxmem=64 * 1024 * 1024)
    return f"scrypt${salt.hex()}${digest.hex()}"


def _check(password: str, stored: str) -> bool:
    try:
        _, salt_hex, digest_hex = stored.split("$")
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=_N, r=_R, p=_P,
                                maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False


# Same cost as a real check, so a wrong e-mail takes as long as a wrong password.
_DUMMY = hash_password(secrets.token_hex(16))


def authenticate(email: str, password: str) -> Optional[dict]:
    email = (email or "").strip().lower()
    with _lock:
        user = next((u for u in _load() if u["email"] == email and not u.get("disabled")), None)
    ok = _check(password or "", user["password"] if user else _DUMMY)
    return {k: v for k, v in user.items() if k != "password"} if (ok and user) else None


def get(user_id: int) -> Optional[dict]:
    with _lock:
        user = next((u for u in _load() if u["id"] == user_id and not u.get("disabled")), None)
    return {k: v for k, v in user.items() if k != "password"} if user else None


def create(email: str, password: str, name: str, role: str) -> dict:
    email = email.strip().lower()
    if role not in ("admin", "member"):
        raise ValueError("role must be admin or member")
    if len(password) < PASSWORD_MIN:
        raise ValueError(f"password must be at least {PASSWORD_MIN} characters")
    with _lock:
        users = _load()
        if any(u["email"] == email for u in users):
            raise ValueError("that e-mail already has an account")
        user = {"id": max([u["id"] for u in users] or [0]) + 1, "email": email, "name": name.strip(),
                "role": role, "password": hash_password(password)}
        users.append(user)
        _save(users)
    return {k: v for k, v in user.items() if k != "password"}
