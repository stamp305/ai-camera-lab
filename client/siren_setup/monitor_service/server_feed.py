"""Stand-in for monitor_service.server_feed: siren.py only asks whether the branch is in
trial (shadow) mode, which silences every ring. Same flag file as the real one."""
import os

LOG_DIR = os.environ.get("AICAM_LOG_DIR", os.path.expanduser("~/Library/Application Support/AICamera/log"))


def shadow() -> bool:
    return os.path.exists(os.path.join(LOG_DIR, "monitor_shadow"))
