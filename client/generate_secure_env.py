import os

try:
    import _secrets
    DB_USER = getattr(_secrets, 'DB_USER', 'advice')
    DB_PASSWORD = getattr(_secrets, 'DB_PASSWORD', '')
    DB_NAME = getattr(_secrets, 'DB_NAME', 'ai_camera')
    DB_HOST = getattr(_secrets, 'DB_HOST', 'localhost')
    DB_PORT = getattr(_secrets, 'DB_PORT', 3306)
    DB_ROOT_USER = getattr(_secrets, 'DB_ROOT_USER', 'advice')
    DB_ROOT_PASSWORD = getattr(_secrets, 'DB_ROOT_PASSWORD', '')
    DB_VIEWER_USER = getattr(_secrets, 'DB_VIEWER_USER', 'client')
    DB_VIEWER_PASSWORD = getattr(_secrets, 'DB_VIEWER_PASSWORD', '')
    API_SERVER_HOST = getattr(_secrets, 'API_SERVER_HOST', 'https://app.advice.th')
    API_SERVER_PORT = getattr(_secrets, 'API_SERVER_PORT', 8081)
    SOCKET_SERVERS = getattr(_secrets, 'SOCKET_SERVERS', [{"url": "https://app.advice.th", "path": "/ai-camera/socket.io"}])
    SOCKET_SSL_VERIFY = getattr(_secrets, 'SOCKET_SSL_VERIFY', False)
    CAMERA_SECRET_KEY = getattr(_secrets, 'CAMERA_SECRET_KEY', '')
except ImportError:
    DB_USER = "advice"
    DB_PASSWORD = os.environ.get("AICAM_DB_PASSWORD", "")
    DB_NAME = "ai_camera"
    DB_HOST = "localhost"
    DB_PORT = 3306
    DB_ROOT_USER = "advice"
    DB_ROOT_PASSWORD = os.environ.get("AICAM_DB_ROOT_PASSWORD", "")
    DB_VIEWER_USER = "client"
    DB_VIEWER_PASSWORD = os.environ.get("AICAM_DB_VIEWER_PASSWORD", "")
    API_SERVER_HOST = "https://app.advice.th"
    API_SERVER_PORT = 8081
    SOCKET_SERVERS = [{"url": "https://app.advice.th", "path": "/ai-camera/socket.io"}]
    SOCKET_SSL_VERIFY = False
    CAMERA_SECRET_KEY = ""

def encrypt(text, key):
    return [ord(c) ^ key[i % len(key)] for i, c in enumerate(text)]

KEY = [168, 104, 221, 228, 56, 214, 27, 232, 33, 38, 45, 116, 50, 98, 61, 14, 95, 44, 36, 110, 3, 217, 66, 146, 146, 171, 190, 194, 29, 114, 113, 229]

lines = [
    "import os",
    "",
    "cdef list KEY = " + str(KEY),
    "",
    "cdef str _decrypt(list encrypted):",
    "    cdef int i",
    "    cdef str result = ''",
    "    for i in range(len(encrypted)):",
    "        result += chr(encrypted[i] ^ KEY[i % len(KEY)])",
    "    return result",
    "",
    "def load_secure_env():"
]

def add_env(k, v):
    # Real values may come from the machine's .env; only fall back to the
    # committed placeholder module when the env var is absent.
    v = os.getenv(k) or v
    lines.append(f"    os.environ[_decrypt({encrypt(str(k), KEY)})] = _decrypt({encrypt(str(v), KEY)})")

add_env("DB_USER", DB_USER)
add_env("DB_PASSWORD", DB_PASSWORD)
add_env("DB_NAME", DB_NAME)
add_env("DB_HOST", DB_HOST)
add_env("DB_PORT", DB_PORT)
add_env("DB_ROOT_USER", DB_ROOT_USER)
add_env("DB_ROOT_PASSWORD", DB_ROOT_PASSWORD)
add_env("DB_VIEWER_USER", DB_VIEWER_USER)
add_env("DB_VIEWER_PASSWORD", DB_VIEWER_PASSWORD)
add_env("API_SERVER_HOST", API_SERVER_HOST)
add_env("API_SERVER_PORT", API_SERVER_PORT)
add_env("SOCKET_SERVER_HOST", SOCKET_SERVERS[0]["url"])
add_env("SOCKET_SERVER_PATH", SOCKET_SERVERS[0]["path"])
add_env("SOCKET_SERVER_PORT", "443")
add_env("SOCKET_SSL_VERIFY", str(SOCKET_SSL_VERIFY))
add_env("CAMERA_SECRET_KEY", CAMERA_SECRET_KEY)

with open("data_service/configs/secure_env.pyx", "w") as f:
    f.write("\n".join(lines) + "\n")
print("Generated secure_env.pyx")
