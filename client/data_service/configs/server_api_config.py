import os
from urllib.parse import urlparse
from dotenv import load_dotenv
load_dotenv()

API_HOST = os.getenv('API_SERVER_HOST', 'https://app.advice.th/ai-camera')
API_PORTS = os.getenv('API_SERVER_PORT', 8081)

# The auth service is served under the /ai-camera prefix, but API_SERVER_HOST is
# shared with the version-manager base in client_logic, which appends
# API_SERVER_PORT and must stay prefix-free — so some builds bake the bare origin
# here. Without the prefix every activation POSTed to /api/auth/activate, got a
# 404, and surfaced in the UI as the useless "Activation service error" (502).
# Only fill in the prefix when the operator supplied no path of their own, so a
# local override like http://127.0.0.1:8011 still points where it was told to.
_AUTH_PREFIX = '/ai-camera'
_parsed = urlparse(API_HOST)
if _parsed.path.strip('/') == '':
    API_HOST = f"{API_HOST.rstrip('/')}{_AUTH_PREFIX}"
API_HOST = API_HOST.rstrip('/')

AUTH_APIS = {
    "activate": f"{API_HOST}/api/auth/activate",
    "login": f"{API_HOST}/api/auth/login",
}
