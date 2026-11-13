# frontend/services/log_unseal.py
"""Open log files a branch sealed with log_seal (client/log_seal.py).

A branch holds only the public key, so it can write its logs but never read
them back. The private key lives on this server alone: a PEM file outside the
repo, named by the AICAM_LOG_SEAL_KEY environment variable, readable by its
owner only (a key file any other account can read is refused, not loaded).

Pulled files stay sealed on disk under LOGS_BASE_PATH. They are opened here,
in memory, only when someone asks to see one, and the plain text goes back in
that response and nowhere else.

File format (one record per line, see client/log_seal.py):

    #AICLOG1 <kid> <base64: ephemeral X25519 public key>     key line
    <kid> <base64: 12-byte nonce + AES-256-GCM ciphertext>    record line

The key for a record is HKDF-SHA256(ECDH(server private, ephemeral public),
salt = ephemeral public + server public, info = "aicamera-log-seal-v1"), with
the kid as associated data. Lines that are neither are shown as they are:
files from before sealing was switched on are plain text.
"""
from __future__ import annotations

import base64
import binascii
import os
import stat
import threading
from typing import Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAGIC = b"#AICLOG1"
_INFO = b"aicamera-log-seal-v1"
_HEX = frozenset(b"0123456789abcdef")
KEY_ENV = "AICAM_LOG_SEAL_KEY"

_key_lock = threading.Lock()
_private_key: Optional[X25519PrivateKey] = None


class UnsealError(Exception):
    """The server cannot open sealed logs; `code` is safe to show the viewer."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code


def _load_private_key() -> X25519PrivateKey:
    """The server's private key, loaded once. Refuses a key others can read."""
    global _private_key
    with _key_lock:
        if _private_key is not None:
            return _private_key
        path = os.environ.get(KEY_ENV, "").strip()
        if not path:
            raise UnsealError("key_not_configured", f"{KEY_ENV} is not set")
        try:
            st = os.stat(path)
        except OSError as exc:
            raise UnsealError("key_unavailable", f"cannot stat key file: {type(exc).__name__}")
        if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077:
            raise UnsealError("key_unsafe", "key file must be a regular file with mode 600")
        try:
            with open(path, "rb") as fh:
                data = fh.read()
            password = os.environ.get("AICAMERA_LOG_KEY_PASSWORD")
            key = serialization.load_pem_private_key(data, password=password.encode() if password else None)
        except Exception as exc:
            raise UnsealError("key_unavailable", f"cannot load key: {type(exc).__name__}")
        if not isinstance(key, X25519PrivateKey):
            raise UnsealError("key_unavailable", "key is not an X25519 private key")
        _private_key = key
        return key


def key_status() -> str:
    """'ok', or the UnsealError code saying why sealed logs cannot be opened."""
    try:
        _load_private_key()
        return "ok"
    except UnsealError as exc:
        return exc.code


def is_sealed(head: bytes) -> bool:
    """True when a file's first bytes show log_seal wrote it."""
    return head.startswith(MAGIC + b" ")


def unseal(raw: bytes) -> bytes:
    """Plain bytes of a whole sealed file. Damaged lines become a marker line."""
    key = _load_private_key()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    sessions: dict[bytes, AESGCM] = {}
    out = []
    for line in raw.splitlines(keepends=True):
        if line.startswith(MAGIC + b" "):
            try:
                _, kid, pub_b64 = line.split()
                ephemeral = base64.b64decode(pub_b64, validate=True)
                shared = key.exchange(X25519PublicKey.from_public_bytes(ephemeral))
                aes_key = HKDF(algorithm=hashes.SHA256(), length=32, salt=ephemeral + public,
                               info=_INFO).derive(shared)
                sessions[kid] = AESGCM(aes_key)
            except Exception:
                out.append(b"[unreadable key line]\n")
            continue
        kid, sep, body = line.partition(b" ")
        if not sep or len(kid) != 16 or not set(kid) <= _HEX:
            out.append(line)  # never sealed
            continue
        aead = sessions.get(kid)
        if aead is None:
            out.append(b"[record without its key line]\n")
            continue
        try:
            blob = base64.b64decode(body.strip(), validate=True)
            out.append(aead.decrypt(blob[:12], blob[12:], kid))
        except (InvalidTag, binascii.Error, ValueError):
            out.append(b"[record does not open with this key, or is damaged]\n")
    return b"".join(out)
