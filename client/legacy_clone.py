"""The app installed on a branch that ran the client from a git clone: carry on where it left off.

A branch set up by git clone (/Applications/advice/ai-camera-client, started at login by the
LaunchAgent com.advice.aicamera.client) keeps some of its state inside that folder, where the
app never looks. Without this, installing the app over it meant activating again, the admin
pushing every service again, and the shop uploading its floor plans again. Both share the
rest: the MariaDB server (floor plans, calibration, cameras, zones), the Keychain and
~/Library/Application Support/AICamera.

Every time the app starts (bootstrap.main, before the supervisor), and only inside the app:

- the clone is stopped and kept from starting at login: its LaunchAgent is unloaded and its
  plist renamed to <name>.replaced-by-app (rename it back to undo). Left running, both
  clients fight over the same ports, and the clone cannot read the services the app keeps
  encrypted — on the test Mac mini it restarted them for ever;
- its activation: an older clone kept client_config.json in its own folder. When App Support
  has no identity store yet, the file is copied there, and device_identity takes it in as it
  does for any branch moving off the plaintext file;
- its services: an older clone kept them in <clone>/dlc_local, a newer one already in App
  Support/managed_services. A service folder the app does not have is copied in as it is
  (the admin's DLC key, or plain text: client_logic encrypts plain files at rest). Folders
  the app already has are never touched;
- the pictures the shop uploaded (floor plans, uniforms) under <clone>/data_service/static/
  images, copied beside the app's own where a file of that name is missing.

Nothing in the clone is deleted or changed, other than the plist's name. Log lines say what
was carried, never a path or a file name (log_redact / no paths in shop-readable logs).
"""
import glob
import json
import logging
import os
import plistlib
import shutil
import signal
import subprocess
import time

logger = logging.getLogger(__name__)

LAUNCHD_LABEL = "com.advice.aicamera.client"
DEFAULT_CLONE = "/Applications/advice/ai-camera-client"
DISABLED_SUFFIX = ".replaced-by-app"
APP_SUPPORT = os.path.expanduser("~/Library/Application Support/AICamera")
IDENTITY_FILES = ("client_config.json", "device_info.json")
STOP_WAIT_S = 15


def plist_path(home: str = "~") -> str:
    return os.path.join(os.path.expanduser(home), "Library", "LaunchAgents", f"{LAUNCHD_LABEL}.plist")


def _is_clone(folder: str) -> bool:
    return (os.path.isdir(os.path.join(folder, ".git"))
            and any(os.path.isfile(os.path.join(folder, n)) for n in ("client.py", "client_logic.py")))


def find_clone(plist: str = None, default: str = DEFAULT_CLONE):
    """The clone's folder: where its LaunchAgent starts client.py from, else the usual place."""
    plist = plist or plist_path()
    for path in (plist, plist + DISABLED_SUFFIX):
        try:
            with open(path, "rb") as f:
                job = plistlib.load(f)
        except Exception:
            continue
        candidates = [job.get("WorkingDirectory")] + [
            os.path.dirname(a) for a in job.get("ProgramArguments") or [] if str(a).endswith((".py", ".sh"))]
        for folder in candidates:
            if folder and _is_clone(folder):
                return folder
    return default if _is_clone(default) else None


def stop_clone(clone: str, plist: str = None) -> bool:
    """Unload the clone's LaunchAgent, keep it from coming back at login, end what is left of it."""
    plist = plist or plist_path()
    stopped = False
    if os.path.exists(plist):
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", plist],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        os.replace(plist, plist + DISABLED_SUFFIX)
        stopped = True
    # Started by hand, or bootout left a child behind: the clone's own processes only.
    pattern = os.path.join(clone, "")
    pids = _pids(pattern)
    for pid in pids:
        _signal(pid, signal.SIGINT)
    deadline = time.time() + STOP_WAIT_S
    while pids and time.time() < deadline:
        time.sleep(0.5)
        pids = [p for p in pids if _alive(p)]
    for pid in pids:
        _signal(pid, signal.SIGKILL)
    return stopped or bool(pids)


def _pids(pattern: str) -> list:
    try:
        out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return []
    return [int(p) for p in out.split() if p.isdigit() and int(p) != os.getpid()]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _signal(pid: int, sig) -> None:
    try:
        os.kill(pid, sig)
    except OSError:
        pass


def carry_identity(clone: str, app_support: str = APP_SUPPORT) -> bool:
    """An older clone's activation file, when the app has no identity of its own yet."""
    if os.path.exists(os.path.join(app_support, ".lcache")):
        return False
    src = os.path.join(clone, "client_config.json")
    try:
        with open(src) as f:
            if not str(json.load(f).get("activateID") or "").strip():
                return False
    except Exception:
        return False
    os.makedirs(app_support, exist_ok=True)
    carried = False
    for name in IDENTITY_FILES:
        s, d = os.path.join(clone, name), os.path.join(app_support, name)
        if os.path.isfile(s) and not os.path.exists(d):
            shutil.copy2(s, d)
            carried = True
    return carried


def _has_main(folder: str) -> bool:
    return bool(glob.glob(os.path.join(folder, "main.*")))


def carry_services(clone: str, managed: str) -> list:
    """Service folders in <clone>/dlc_local that managed_services lacks; returns their names."""
    src_root = os.path.join(clone, "dlc_local")
    if not os.path.isdir(src_root):
        return []
    os.makedirs(managed, exist_ok=True)
    carried = []
    for name in sorted(os.listdir(src_root)):
        src, dest = os.path.join(src_root, name), os.path.join(managed, name)
        if name.startswith((".", "__")) or not os.path.isdir(src) or os.path.exists(dest):
            continue
        if not _has_main(src) and name not in ("configs", "events"):
            continue  # cut off half way when the old client received it
        tmp = os.path.join(managed, f".carry-{name}")
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.copytree(src, tmp, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
        os.rename(tmp, dest)
        carried.append(name)
    return carried


def carry_pictures(clone: str, app_static: str) -> int:
    """Pictures the shop uploaded (static/images/<kind>/<file>) the app does not have; returns how many."""
    src_root = os.path.join(clone, "data_service", "static", "images")
    if not os.path.isdir(src_root):
        return 0
    count = 0
    for dirpath, _, files in os.walk(src_root):
        for fname in files:
            if fname.startswith("."):
                continue
            src = os.path.join(dirpath, fname)
            dest = os.path.join(app_static, "images", os.path.relpath(src, src_root))
            if os.path.exists(dest):
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(src, dest)
            count += 1
    return count


def take_over(app_dir: str, managed: str, app_support: str = APP_SUPPORT, plist: str = None,
              clone: str = None) -> dict:
    """All of the above; what was done, for the log. Each part goes on when another fails."""
    clone = clone or find_clone(plist)
    done = {"clone": bool(clone)}
    if not clone or os.path.realpath(clone) == os.path.realpath(app_dir):
        return done
    for key, part in (
            ("stopped", lambda: stop_clone(clone, plist)),
            ("identity", lambda: carry_identity(clone, app_support)),
            ("services", lambda: carry_services(clone, managed)),
            ("pictures", lambda: carry_pictures(clone, os.path.join(app_dir, "data_service", "static")))):
        try:
            done[key] = part()
        except Exception as e:
            logger.warning(f"[CLONE] Could not carry over the {key} from the git-clone install: {type(e).__name__}")
    return done
