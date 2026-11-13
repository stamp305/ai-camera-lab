"""Per-machine crypto helpers for data_service.

The camera RTSP/HTTP credentials are stored encrypted at rest using a
per-machine Fernet key derived from the device serial + NIC MAC. The same
derivation lives in the bootstrap layer (``bootstrap._derive_machine_key``);
this module mirrors it so data_service can decrypt what the device owns
without shipping any shared/static key material.

Legacy plaintext rows (stored before encryption existed) are supported on
read: a value that is not a valid Fernet token is returned as-is.
"""
import functools
import logging

from cryptography.fernet import Fernet, InvalidToken

_log = logging.getLogger(__name__)

def derive_machine_key() -> bytes:
    """Same PBKDF2 over serial + MAC that bootstrap uses for .env.

    Lives in device_identity now, which reads the serial from the hardware:
    the device_info.json this used to read first no longer exists (its values
    moved into the encrypted identity store, which needs this key to open).
    """
    from device_identity import derive_machine_key as _derive
    return _derive()


@functools.lru_cache(maxsize=1)
def _machine_cipher() -> Fernet:
    return Fernet(derive_machine_key())


def encrypt_password(plaintext: str) -> str:
    if not plaintext:
        return plaintext
    return _machine_cipher().encrypt(plaintext.encode('utf-8')).decode('utf-8')


def decrypt_password(token: str) -> str:
    if not token:
        return token
    if not token.startswith('gAAAA'):
        # Legacy plaintext row (stored before encryption was introduced).
        return token
    try:
        return _machine_cipher().decrypt(token.encode('utf-8')).decode('utf-8')
    except (InvalidToken, ValueError):
        # The machine key is derived from serial + MAC, so a value encrypted on
        # this machine only fails here when one of those changed (restored
        # backup, replaced board). Returning the token
        # keeps the old behaviour, but silently: the ciphertext then goes into
        # the RTSP URL and every camera "stops working" for no visible reason.
        _log.warning(
            "Camera password could not be decrypted — the machine key no "
            "longer matches the one it was stored with (serial/MAC changed?). "
            "Cameras will fail to connect until the password is re-entered.")
        return token
