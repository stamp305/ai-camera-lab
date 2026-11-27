"""Stand-in for monitor_service.server_feed: siren.py here only asks whether the branch is in
trial (shadow) mode, which silences every ring, and never reports anything to the server."""
import os

LOG_DIR = os.environ.get("AICAM_LOG_DIR", os.path.expanduser("~/Library/Application Support/AICamera/log"))


def shadow() -> bool:
    return os.path.exists(os.path.join(LOG_DIR, "monitor_shadow"))


def send_event(*_args, **_kwargs):   # the setup page never reports to the server
    return False
