"""What this branch is ready for, told to the admin so the Alitech portal offers only ready branches.

    live          live_agent loaded and ffmpeg found (CCTV Streaming)
    alerts        monitor_service answers /intrusion and trial mode is off, so person-after-hours
                  and camera-check alerts really reach the admin (Alert & Log)
    siren         the siren setup service answers on this Mac (Hardware Integration)
    siren_paired  that service has at least one siren saved

Only yes/no values leave the machine: no device ids, names, IPs or paths.
Sent in identify and as `branch_features` when something changed (checked every 5 min).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import urllib.request

logger = logging.getLogger(__name__)

APP_SUPPORT = os.path.expanduser("~/Library/Application Support/AICamera")
SHADOW_FLAG = os.path.join(APP_SUPPORT, "log", "monitor_shadow")
SIREN_PORT = 8767
CHECK_EVERY = 300
TIMEOUT = 4


def _get(url: str, headers: dict | None = None):
    # Loopback only: the handler never follows redirects off this Mac.
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        if resp.status != 200:
            return None
        return resp.read(65536)


def _monitor_port() -> int:
    try:
        import port_store
        return int(port_store.get_port("monitor_service", 8011))
    except Exception:
        return 8011


def _alerts() -> bool:
    if os.path.exists(SHADOW_FLAG):
        return False
    try:
        return _get(f"http://127.0.0.1:{_monitor_port()}/intrusion") is not None
    except Exception:
        return False


def _siren() -> tuple[bool, bool]:
    try:
        body = _get(f"http://127.0.0.1:{SIREN_PORT}/api/state", {"Host": f"127.0.0.1:{SIREN_PORT}"})
    except Exception:
        return False, False
    if body is None:
        return False, False
    try:
        saved = json.loads(body).get("saved") or []
    except (ValueError, AttributeError):
        return True, False
    return True, bool(saved)


def compute(live_agent) -> dict:
    live = False
    if live_agent is not None:
        try:
            live = bool(live_agent._tool("ffmpeg"))
        except Exception:
            live = False
    siren, paired = _siren()
    return {"live": live, "alerts": _alerts(), "siren": siren, "siren_paired": paired}


_last: dict | None = None
_started = False
_wake = threading.Event()


def current() -> dict:
    return dict(_last) if _last else {"live": False, "alerts": False, "siren": False, "siren_paired": False}


def start_reporter(live_agent, emit, connected) -> None:
    """Check now and every CHECK_EVERY seconds; emit only when the answer changed (or on wake())."""
    global _started
    if _started:
        wake()
        return
    _started = True

    def loop():
        global _last
        force = True
        while True:
            try:
                now = compute(live_agent)
                if (force or now != _last) and connected():
                    emit(now)
                    force = False
                _last = now
            except Exception as e:  # never let a check kill the reporter
                logger.warning(f"[FEATURES] check failed: {type(e).__name__}")
            if _wake.wait(CHECK_EVERY):
                _wake.clear()
                force = True

    threading.Thread(target=loop, name="branch-features", daemon=True).start()


def wake() -> None:
    """Report again soon (after a reconnect the admin has forgotten what we said)."""
    _wake.set()
