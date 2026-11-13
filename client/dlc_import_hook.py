"""Import the delivered services (managed_services/) from encrypted files.

The admin sends every service .py Fernet-encrypted. The client used to decrypt
each one and write the plain source to ~/Library/Application Support/AICamera/
managed_services/ — every service readable by anyone — and Python then cached
compiled bytecode next to them in __pycache__/. Now the .app keeps the files as
they arrive (still encrypted) and this hook decrypts them in memory at import
time, never writing bytecode.

It has to be active in EVERY process that imports a service module, including
multiprocessing "spawn" children: those re-import their target's module by name
before any of their own code runs (consumer_service's detect_pool workers). So
the .app installs it twice:
- client_logic calls install() before the first service import;
- build_mac_app.sh puts a copy of this file and a one-line .pth into the bundled
  python_env's site-packages, so every interpreter started from it installs the
  hook at startup.

The key is the admin's DLC key (STATIC_LEGACY_KEY, from the environment or the
compiled _secrets module), looked up the first time an encrypted file is met —
not the machine key, which is derived from the serial number and MAC address and
would leave every service unreadable after a network card change.
"""
from __future__ import annotations

import importlib.machinery
import os
import sys

# AICAMERA_MANAGED_PATH only for tests: the real folder is fixed.
MANAGED_PATH = os.path.realpath(os.environ.get("AICAMERA_MANAGED_PATH")
                                or os.path.expanduser("~/Library/Application Support/AICamera/managed_services"))
FERNET_PREFIX = b"gAAAA"

_cipher = None


def _get_cipher():
    global _cipher
    if _cipher is None:
        from cryptography.fernet import Fernet
        key = os.environ.get("STATIC_LEGACY_KEY", "")
        if not key:
            try:
                import _secrets
                key = getattr(_secrets, "STATIC_LEGACY_KEY", "") or ""
            except ImportError:
                key = ""
        if isinstance(key, str):
            key = key.strip().encode()
        if not key:
            raise ImportError("dlc_import_hook: no DLC key (STATIC_LEGACY_KEY) to decrypt managed services")
        _cipher = Fernet(key)
    return _cipher


def decrypt_if_needed(data: bytes) -> bytes:
    """Plain bytes pass through; a Fernet token is decrypted."""
    if data[:len(FERNET_PREFIX)] == FERNET_PREFIX:
        return _get_cipher().decrypt(data)
    return data


def is_managed(path: str | None) -> bool:
    if not path:
        return False
    real = os.path.realpath(path)
    return real == MANAGED_PATH or real.startswith(MANAGED_PATH + os.sep)


class DecryptingSourceLoader(importlib.machinery.SourceFileLoader):
    """SourceFileLoader that decrypts the source and never reads or writes bytecode."""

    def get_data(self, path):
        if path != self.path:
            # The only other file SourceLoader asks for is the cached .pyc. Saying it
            # does not exist makes it compile from the decrypted source instead.
            raise OSError(f"no bytecode cache for managed module {self.name}")
        with open(path, "rb") as f:
            return decrypt_if_needed(f.read())

    def set_data(self, path, data, *, _mode=0o666):
        return  # never write __pycache__/*.pyc for a managed module


class _ManagedServiceFinder:
    """Hands managed_services/*.py to DecryptingSourceLoader; everything else is untouched."""

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "dlc_local" and "dlc_local" not in sys.modules:
            # The services import each other through this alias. client_logic
            # registers it in the main process; a spawn child has to get it here.
            spec = importlib.machinery.ModuleSpec("dlc_local", None, is_package=True)
            spec.submodule_search_locations = [MANAGED_PATH]
            return spec
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is None or not spec.origin or not spec.origin.endswith(".py") or not is_managed(spec.origin):
            return None
        spec.loader = DecryptingSourceLoader(fullname, spec.origin)
        return spec

    def invalidate_caches(self):
        importlib.machinery.PathFinder.invalidate_caches()


def install() -> None:
    """Put the finder first on sys.meta_path, once per process."""
    if any(isinstance(f, _ManagedServiceFinder) or type(f).__name__ == "_ManagedServiceFinder"
           for f in sys.meta_path):
        return
    sys.meta_path.insert(0, _ManagedServiceFinder())


def spec_from_file(name: str, path: str):
    """importlib.util.spec_from_file_location, decrypting when the file is managed."""
    import importlib.util
    loader = DecryptingSourceLoader(name, path) if is_managed(path) else None
    return importlib.util.spec_from_file_location(name, path, loader=loader)


# Imported from the bundled python_env's .pth at interpreter start.
if os.environ.get("AICAMERA_DLC_HOOK") == "1" or "/AICamera.app/" in (__file__ or ""):
    install()
