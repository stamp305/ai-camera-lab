"""Files that only matter while the app runs: the pid file and the startup window's status.

They used to sit in ~/Library/Application Support/AICamera, where a shop could
open and edit them. They live in this user's private temporary folder now
(/var/folders/…/T/, the folder macOS gives each user — not shown in Finder,
readable by no other account, emptied on restart, which is fine for both).
"""
import ctypes
import ctypes.util
import os

_CS_DARWIN_USER_TEMP_DIR = 65537


def _user_temp_dir():
    """The same folder for every process of this user, whatever TMPDIR it was started with."""
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"))
        size = libc.confstr(_CS_DARWIN_USER_TEMP_DIR, None, 0)
        if size > 0:
            buf = ctypes.create_string_buffer(size)
            libc.confstr(_CS_DARWIN_USER_TEMP_DIR, buf, size)
            if buf.value:
                return buf.value.decode()
    except Exception:
        pass
    return os.environ.get("TMPDIR") or "/tmp"


RUN_DIR = os.path.join(_user_temp_dir(), "com.advice.aicamera")
PID_FILE = os.path.join(RUN_DIR, "aicamera.pid")
STARTUP_STATUS_FILE = os.path.join(RUN_DIR, "startup_status")
# What the menu bar icon shows (client_logic writes it every few seconds).
APP_STATUS_FILE = os.path.join(RUN_DIR, "app_status")
# Left by the menu bar's Restart just before it restarts the app: every open page
# sees it on its next poll and shows the restarting notice (frontend_web).
RESTART_NOTICE_FILE = os.path.join(RUN_DIR, "restart_notice")
RESTART_NOTICE_MAX_AGE = 300

# Where older versions kept them. An older updater still reads LEGACY_PID_FILE
# for one upgrade, so bootstrap writes it briefly (see bootstrap._write_pid).
_SUPPORT_DIR = os.path.expanduser("~/Library/Application Support/AICamera")
LEGACY_PID_FILE = os.path.join(_SUPPORT_DIR, "aicamera.pid")
LEGACY_STATUS_FILE = os.path.join(_SUPPORT_DIR, ".startup_status")


def ensure_run_dir():
    os.makedirs(RUN_DIR, mode=0o700, exist_ok=True)
