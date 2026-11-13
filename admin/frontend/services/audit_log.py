# frontend/services/audit_log.py
"""Who looked at what of a branch: its logs now, its cameras later.

One JSON object per line in Config.AUDIT_LOG_PATH (mode 600): when, who, what
they did, which branch, which file or camera, and whether it worked. Never the
content itself — a log line or a video frame does not go in here.

The admin UI has no per-person login yet; it is reached from the office IPs
(gate.py). Until it does, the actor is the caller's IP. actor() is the one
place that decides it, so a login later only changes that function.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Optional

from flask import has_request_context

from config import Config

_lock = threading.Lock()


def actor() -> str:
    """Who is asking: the caller's IP until the admin UI has its own login."""
    if not has_request_context():
        return "system"
    from gate import client_ip  # gate imports Flask request state; keep it lazy
    return f"ip:{client_ip()}"


def record(action: str, client_id: Optional[str] = None, target: Optional[str] = None,
           result: str = "ok", **extra) -> None:
    """Append one audit entry. A failure to write is raised, not swallowed:
    an access that cannot be recorded must not go through silently."""
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "actor": actor(),
        "action": action,
        "client_id": client_id,
        "target": target,
        "result": result,
    }
    entry.update({k: v for k, v in extra.items() if v is not None})
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    path = Config.AUDIT_LOG_PATH
    with _lock:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
