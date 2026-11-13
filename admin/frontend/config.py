# frontend/config.py
"""Configuration settings for the AI Camera Admin frontend."""
from __future__ import annotations

import os

# Base directory
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))


def _env(name: str) -> str:
    """name from the process env if set non-empty, else its line in frontend/.env (not in git), else ""."""
    value = os.getenv(name, "")
    if value.strip():
        return value
    try:
        with open(os.path.join(_THIS_DIR, ".env"), encoding="utf-8-sig") as f:
            for line in f:
                key, sep, rest = line.strip().partition("=")
                if sep and key.strip().replace("export ", "", 1).strip() == name:
                    value = rest.split("#", 1)[0].strip().strip("\"'")
    except OSError:
        pass
    return value


class Config:
    """Application configuration."""
    
    # External API settings
    DATA_SERVICE_API_BASE = os.getenv("AI_CAMERA_ADMIN_BACKEND_URL", "http://127.0.0.1:8011")
    HTTP_TIMEOUT_SHORT = 5
    HTTP_TIMEOUT_LONG = 30
    # Storing a version pulls the commit + compiles payload to .so (can take
    # minutes). A short timeout made the frontend show a false "failed" toast
    # while the backend kept working and eventually stored the version anyway.
    HTTP_TIMEOUT_STORE = 600
    
    # Encryption key (must match backend/server.py & client/client.py)
    # Not in git (mock admin): AI_CAMERA_ADMIN_DLC_KEY from the environment or frontend/.env.
    ENCRYPTION_KEY = _env("AI_CAMERA_ADMIN_DLC_KEY").strip().encode()
    
    # IP whitelists, applied by gate.py (ADR 0007). The admin UI accepts only
    # OFFICE_IPS. Branch REST and Activation accept TRANSITION_IPS: the office
    # list plus these shop IPs, so keyless Branches keep working until every
    # Branch holds a Device Key. Shop IPs never open the admin UI.
    BRANCH_IPS = {
        "AVI001": "101.109.122.97",
        "AVI012": "49.49.222.144",
        "AVI014": "171.97.159.157",
        "AVI017": "101.108.116.1",
        "AVI022": "171.97.6.161",
        "AVI025IT": "49.228.154.145",
        "AVI025MONO": "49.228.248.116",
        "AVI028": "182.232.50.171",
        "AVI029": "110.78.183.141",
        "AVI039": "49.228.233.252",
        "UNS008": "27.130.178.252",
        "UNS011": "49.228.245.185",
        "UNS018IT": "159.192.176.149",
        "UNS024IT": "159.192.37.102",
        "UNS024MONO": "49.228.171.26",
        "UNS027": "184.22.114.122",
        "UNS031": "49.228.168.62",
        "UNS033": "49.228.177.213",
        "UNS034": "49.228.178.28",
        "UNS037": "58.10.106.118",
        "UNS039": "49.228.41.19",
        "UNS043": "49.228.49.142",
        # "UNS049": "159.192.41.9",
        "UNS049": "159.192.40.148",
        "UNS051": "101.51.58.240",
        "UNS052": "184.22.78.216",
        "UNS061IT": "49.228.109.150",
        "UNS061MONO": "223.207.238.184",
        "UNS063": "27.130.156.152",
        "UNS066IT": "184.22.113.110",
        "UNS066MONO": "184.22.243.98",
        "UNS071": "223.205.233.132",
        "UNS078": "184.22.164.24",
        "UNS080": "125.26.217.178",
        "UNS084": "49.237.167.117",
        "UNS089": "49.228.177.47",
        "UNS093": "184.22.46.101",
        "UNS106": "184.22.77.35",
    }
    # AI_CAMERA_ADMIN_EXTRA_ALLOWED_IPS lets a LAN test machine (a branch client
    # on 192.168.x.x, or 127.0.0.1 when the admin and a client share one box)
    # through without editing this list — comma separated, and left unset in
    # production so the whitelist stays exactly as it is today.
    OFFICE_IPS = (
        {"171.100.254.57"}
        | {ip.strip() for ip in os.getenv("AI_CAMERA_ADMIN_EXTRA_ALLOWED_IPS", "").split(",") if ip.strip()}
    )
    TRANSITION_IPS = OFFICE_IPS | set(BRANCH_IPS.values())

    # Ends the Device Key transition (ADR 0007). Off: keyless Branches still get
    # in (socket by a known Activation Code, REST by TRANSITION_IPS). On: every
    # Branch socket and Branch REST call must present its Device Key. Turn it on
    # only once every Branch in use holds a key (DEPLOYMENT.md §7). Set it in
    # frontend/.env; read once at start, so a change needs a frontend restart.
    REQUIRE_DEVICE_KEY = _env("AI_CAMERA_ADMIN_REQUIRE_DEVICE_KEY").strip().lower() in ("1", "true", "yes", "on")
    
    # Path settings
    DLC_MASTER_PATH = os.path.abspath(os.path.join(_THIS_DIR, "../backend/dlc_master"))
    MANAGED_MASTER_PATH = os.path.abspath(os.path.join(_THIS_DIR, "../backend/managed_master"))
    MANAGED_SERVICES_VERSIONS_CACHE = os.path.abspath(os.path.join(_THIS_DIR, "../backend/client_code/versions"))
    LOGS_BASE_PATH = os.path.abspath(os.path.join(_THIS_DIR, "../backend/client_logs"))

    # Frontend base URL used for build-farm uploads / force-update callbacks
    FRONTEND_BASE_URL = os.getenv("AI_CAMERA_ADMIN_FRONTEND_URL", "http://localhost:8002")
