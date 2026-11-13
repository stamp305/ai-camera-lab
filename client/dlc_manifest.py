"""sha256 of every file under managed_services, of its plaintext.

Shared by client_logic.get_client_manifest() (the list the admin asks for
before an update) and tools/dlc_hash_check.py (the read-only check a technician
runs on a Branch before pulling the Main Client). It imports nothing of the
client, logs nothing and writes nothing, so the check can run on a Branch whose
client is older than this file: copy it next to the tool.

The .app keeps each delivered .py as it arrived, Fernet-encrypted with the DLC
key; a file the key opens is hashed decrypted, so both kinds of Branch hash to
the same value as the file in dlc_master.
"""
import hashlib
import os

FERNET_PREFIX = b'gAAAA'


def file_sha256(full_path, get_cipher):
    """sha256 hex of one file's plaintext. Raises OSError when it cannot be read.

    get_cipher() returns the DLC Fernet or None; it is called only for a file
    that looks encrypted. A token the key does not open is hashed as it is.
    """
    with open(full_path, 'rb') as f:
        head = f.read(len(FERNET_PREFIX))
        if head != FERNET_PREFIX:
            # Not encrypted (models among them, hundreds of MB): hashed in
            # blocks instead of read into memory whole.
            sha = hashlib.sha256(head)
            for block in iter(lambda: f.read(1024 * 1024), b''):
                sha.update(block)
            return sha.hexdigest()
        data = head + f.read()
    cipher = get_cipher()
    if cipher is not None:
        try:
            return hashlib.sha256(cipher.decrypt(data)).hexdigest()
        except Exception:
            pass  # fall back to the raw bytes
    return hashlib.sha256(data).hexdigest()


def tree_sha256(root, get_cipher, skip=None):
    """{path relative to root with '/': sha256 hex, or None when unreadable}.

    skip(path) True leaves a file out before it is read.
    """
    manifest = {}
    for dirpath, _, filenames in os.walk(root):
        for filename in filenames:
            full_path = os.path.join(dirpath, filename)
            key = os.path.relpath(full_path, root).replace(os.sep, '/')
            if skip is not None and skip(key):
                continue
            try:
                manifest[key] = file_sha256(full_path, get_cipher)
            except Exception:
                manifest[key] = None
    return manifest
