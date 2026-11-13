"""Single source of truth for the client's installed version (P2-4).

The version is compiled into ``_build_info.so`` (read first): build_mac_app.sh
writes it for the .app, the farm worker for every update payload. Once it is
there the plain VERSION / client_version.json files are removed from the
bundle, so a shop has nothing readable to edit. ``client_version.json`` of the
form ``{"version": "V.x.y.z"}`` remains the fallback for a payload from an
older builder and for a branch installed by git clone.

Writers:
    - build_mac_app.sh and the farm worker bake it into _build_info
    - the updater writes client_version.json only when a payload has no
      _build_info, so a client still reports its ACTUAL installed version

Readers (all through read_client_version):
    - data_service/api/store.py  (/updates/check)
    - frontend_web/main.py       (version label, session-cookie binding)
    - client_logic.py            (identify / request_update payloads)
"""
from __future__ import annotations

import json
import os
from typing import Optional

VERSION_FILENAME = "client_version.json"
DEFAULT_VERSION = "1.0.0"


def version_file_path(root_dir: str) -> str:
    return os.path.join(root_dir, VERSION_FILENAME)


BAKED_MODULE = "_build_info"


def baked_version(root_dir: str) -> Optional[str]:
    """VERSION compiled into ``_build_info`` at ``root_dir``, or None.

    The build farm writes _build_info.py with the job's version before it
    compiles the payload, so the number travels inside a .so next to the code
    it describes: editing client_version.json cannot change it, and a rolled-
    back update brings its own (older) number back with the restored files.
    Loaded by path so it is the file in *this* bundle, not whatever is first on
    sys.path.
    """
    import importlib.machinery
    import importlib.util
    for suffix in importlib.machinery.EXTENSION_SUFFIXES + [".py"]:
        path = os.path.join(root_dir, BAKED_MODULE + suffix)
        if not os.path.exists(path):
            continue
        try:
            spec = importlib.util.spec_from_file_location(BAKED_MODULE, path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            version = str(getattr(mod, "VERSION", "") or "").strip()
            if version:
                return version
        except Exception:
            continue
    return None


def has_baked_module(root_dir: str) -> bool:
    """True when ``root_dir`` holds a compiled ``_build_info`` (the version travels inside it)."""
    import importlib.machinery
    return any(os.path.exists(os.path.join(root_dir, BAKED_MODULE + s))
               for s in importlib.machinery.EXTENSION_SUFFIXES)


def remove_plain_version_files(root_dir: str) -> list:
    """Delete VERSION / client_version.json once the compiled version is there.

    Only inside an installed .app: a branch installed by git clone tracks both
    files in git, and deleting them would leave its checkout modified.
    Returns the paths removed.
    """
    removed = []
    if "/AICamera.app/" not in os.path.realpath(root_dir) + "/" or not baked_version(root_dir):
        return removed
    for name in (VERSION_FILENAME, "VERSION"):
        path = os.path.join(root_dir, name)
        try:
            os.remove(path)
            removed.append(path)
        except OSError:
            pass
    return removed


def read_client_version(root_dir: str, default: str = DEFAULT_VERSION) -> str:
    """Installed version at ``root_dir``: the compiled-in one, else client_version.json, else ``default``."""
    baked = baked_version(root_dir)
    if baked:
        return baked
    try:
        with open(version_file_path(root_dir), "r", encoding="utf-8") as f:
            data = json.load(f)
        return str(data.get("version", default))
    except Exception:
        return default


def write_client_version(root_dir: str, version: str) -> bool:
    """Atomically write the version file. Returns True on success."""
    try:
        tmp = version_file_path(root_dir) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": version}, f)
        os.replace(tmp, version_file_path(root_dir))
        return True
    except Exception:
        return False
