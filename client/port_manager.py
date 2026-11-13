"""Restart coordination for the client supervisor.

This module used to own `client_port_config.py` — reading it, rewriting it
with `ast`, and validating what the web UI sent. Ports are rows in the
store's database now (see `port_store`), so all that is gone and what is
left is the one thing that was never about the file: the marker the running
client watches to know it should restart itself.
"""
import os

# The marker used to live next to the client code, but a .pkg install puts that
# under /Applications where the app cannot write. Machine-local runtime state
# belongs in App Support; client_root is kept in the signature so existing
# callers need no change.
_APP_SUPPORT_DIR = os.path.expanduser('~/Library/Application Support/AICamera')


def restart_marker_path(client_root=None):
    os.makedirs(_APP_SUPPORT_DIR, exist_ok=True)
    return os.path.join(_APP_SUPPORT_DIR, ".restart_required")


def request_restart(client_root, marker="requested"):
    """Create the restart marker the client watches. Returns the path.

    ``marker="PORT_CHANGE"`` is what client_logic.py's watcher checks for to
    launch updater.py in --port-change-restart mode instead of --restart-only
    — the difference being whether AICAMERA_RESTARTED is set afterward, which
    is what tells menubar_app.py's launch_browser() to open a fresh browser
    window once the frontend is back up. Any other marker text restarts
    silently with no window reopening at all.
    """
    path = restart_marker_path(client_root)
    with open(path, "w", encoding="utf-8") as f:
        f.write(marker + "\n")
    return path


def consume_restart_request(client_root):
    """Remove the marker if present and return True (a restart is needed)."""
    path = restart_marker_path(client_root)
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass
        return True
    return False
