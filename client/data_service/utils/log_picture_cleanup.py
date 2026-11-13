"""Delete pictures in data_service/static/logs older than 7 days.

Pictures of people saved by the old /logs write endpoints live here; nothing
else cleans this folder (ai_camera_db cleans only its own).
"""
import os
import time

from data_service.configs.static_path_config import STATIC_DIR

LOGS_DIR = os.path.join(STATIC_DIR, "logs")
MAX_AGE_SECONDS = 7 * 24 * 60 * 60  # 7 days
INTERVAL_SECONDS = 24 * 60 * 60  # once a day


def cleanup_old_log_images() -> int:
    """Delete log images older than 7 days. Returns number of files removed."""
    if not os.path.isdir(LOGS_DIR):
        return 0
    now = time.time()
    removed = 0
    for log_type_dir in os.listdir(LOGS_DIR):
        subdir = os.path.join(LOGS_DIR, log_type_dir)
        if not os.path.isdir(subdir):
            continue
        for filename in os.listdir(subdir):
            file_path = os.path.join(subdir, filename)
            if not os.path.isfile(file_path):
                continue
            if now - os.path.getmtime(file_path) > MAX_AGE_SECONDS:
                os.remove(file_path)
                removed += 1
    print(f"🧹 Log image cleanup: removed {removed} file(s) older than 7 days")
    return removed
