"""Main Client unit tests: plain pytest, run from a git checkout (no app bundle).

Everything here must happen before frontend_web or port_store is imported.
"""
import os
import socket
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# The real loader is built per machine and holds secrets; it is never in git.
_secure_env = types.ModuleType("data_service.configs.secure_env")
_secure_env.load_secure_env = lambda: None
sys.modules["data_service.configs.secure_env"] = _secure_env

# Set, so port_store never goes looking for the compiled env; DB_PORT unset = 3306.
os.environ["DB_USER"] = "test"
os.environ["DB_NAME"] = "test"
os.environ.pop("DB_PORT", None)

# Tests reach loopback only.
_real_connect = socket.socket.connect


def _loopback_only(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6) and address[0] not in ("127.0.0.1", "::1", "localhost"):
        raise OSError(f"test tried to leave loopback: {address}")
    return _real_connect(self, address)


socket.socket.connect = _loopback_only
