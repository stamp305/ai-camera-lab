import os
import json
import time
from fastapi import APIRouter, HTTPException, Response, status
from data_service.schemas.store_schemas import BranchModel
from data_service.db.crud.store_crud import (
    create_store_data,
    read_store_data
)

router = APIRouter()

@router.post("/")
def api_create_store_data(branch_data: BranchModel):
    """
    API endpoint to create or update store data.
    """
    try:
        create_store_data(branch_data)
        return {"message": "Store data created successfully."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/")
def api_get_store_data(response: Response):
    """
    API endpoint to retrieve store data.
    """
    try:
        store_data = read_store_data()
        if not store_data:
            response.status_code = status.HTTP_404_NOT_FOUND
            return {"message": "Store data not found."}
        return store_data
    except Exception as e:
        print(f"[ERROR] Failed to retrieve store data: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.put("/")
def api_update_store_data(branch_data: BranchModel):
    """
    API endpoint to update store data.
    """
    try:
        create_store_data(branch_data)
        return {"message": "Store data updated successfully."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _running_app_root():
    """Resolve the running app's source root from this module's location."""
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _candidate_app_roots():
    """Return candidate app roots, ordered by likelihood.

    The updater writes ``client_version.json`` into the deployed bundle's
    app root, so the bundle is authoritative. When this module is loaded
    from the git source checkout the bundle still reflects the version the
    updater actually applied, so it is checked first.
    """
    roots = []
    home = os.path.expanduser("~")
    for p in (
        os.path.join(home, "Documents", "ai-camera-client-secure", "dist", "AICamera.app", "Contents", "Resources", "app"),
        os.path.join(home, "Documents", "ai-camera-client-secure", "release", "AICamera.app", "Contents", "Resources", "app"),
        _running_app_root(),
    ):
        if p not in roots:
            roots.append(p)
    return roots


def _bundle_executable_names(bundle_dir):
    """Executable names to try inside <bundle>/Contents/MacOS, best first.

    The name is whatever Info.plist declares, which is not always "AICamera":
    the packaged bundle ships "AICameraLauncher". Hardcoding the old name meant
    _find_app_bundle_bin() found nothing on a real install, so every press of
    Update in the app answered 500 "App bundle not found" and no update could
    be started from the branch at all.
    """
    names = []
    try:
        import plistlib

        with open(os.path.join(bundle_dir, "Contents", "Info.plist"), "rb") as f:
            declared = (plistlib.load(f) or {}).get("CFBundleExecutable")
        if declared:
            names.append(str(declared))
    except Exception:
        pass
    for fallback in ("AICamera", "AICameraLauncher"):
        if fallback not in names:
            names.append(fallback)
    return names


def _find_app_bundle_bin():
    """Locate the AICamera.app binary to relaunch after an update."""
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, "Documents", "ai-camera-client-secure", "dist", "AICamera.app", "Contents", "MacOS", "AICamera"),
        os.path.join(home, "Documents", "ai-camera-client-secure", "release", "AICamera.app", "Contents", "MacOS", "AICamera"),
    ]
    # Walk up from this module until an .app bundle boundary is found.
    d = os.path.abspath(os.path.dirname(__file__))
    for _ in range(10):
        if d.endswith(".app") and os.path.isdir(d):
            macos_dir = os.path.join(d, "Contents", "MacOS")
            candidates[:0] = [os.path.join(macos_dir, n)
                              for n in _bundle_executable_names(d)]
            break
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    for p in candidates:
        if os.path.isfile(p):
            return p
    return None


def _socket_assigned_version():
    """assigned_version from the last 'identified' event (client_logic saves it), or None."""
    try:
        from data_service.db.crud.client_state_crud import get_state, ASSIGNED_VERSION
        return get_state(ASSIGNED_VERSION) or None
    except Exception:
        return None


@router.get("/assigned-version")
def api_assigned_version():
    """The version the admin assigned to this branch, for the web UI's update notice."""
    return {"assigned_version": _socket_assigned_version()}


@router.get("/updates/check")
def api_check_updates():
    """
    Check for updates by comparing the local version to the version the
    admin server has assigned to this branch. Falls back to the local
    `.latest_version` flag file when the admin server is unreachable.
    """
    # Read current version from client_version.json (single source, P2-4)
    current_version = "1.0.0"
    from data_service.configs import version_store as _vs
    for root in _candidate_app_roots():
        version = _vs.read_client_version(root)
        if version != _vs.DEFAULT_VERSION:
            current_version = version
            break

    latest_version = None
    # 1) Authoritative source: ask the admin server for the assigned version.
    try:
        import requests
        import device_identity
        own_activate_id = ""
        try:
            own_activate_id = device_identity.activation_code()
        except Exception:
            pass
        # SOCKET_SERVER_HOST may already carry its port ("http://127.0.0.1:8002"),
        # and gluing SOCKET_SERVER_PORT on regardless produced
        # "http://127.0.0.1:8002:8002" — an unparseable URL. The request then
        # failed, the code fell through to the local .latest_version flag file,
        # and the check answered "already on the latest version" every single
        # time, no matter what the admin had assigned.
        from urllib.parse import urlparse
        raw_host = os.getenv("SOCKET_SERVER_HOST", "http://localhost").rstrip("/")
        port = os.getenv("SOCKET_SERVER_PORT", "3001")
        parsed = urlparse(raw_host if "://" in raw_host else f"http://{raw_host}")
        scheme = parsed.scheme or "http"
        if parsed.port:
            netloc = parsed.netloc
        elif port:
            netloc = f"{parsed.hostname}:{port}"
        else:
            netloc = parsed.hostname or "localhost"
        base = f"{scheme}://{netloc}"
        branches_url = f"{base}/ai-camera/api/branches"
        resp = requests.get(branches_url, timeout=5, verify=False,
                            headers=device_identity.admin_auth_headers(branches_url))
        if resp.status_code == 200:
            for branch in resp.json():
                # Must match this device's own branch — otherwise this picks
                # up whichever branch happens to be first in the list with
                # any assigned_version set, showing another store's version
                # as if it were "latest" for this one.
                if own_activate_id and branch.get("activation_code") != own_activate_id:
                    continue
                if branch.get("assigned_version"):
                    latest_version = branch["assigned_version"]
                    break
    except Exception:
        pass

    # 2) The version the admin assigned, as it arrived in the socket's
    #    'identified' event. Branches need this: since Device Keys (ADR 0007)
    #    /api/branches is an admin-only page and answers 403 to every shop IP.
    if not latest_version:
        latest_version = _socket_assigned_version()

    # 3) Fallback: local flag file written by the client.
    if not latest_version:
        latest_version_file = os.path.join(_running_app_root(), ".latest_version")
        if os.path.exists(latest_version_file):
            try:
                with open(latest_version_file, "r") as f:
                    latest_version = f.read().strip() or current_version
            except:
                pass
    if not latest_version:
        latest_version = current_version

    has_update = (current_version != latest_version)
    return {
        "has_update": has_update,
        "current_version": current_version,
        "latest_version": latest_version
    }

@router.post("/updates/apply")
def api_apply_updates():
    """
    Trigger the update process. Because the admin assigning a version already
    authorizes the update, this simply restarts the app: on the next boot the
    app identifies to the admin, which auto-pushes the assigned version.
    """
    import subprocess
    root_dir = _running_app_root()

    bundle_bin = _find_app_bundle_bin()
    if not bundle_bin:
        raise HTTPException(status_code=500, detail="App bundle not found. Cannot restart for update.")

    # Idempotency guard: if a relaunch helper was spawned recently (either by
    # this endpoint or by the client updater writing the same relaunch.log),
    # treat the restart as already in progress and do not spawn a second
    # helper (two helpers pkill each other and the app never comes back).
    log_path = os.path.join(root_dir, "relaunch.log")
    try:
        if time.time() - os.path.getmtime(log_path) < 20:
            return {"message": "Update already in progress. The app is restarting..."}
    except OSError:
        pass
    lock_path = os.path.join(root_dir, ".relaunch.lock")
    if os.path.isdir(lock_path):
        return {"message": "Update already in progress. The app is restarting..."}

    # Detached helper: kill every leftover AICamera instance, then relaunch the
    # binary directly with PyInstaller env vars cleared. An atomic mkdir lock
    # guarantees only one helper ever proceeds.
    #
    # The whole app (bootloader -> main python -> services) is ONE process
    # group, and this helper runs detached in its own group, so the app is
    # killed precisely with `kill -9 -- -<pgid>`. pgid is captured here via
    # os.getpgid(0) (the data_service shares the app's group).
    #
    # Plain pkill is also kept for leftovers: macOS/BSD pkill EXCLUDES the
    # pkill process's own ancestors by default, which conveniently stops it
    # from matching this helper shell (an ancestor), and the [A] trick stops
    # it matching itself. Note that pkill alone can never kill the app main
    # from here (the main is an ancestor), which is why the pgid kill is
    # required. mariadbd (port 3306) is killed separately because a fresh
    # instance dies during startup while the old one still holds the port.
    kill_cmd = ""
    try:
        kill_cmd = "kill -9 -- -%s 2>/dev/null; " % os.getpgid(0)
    except Exception:
        pass
    cmd = (
        'LOG="%s/relaunch.log"; '
        'LOCK="%s/.relaunch.lock"; '
        'if ! mkdir "$LOCK" 2>/dev/null; then exit 0; fi; '
        '{ echo "$(date +%%H:%%M:%%S) apply helper start"; } >> "$LOG" 2>&1; '
        'sleep 3; '
        '{ echo "$(date +%%H:%%M:%%S) apply helper pkill start"; '
        '%s'
        'pkill -9 -f "Contents/MacOS/[A]ICamera" 2>/dev/null; '
        'pkill -9 -f "Contents/Resources/bin/mariad[b]d" 2>/dev/null; '
        'pkill -9 -f "bootstrap.py --no-loading-[g]ui" 2>/dev/null; '
        'echo "$(date +%%H:%%M:%%S) apply helper pkill done rc=$?"; } >> "$LOG" 2>&1; '
        'sleep 1; '
        '{ echo "$(date +%%H:%%M:%%S) apply helper launch start"; '
        'env -u _PYI_PARENT_PROCESS_LEVEL -u _PYI_ARCHIVE_FILE -u _PYI_APPLICATION_HOME_DIR '
        '-u TCL_LIBRARY -u TK_LIBRARY '
        'nohup "%s" --no-loading-gui >/dev/null 2>&1 & '
        'echo "$(date +%%H:%%M:%%S) apply helper launch done rc=$?"; } >> "$LOG" 2>&1; '
        'rmdir "$LOCK" 2>/dev/null'
        % (root_dir, root_dir, kill_cmd, bundle_bin)
    )
    try:
        subprocess.Popen(
            ["/bin/sh", "-c", cmd],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return {"message": "Update triggered successfully. Restarting app to fetch the assigned version."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
