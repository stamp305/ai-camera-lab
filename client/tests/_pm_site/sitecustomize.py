"""Run at start-up by every process of the seam-4 harness (driver and spawned services).

Only on PYTHONPATH when test_process_manager.py starts its driver.
"""
import os
import socket
import sys
import types

# The real loader is built per machine and holds secrets; each spawned child imports client_logic.
_secure_env = types.ModuleType("data_service.configs.secure_env")
_secure_env.load_secure_env = lambda: None
sys.modules["data_service.configs.secure_env"] = _secure_env

# Loopback (and the database's unix socket) only.
_real_connect = socket.socket.connect


def _loopback_only(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6) and address[0] not in ("127.0.0.1", "::1", "localhost"):
        raise OSError(f"test tried to leave loopback: {address}")
    return _real_connect(self, address)


socket.socket.connect = _loopback_only

# The port page's event file lives in the user's real temp folder; keep test lines out of it.
import runtime_paths  # noqa: E402

runtime_paths.RUN_DIR = os.environ["PM_TEST_RUN_DIR"]
