"""Template for machine-local secrets — copy to `_secrets.py` and fill in.

REAL VALUES DO NOT LIVE IN GIT. Under the env-override scheme every value
below can be supplied through the machine's `.env` instead (recommended):

    STATIC_LEGACY_KEY=...        # Fernet key used to decrypt DLC payloads
    CAMERA_SECRET_KEY=...        # shared camera credential key
    DB_ROOT_PASSWORD=...         # MariaDB root password
    GEMINI_API_KEY=...           # read directly by the google SDK

Keep this file's values empty/placeholder; `git` must never see real keys.
"""

DB_USER = "root"
DB_PASSWORD = ""          # or set DB_PASSWORD in .env
DB_NAME = "ai_camera"
DB_HOST = "localhost"
DB_PORT = 3306
DB_ROOT_USER = "root"
DB_ROOT_PASSWORD = ""     # or set DB_ROOT_PASSWORD in .env

API_SERVER_HOST = "https://app.advice.th/ai-camera/"
API_SERVER_PORT = 443

SOCKET_SERVERS = [
    {"url": "https://app.advice.th:443", "path": "/ai-camera/socket.io"}
]
SOCKET_SSL_VERIFY = False

# Fernet key (urlsafe base64, 32 bytes) — leave empty here; set in .env
STATIC_LEGACY_KEY = b''
CAMERA_SECRET_KEY = ""
