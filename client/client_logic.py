import base64
import hashlib
import uuid
import importlib
import importlib.abc
import importlib.machinery
import json
import logging
from logging.handlers import TimedRotatingFileHandler
import multiprocessing
import os
import re
import shutil
import subprocess
import sys
import traceback
import threading
import time
from datetime import datetime, timedelta, time as dt_time
from types import ModuleType

from mac_utils import get_canonical_mac_address
import device_identity
import dlc_manifest


def get_mac_address():
    return get_canonical_mac_address()

# --------------------------------------------------------------------------------------
# Logging Configuration - Save logs to 'logs' folder for past 7 days
# --------------------------------------------------------------------------------------
def setup_logging():
    """Configure logging to save logs to Application Support folder with daily rotation."""
    # Also here for `python client.py`, which never runs bootstrap.py.
    try:
        import log_redact
        log_redact.install()
    except ImportError:
        pass
    import log_seal
    log_seal.install()
    app_support = os.path.expanduser('~/Library/Application Support/AICamera')
    logs_dir = os.path.join(app_support, 'logs')
    os.makedirs(logs_dir, exist_ok=True)
    
    log_file = os.path.join(logs_dir, 'client.log')
    
    # Create formatter
    formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # File handler with daily rotation, keeping 7 days
    file_handler = TimedRotatingFileHandler(
        log_file,
        when='midnight',
        interval=1,
        backupCount=7,
        encoding='utf-8'
    )
    file_handler.setFormatter(formatter)
    file_handler.suffix = '%Y-%m-%d'
    
    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    
    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)
    # At INFO these log every request line they make, query string included
    # (camera addresses, the server's endpoints).
    for _noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)
    # client.log is sealed now, so the port page reads its few lines from here.
    import port_store
    root_logger.addHandler(port_store.ServiceEventTap())
    
    return logging.getLogger(__name__)

# Initialize logging
logger = setup_logging()

try:
    import _secrets
except ImportError:
    class _DummySecrets:
        DB_USER = "advice"
        DB_PASSWORD = ""
        DB_NAME = "ai_camera"
        DB_HOST = "localhost"
        DB_PORT = 3306
        DB_ROOT_USER = "advice"
        DB_ROOT_PASSWORD = ""
        DB_VIEWER_USER = "client"
        DB_VIEWER_PASSWORD = ""
        API_SERVER_HOST = "https://app.advice.th"
        API_SERVER_PORT = 8081
        SOCKET_SERVERS = [{"url": "https://app.advice.th", "path": "/ai-camera/socket.io"}]
        SOCKET_SSL_VERIFY = False
        STATIC_LEGACY_KEY = b''
        CAMERA_SECRET_KEY = ""
    _secrets = _DummySecrets()
    logger.warning("[CLIENT] _secrets.py not found. Falling back to default values. Please setup .env")

from cryptography.fernet import Fernet

import device_identity

def get_dlc_cipher():
    """Return the Fernet the admin encrypts DLC payloads with.

    That is one shared key (STATIC_LEGACY_KEY), not the Device Key: the admin
    encrypts every payload with its own ENCRYPTION_KEY and keeps only a hash of
    each Device Key, so it has nothing per-machine to encrypt with — and a
    Device Key is token_urlsafe(32), which Fernet rejects as a key anyway. The
    Device Key authenticates the machine to the admin; it does not decrypt.
    """
    key = os.getenv("STATIC_LEGACY_KEY") or getattr(_secrets, "STATIC_LEGACY_KEY", b"")
    if isinstance(key, str):
        key = key.strip().encode()
    if not key:
        return None
    try:
        return Fernet(key)
    except Exception as e:
        logger.error(f"[CLIENT SOCKET] STATIC_LEGACY_KEY is not a valid Fernet key: {e}")
        return None
# Captured BEFORE load_secure_env(), which assigns os.environ[...] outright and
# would otherwise wipe an operator-supplied address with the one baked in at
# build time. Empty in the field; set only to aim a build at a test admin.
# The port belongs here too: setup_clients() reads SOCKET_SERVER_PORT long
# after load_secure_env() has replaced it, so a host given without a port was
# always dialled on the compiled-in one.
# API_SERVER_HOST/PORT likewise: activation and the admin REST calls read it,
# and a test admin is useless if activation still goes to the baked-in host
# while the socket goes to the test one.
_ENV_SOCKET_OVERRIDES = {
    _name: os.environ[_name]
    for _name in ('SOCKET_SERVER_HOST', 'SOCKET_SERVER_PATH',
                  'SOCKET_SERVER_PORT', 'SOCKET_SSL_VERIFY',
                  'API_SERVER_HOST', 'API_SERVER_PORT')
    if os.environ.get(_name)
}
_ENV_SOCKET_HOST_OVERRIDE = _ENV_SOCKET_OVERRIDES.get('SOCKET_SERVER_HOST')
_ENV_SOCKET_PATH_OVERRIDE = _ENV_SOCKET_OVERRIDES.get('SOCKET_SERVER_PATH')
_ENV_SOCKET_PORT_OVERRIDE = _ENV_SOCKET_OVERRIDES.get('SOCKET_SERVER_PORT')

from data_service.configs.secure_env import load_secure_env as _load_secure_env_raw



def _dlc_kept_encrypted() -> bool:
    """True in the .app: bootstrap.py ran (it sets sys._machine_cipher) and the
    bundled python_env carries dlc_import_hook for every child process."""
    return hasattr(sys, '_machine_cipher')


def _encrypt_managed_services_at_rest():
    """Bring an install made before services were kept encrypted up to date.

    Encrypts every plain .py under managed_services/ with the DLC key (the same
    key the admin sends them in, so the update manifest still hashes to the
    same plaintext), re-keys any file a past build re-encrypted with the
    machine key, and deletes the __pycache__ folders, whose .pyc files are the
    services' compiled code in readable form.
    """
    if not _dlc_kept_encrypted() or not os.path.isdir(MANAGED_PATH):
        return
    cipher = get_dlc_cipher()
    if cipher is None:
        logger.error("[DLC] No DLC key; leaving managed services as they are")
        return
    machine_cipher = getattr(sys, '_machine_cipher', None)
    encrypted = rekeyed = caches = 0
    for dirpath, dirnames, filenames in os.walk(MANAGED_PATH):
        if '__pycache__' in dirnames:
            shutil.rmtree(os.path.join(dirpath, '__pycache__'), ignore_errors=True)
            dirnames.remove('__pycache__')
            caches += 1
        for fname in filenames:
            if not fname.endswith('.py'):
                continue
            path = os.path.join(dirpath, fname)
            try:
                with open(path, 'rb') as f:
                    data = f.read()
                if data.startswith(b'gAAAA'):
                    try:
                        cipher.decrypt(data)
                        continue  # already as the hook expects
                    except Exception:
                        if machine_cipher is None:
                            continue
                        data = machine_cipher.decrypt(data)
                        rekeyed += 1
                else:
                    encrypted += 1
                tmp = path + '.tmp'
                with open(tmp, 'wb') as f:
                    f.write(cipher.encrypt(data))
                os.replace(tmp, path)
            except Exception as e:
                logger.warning(f"[DLC] Could not encrypt a service file: {type(e).__name__}")
    if encrypted or rekeyed or caches:
        logger.info(f"[DLC] Managed services at rest: encrypted {encrypted}, re-keyed {rekeyed}, "
                    f"removed {caches} __pycache__ folder(s)")

def load_secure_env():
    """load_secure_env() that leaves the operator's overrides standing.

    The compiled loader assigns os.environ[...] outright, so every call wipes
    them again. It is called four times over startup — here, at DB config, and
    twice in _create_backend_env() — and the services below are started with
    multiprocessing 'spawn': a child re-imports this module and re-reads
    os.environ, so one un-restored call late in startup is enough to leave the
    parent talking to the test admin while every service dials production.
    """
    _load_secure_env_raw()
    os.environ.update(_ENV_SOCKET_OVERRIDES)


load_secure_env()

import encodings.idna

# --------------------------------------------------------------------------------------
# FastAPI Service Loader — resolves .so or .py on filesystem, then imports via SourceFileLoader
# --------------------------------------------------------------------------------------
def _resolve_path(p: str):
    if not p:
        return None
    _app_dir_val = os.path.abspath(os.path.dirname(__file__))
    # Always prepend _app_dir_val — this is the only reliable path in Cython builds
    # where the module's __file__ is the .so location
    for candidate in [p, os.path.join(_app_dir_val, p)]:
        if os.path.exists(candidate):
            return candidate
        so_variant = candidate.replace('.py', '.so')
        if os.path.exists(so_variant):
            return so_variant
        managed_path = os.path.join(APP_SUPPORT_DIR, 'managed_services', p[len("client/managed_services/"):])
        managed_so = managed_path.replace('.py', '.so')
        if os.path.exists(managed_so):
            return managed_so
        if os.path.exists(managed_path):
            return managed_path
        alt = p[len("client/managed_services/"):]
        alt_so = alt.replace('.py', '.so')
        abs_alt = os.path.join(_app_dir_val, alt)
        abs_so = os.path.join(_app_dir_val, alt_so)
        if os.path.exists(abs_so):
            return abs_so
        if os.path.exists(abs_alt) or os.path.exists(abs_alt.replace('.py', '.so')):
            return abs_alt
            
    if p.startswith("ai-camera-client/"):
        alt = p.split('/', 1)[1]
        alt_so = alt.replace('.py', '.so')
        if os.path.exists(alt_so):
            return alt_so
        if os.path.exists(alt):
            return alt
        abs_alt = os.path.join(_app_dir_val, alt)
        abs_so = os.path.join(_app_dir_val, alt_so)
        if os.path.exists(abs_so):
            return abs_so
        if os.path.exists(abs_alt) or os.path.exists(abs_alt.replace('.py', '.so')):
            return abs_alt
            
    if p.startswith("client/"):
        alt = p.split('/', 1)[1]
        alt_so = alt.replace('.py', '.so')
        if os.path.exists(alt_so):
            return alt_so
        if os.path.exists(alt):
            return alt
        abs_alt = os.path.join(_app_dir_val, alt)
        abs_so = os.path.join(_app_dir_val, alt_so)
        if os.path.exists(abs_so):
            return abs_so
        if os.path.exists(abs_alt) or os.path.exists(abs_alt.replace('.py', '.so')):
            return abs_alt
            
    # Check App Support dir for managed_services files
    if APP_SUPPORT_DIR:
        as_path = os.path.join(APP_SUPPORT_DIR, p)
        if os.path.exists(as_path) or os.path.exists(as_path.replace('.py', '.so')):
            return as_path
        as_so = as_path.replace('.py', '.so')
        if os.path.exists(as_so):
            return as_so
    
    return p

def _export_service_ports_to_env():
    """Tell a service where its neighbours listen, from the port table.

    A delivered service hard-codes a default for every service it talks to
    ("http://127.0.0.1:8000" for data_service). Those defaults are wrong the
    moment a branch moves a port from the web UI — and they were already wrong
    here, with data_service on 8200 while recorder/analyzer still called 8000.
    So every service gets the live numbers before it is imported.

    setdefault, not assignment: a value already in the environment (.env, or
    the admin's own override) is a deliberate choice and outranks the table.
    AICAM_PORT_* is the generic form, so a service delivered later can read its
    neighbours' ports without this function having to learn its name.
    """
    try:
        ports = port_store.port_map()
    except Exception as e:
        logger.warning(f"[PORTS] Could not read the port table for the service environment: {e}")
        return
    host = _service_bind_host()
    for name, value in ports.items():
        os.environ.setdefault(f"AICAM_PORT_{name.upper()}", str(value))
    for env_name, service in (("DATA_SERVICE_URL", "data_service"),
                              ("ANALYZER_URL", "analyzer_service")):
        if service in ports:
            os.environ.setdefault(env_name, f"http://{host}:{ports[service]}")


def _load_service_app(service_key: str, root_pkg: str, port_key: str, port: int):
    # Runs in the service's own process. Every way it cannot serve exits 1 (it used to
    # return: exit 0, the code of a clean stop), which the watchdog logs.
    svc_def = FILE_PATHS.get(service_key, {})
    # DLC services have no FILE_PATHS entry: every DLC folder follows <folder>/main.py.
    orig_path = svc_def.get('main') or f"client/managed_services/{service_key}/main.py"
    main_path = _resolve_path(orig_path)
    if not main_path:
        logger.warning(f"⏭️  {service_key} skipped — main file not yet available")
        sys.exit(1)
    __app_dir_val = os.path.abspath(os.path.dirname(__file__))
    if not os.path.exists(main_path):
        abs_so = os.path.join(__app_dir_val, main_path.replace('.py', '.so'))
        if os.path.exists(abs_so):
            main_path = abs_so
        else:
            logger.warning(f"⏭️  {service_key} skipped — file not found (will be available after DLC download)")
            sys.exit(1)
    root_dir = os.path.dirname(main_path)
    if root_dir and root_dir not in sys.path:
        sys.path.insert(0, root_dir)
    # The folder holding all services, so a service that imports itself as a
    # package ("from analyzer_service.run_metrics import …" — what the video
    # pipeline services do) resolves. Older services import through the
    # 'dlc_local' alias registered below and do not need this.
    services_dir = os.path.dirname(root_dir)
    if services_dir and services_dir not in sys.path:
        sys.path.insert(0, services_dir)
    # Also add app root to sys.path for config imports (port_store, etc.)
    _app_root = os.path.abspath(os.path.dirname(__file__))
    if _app_root not in sys.path:
        sys.path.insert(0, _app_root)

    # Register managed_services as 'dlc_local' for backward compat with old DLC code
    if 'dlc_local' not in sys.modules:
        from types import ModuleType
        _dlc_mod = ModuleType('dlc_local')
        _dlc_mod.__path__ = [MANAGED_PATH]
        _dlc_mod.__package__ = 'dlc_local'
        sys.modules['dlc_local'] = _dlc_mod
    else:
        sys.modules['dlc_local'].__path__ = [MANAGED_PATH]
    
    # Before the import: a service reads these at module level, so setting them
    # afterwards would be too late.
    _export_service_ports_to_env()

    # Managed services are kept encrypted on disk in the .app; this decrypts them
    # at import. Spawned children get it from the bundled python_env's .pth.
    if _dlc_kept_encrypted():
        import dlc_import_hook
        dlc_import_hook.install()

    logger.info(f"Importing {service_key} main module...")
    folder_name = os.path.basename(root_dir)
    is_package = os.path.exists(os.path.join(root_dir, '__init__.py'))

    # No config module to reload here any more: a service reads its port from
    # the port table when it starts, so there is nothing cached to invalidate.
    try:
        if is_package:
            # Loading main.py standalone names it "main", which has no parent
            # package, so a relative import inside it ("from .metrics import …",
            # which monitor_service does) fails. Importing it as
            # "<folder>.main" gives it the parent it expects.
            sys.modules.pop(f"{folder_name}.main", None)
            mod = importlib.import_module(f"{folder_name}.main")
        else:
            main_mod_name = os.path.splitext(os.path.basename(main_path))[0]
            import dlc_import_hook
            spec = dlc_import_hook.spec_from_file(main_mod_name, main_path)
            if spec is None:
                logger.error(f"❌ Cannot create spec for {service_key}")
                sys.exit(1)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[main_mod_name] = mod
            spec.loader.exec_module(mod)
    except Exception as e:
        logger.error(f"❌ Runtime error importing {service_key} main: {e}")
        logger.error(traceback.format_exc())
        sys.exit(1)
    
    fastapi_app = getattr(mod, 'app', None)
    if fastapi_app:
        _strip_docs_routes(fastapi_app)
        logger.info(f"✅ FastAPI 'app' object found for {service_key}.")
        logger.info(f"🚀 Starting Uvicorn server for {service_key} on port {port}...")
        import uvicorn
        # Bind internal services to the configured API host. The default is
        # loopback: the browser reaches everything through the frontend proxy,
        # so exposing raw services (no auth, no CORS) on the LAN is unnecessary.
        # log_config=None: uvicorn's own config sends its errors (a request that
        # raised, a failed start) to stderr, which the launcher discards. Without
        # it they reach this process's log files like every other message.
        uvicorn.run(fastapi_app, host=_service_bind_host(), port=port, access_log=False,
                    timeout_graceful_shutdown=5, log_config=None, log_level="warning")
    else:
        logger.error(f"❌ {service_key} main imported but no 'app' object was found.")
        sys.exit(1)

try:
    import mariadb
except ImportError:
    import pymysql
    pymysql.install_as_MySQLdb()
    
    import pymysql.cursors
    original_cursor = pymysql.connections.Connection.cursor
    def _patched_cursor(self, cursor=None, dictionary=False):
        if dictionary:
            return original_cursor(self, pymysql.cursors.DictCursor)
        return original_cursor(self, cursor)
    pymysql.connections.Connection.cursor = _patched_cursor
    
    sys.modules['mariadb'] = pymysql
    import mariadb

logger.info("[CLIENT_LOGIC] All external modules imported successfully!")
import port_store

# Generated output, never an input: nothing reads this file back into the
# environment, so editing it changes nothing. It is a record of what the app
# resolved at first run. To point a build at a different admin, set
# SOCKET_SERVER_HOST (with the port in the URL) in the environment that starts
# the app — see _ENV_SOCKET_OVERRIDES above. Deliberately not read back: it is
# written once and never refreshed, so a store that kept an old copy would be
# pinned to a retired server address forever.
ENV_FILE = os.path.expanduser('~/Library/Application Support/AICamera/.env')
load_secure_env()

DB_USER = os.getenv("DB_USER", _secrets.DB_USER)
DB_PASSWORD = os.getenv("DB_PASSWORD", _secrets.DB_PASSWORD)
DB_HOST = os.getenv("DB_HOST", _secrets.DB_HOST)
DB_PORT = int(os.getenv("DB_PORT", str(_secrets.DB_PORT))) if os.getenv("DB_PORT") else _secrets.DB_PORT
DB_NAME = os.getenv("DB_NAME", _secrets.DB_NAME)

# --------------------------------------------------------------------------------------
# Environment setup functions
# --------------------------------------------------------------------------------------
def _create_backend_env():
    """Load the backend settings into os.environ; never write them to disk.

    This used to write every value (DB root password, CAMERA_SECRET_KEY) to a
    plain ~/Library/Application Support/AICamera/.env that anyone on the Mac
    could read. Nothing needs that file: load_secure_env() sets every one of
    those keys from the compiled module, and a later load_dotenv() never
    overrides a key already set. A copy left by an older version is deleted.
    """
    global DB_USER,DB_PASSWORD,DB_HOST,DB_PORT,DB_NAME
    try:
        load_secure_env()
        if os.path.exists(ENV_FILE):
            os.remove(ENV_FILE)
            logger.info(f"[CLIENT] Removed the plain-text settings file {ENV_FILE}")
        DB_USER = os.getenv("DB_USER", _secrets.DB_USER)
        DB_PASSWORD = os.getenv("DB_PASSWORD", _secrets.DB_PASSWORD)
        DB_HOST = os.getenv("DB_HOST", _secrets.DB_HOST)
        DB_PORT = int(os.getenv("DB_PORT", str(_secrets.DB_PORT))) if os.getenv("DB_PORT") else _secrets.DB_PORT
        DB_NAME = os.getenv("DB_NAME", _secrets.DB_NAME)
    except Exception as e:
        logger.error(f"[CLIENT] Failed to load backend settings: {e}")


# --------------------------------------------------------------------------------------
# Frontend .env auto-update to reflect current device IP
# --------------------------------------------------------------------------------------
def _determine_local_ip():
    """Best-effort determine the primary local IPv4 address (non-loopback).

    Tries a UDP connect trick (no packets actually sent) then falls back to
    hostname resolution, finally 127.0.0.1.
    """
    try:
        import subprocess
        result = subprocess.run([sys.executable, '--check-ip'], capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    try:
        import subprocess
        result = subprocess.run([sys.executable, '--check-hostname-ip'], capture_output=True, text=True)
        if result.returncode == 0:
            ip = result.stdout.strip()
            if ip and not ip.startswith("127."):
                return ip
    except Exception:
        pass
    return "127.0.0.1"

# _update_frontend_env() stood here. It rewrote NEXT_PUBLIC_* keys in .env on
# every boot for the Next.js frontend, which has been deleted — nothing has
# read those keys since the UI moved to frontend_web.



# --------------------------------------------------------------------------------------
# Configuration for the client application
# --------------------------------------------------------------------------------------
APP_SUPPORT_DIR = os.path.expanduser('~/Library/Application Support/AICamera')
MANAGED_PATH = os.path.join(APP_SUPPORT_DIR, 'managed_services')



# --------------------------------------------------------------------------------------
# API and Socket Port Configuration
# --------------------------------------------------------------------------------------
def _as_int_port(val, default: int):
    """Best-effort convert SERVER_SOCKET_PORT values (which may be str) to int.
    Falls back to provided default on failure.
    """
    try:
        return int(val)
    except (TypeError, ValueError):
        return default

SERVER_SOCKET_HOST = 'http://localhost'
SERVER_SOCKET_PATH = os.getenv('SOCKET_SERVER_PATH', '/ai-camera/socket.io')
SERVER_HTTP_PORT = int(os.getenv('SOCKET_SERVER_PORT', 3001))

if SERVER_SOCKET_HOST.startswith('http://'):
    SERVER_SOCKET_URL = f"{SERVER_SOCKET_HOST}:{SERVER_HTTP_PORT}"
elif SERVER_SOCKET_HOST.startswith('https://'):
    SERVER_SOCKET_URL = f"{SERVER_SOCKET_HOST}"

# ----------------------------------------------------------------------------------
# Socket.IO client setup
# ----------------------------------------------------------------------------------
import requests
import socketio


def _link_up(c) -> bool:
    """Connected for real: python-socketio says so and Engine.IO still has the connection."""
    return bool(c.connected) and getattr(getattr(c, 'eio', None), 'state', None) == 'connected'


def _reconnecting(c) -> bool:
    """python-socketio is reconnecting `c` by itself (and may be running connect() right now)."""
    task = getattr(c, '_reconnect_task', None)
    return bool(task) and task.is_alive()


class MultiSocketClient:
    def __init__(self):
        self.clients = []
        self._dead_seen = set()   # id() of clients seen marked connected with no connection
        self._replace_lock = threading.RLock()   # the supervisor and the refresher both check
        # Use the globally initialized _secrets object
        self.servers = getattr(_secrets, 'SOCKET_SERVERS', [
            {"url": "http://localhost:3001", "path": "/ai-camera/socket.io"}
        ])
        # SOCKET_SERVER_HOST wins over the compiled-in _secrets list, the same way
        # every other setting above lets an env var beat the placeholder module.
        # Without it the server address is fixed at build time and the app can
        # only ever talk to production — there is no way to point a build at a
        # test admin. Unset in the field, so stores keep using _secrets.
        env_host = _ENV_SOCKET_HOST_OVERRIDE
        if env_host:
            self.servers = [{
                "url": env_host,
                "path": _ENV_SOCKET_PATH_OVERRIDE or self.servers[0].get('path', '/ai-camera/socket.io'),
            }]
            logger.info("[CLIENT SOCKET] Server address taken from the environment (SOCKET_SERVER_HOST)")

    def setup_clients(self):
        if self.clients: return
        self.http_session = requests.Session()
        self.http_session.verify = False
        
        for srv in self.servers:
            self.clients.append(self._new_client(srv))

    def _new_client(self, srv):
        # Ensure the server URL includes an explicit port when an env var is set
        url = srv.get('url')
        path = srv.get('path')
        effective_url = url
        try:
            from urllib.parse import urlparse, urlunparse
            parsed = urlparse(url)
            # If netloc has no port and an env SOCKET_SERVER_PORT is set, append it
            if parsed.hostname and parsed.port is None:
                port_env = _ENV_SOCKET_PORT_OVERRIDE or os.getenv('SOCKET_SERVER_PORT')
                if port_env:
                    netloc = f"{parsed.hostname}:{port_env}"
                    if parsed.username:
                        # Preserve username/password if present
                        auth = f"{parsed.username}"
                        if parsed.password:
                            auth += f":{parsed.password}"
                        netloc = f"{auth}@" + netloc
                    parsed = parsed._replace(netloc=netloc)
                    effective_url = urlunparse(parsed)
        except Exception:
            effective_url = url

        sio = socketio.Client(reconnection=True, http_session=self.http_session)
        sio.server_info = {**srv, 'url': effective_url, 'path': path}
        
        # Register event handlers wrapped to pass this sio (or just let them use get_sio())
        sio.on('connect', connect)
        sio.on('disconnect', disconnect)
        sio.on('connect_error', on_connect_error)
        sio.on('service_update', lambda data: None)
        sio.on('main_client_update', lambda data: None)
        sio.on('update_camera_status', lambda data: None)
        sio.on('broadcast_force_update', lambda data: logger.info("[CLIENT SOCKET] Received Force Update broadcast"))
        
        sio.on('identified', on_identified)
        sio.on('check_for_updates', on_check_for_updates)
        sio.on('delete_file', on_delete_file)
        sio.on('add_file', on_add_file)
        sio.on('add_file_begin', on_add_file_begin)
        sio.on('add_file_chunk', on_add_file_chunk)
        sio.on('add_file_end', on_add_file_end)
        sio.on('dlc_caps', on_dlc_caps)
        sio.on('main_update_begin', on_main_update_begin)
        sio.on('main_update_file', on_main_update_file)
        sio.on('main_update_complete', on_main_update_complete)
        sio.on('delete_folder', on_delete_folder)
        sio.on('service_update_begin', on_service_update_begin)
        sio.on('service_folder_begin', on_service_folder_begin)
        sio.on('service_folder_complete', on_service_folder_complete)
        sio.on('service_update_complete', on_service_update_complete)
        sio.on('dlc_update_begin', on_dlc_update_begin)
        sio.on('dlc_update_complete', on_dlc_update_complete)
        sio.on('update_complete', on_update_complete)
        sio.on('trigger_build', on_trigger_build)
        sio.source_server = srv
        return sio

    @property
    def connected(self):
        return any(_link_up(c) for c in self.clients)

    @property
    def eio(self):
        for c in self.clients:
            if _link_up(c): return getattr(c, 'eio', None)
        return getattr(self.clients[0], 'eio', None) if self.clients else None

    def replace_dead_clients(self) -> bool:
        """Swap a client marked connected with no Engine.IO connection under it for a new one.

        UNS089, 30 Oct: such a client drops every emit without an error, refuses to
        connect again ("Already connected") and nothing in python-socketio ever clears
        it, so the branch stayed offline until the app was restarted. Only one seen
        that way twice in a row is replaced: a disconnect passes through the same state
        for a moment. True when one was replaced.
        """
        replaced = False
        with self._replace_lock:
            for i, c in enumerate(list(self.clients)):
                if not (c.connected and not _link_up(c)):
                    self._dead_seen.discard(id(c))
                    continue
                if id(c) not in self._dead_seen:
                    self._dead_seen.add(id(c))
                    continue
                self._dead_seen.discard(id(c))
                logger.warning("[CLIENT SOCKET] The connection is gone but still marked connected — starting a new one")
                self.replace_client(i)
                replaced = True
        return replaced

    def replace_client(self, i: int):
        with self._replace_lock:
            old = self.clients[i]
            old.reconnection = False
            try:
                old.disconnect()
            except Exception:
                pass
            self.clients[i] = self._new_client(old.source_server)

    def emit(self, *args, **kwargs):
        success = False
        last_err = None
        for c in self.clients:
            try:
                c.emit(*args, **kwargs)
                success = True
            except Exception as e:
                last_err = e
        if not success and last_err:
            raise last_err

    def disconnect(self):
        for c in self.clients:
            try:
                c.disconnect()
            except:
                pass
                
_sio = MultiSocketClient()

def get_sio():
    _sio.setup_clients()
    return _sio

# Flag to avoid multiple manual disconnect attempts
_MANUAL_DISCONNECT = threading.Event()

def _safe_socket_disconnect(reason: str):  # pragma: no cover - network side effect
    """Manually disconnect the Socket.IO client so services can restart cleanly.

    Sets an event to prevent duplicate disconnect attempts. The automatic
    reconnection loop in `start_socket_client` is only invoked again after
    services are started in the next cycle.
    """
    global _sio
    _sio = get_sio()
    if not _sio.connected:
        return
    if _MANUAL_DISCONNECT.is_set():
        return
    try:
        logger.info(f"[CLIENT SOCKET] Manual disconnect requested: {reason}")
        _MANUAL_DISCONNECT.set()
        _sio.disconnect()
    except Exception as e:  # pragma: no cover
        logger.error(f"[CLIENT SOCKET] Manual disconnect failed: {e}")

# Set when the admin answers an identify, cleared on every new connection. The admin
# ignores an identify that reaches it before it has accepted the connection (no reply,
# nothing logged on this side), and python-socketio's own reconnect sends it the moment
# the socket is up, so an identify without an answer is sent again a few times.
_IDENTIFIED = threading.Event()
_IDENTIFY_GENERATION = 0
IDENTIFY_RETRY_DELAYS = (5, 10, 20, 40)
# The admin answers an identify it did not take ("retry": the connection is not accepted
# yet) in the acknowledgement; ask again after a short pause, a few times.
IDENTIFY_ACK_RETRIES = 3
IDENTIFY_RETRY_PAUSE = 2
_identify_retries_left = IDENTIFY_ACK_RETRIES
# The admin can still lose track of a connection that stays open (two connections
# overlapped while reconnecting and the wrong one hung up last, or every identify
# above went unanswered): it then shows the branch offline and can send it nothing
# for as long as the socket lasts. So a connected client repeats its identify this
# often; the admin answers one it already holds without doing anything.
IDENTIFY_REFRESH_SECONDS = 300
IDENTIFY_REFRESH_ACK_SECONDS = 30
IDENTIFY_REFRESH_MISSES = 2
SOCKET_CHECK_SECONDS = 30
_last_ident_payload = None
_identify_refresher_started = threading.Event()


def _emit_identify(payload: dict, generation: int):
    """Send identify and ask for the admin's acknowledgement of this very message.

    An admin from before acknowledgements answers it empty and sends the "identified"
    event instead; one with them sends both, and on_identified takes the first.
    """
    def on_ack(reply=None):
        global _identify_retries_left
        status = reply.get("status") if isinstance(reply, dict) else None
        if status == "ok":
            on_identified(reply)
        elif (status == "retry" and generation == _IDENTIFY_GENERATION
              and not _IDENTIFIED.is_set() and _identify_retries_left > 0):
            _identify_retries_left -= 1
            threading.Timer(IDENTIFY_RETRY_PAUSE, _resend_identify, args=(generation, payload)).start()
    _sio.emit('identify', payload, callback=on_ack)


def _resend_identify(generation: int, payload: dict):
    if _IDENTIFIED.is_set() or generation != _IDENTIFY_GENERATION or not _sio.connected:
        return
    try:
        _emit_identify(payload, generation)
        logger.info("[CLIENT SOCKET] No answer to identify — sent it again")
    except Exception as e:
        logger.error(f"[CLIENT SOCKET] Identify resend failed: {type(e).__name__}")


def _identify_watchdog(generation: int, payload: dict, delays=IDENTIFY_RETRY_DELAYS):
    for delay in delays:
        if _IDENTIFIED.wait(delay):
            return
        if generation != _IDENTIFY_GENERATION or not _sio.connected:
            return
        _resend_identify(generation, payload)


def _refresh_identify(payload: dict) -> threading.Event:
    """Send the repeat; the returned event is set once the admin answers it at all."""
    answered = threading.Event()

    def on_ack(reply=None):
        answered.set()
        # "known": the admin still had this connection. An admin from before repeats
        # were answered that way does not say, so this logs every time against it.
        if isinstance(reply, dict) and reply.get("status") == "ok" and not reply.get("known"):
            logger.info("[CLIENT SOCKET] The admin had lost this connection — identified again")
            on_identified(reply)
        elif isinstance(reply, dict) and reply.get("status") == "ok":
            # The repeat is the only time a connected branch hears from the admin
            # on its own, so a version assigned since it connected arrives here —
            # every IDENTIFY_REFRESH_SECONDS — and the Store page's update icon
            # turns into the alert within one round instead of at the next reconnect.
            _save_assigned_version(reply)
    _sio.emit('identify', payload, callback=on_ack)
    return answered


def _identify_refresher(interval=IDENTIFY_REFRESH_SECONDS, check_every=SOCKET_CHECK_SECONDS):
    """Repeats identify every `interval` and checks the connection every `check_every`.

    The check also runs outside open hours, when nothing else looks at the socket.
    Two repeats in a row without any answer mean the connection is dead whatever
    it is marked as: it is dropped and dialled again.
    """
    unanswered = 0
    waited = 0
    while True:
        time.sleep(check_every)
        waited += check_every
        try:
            if _sio.replace_dead_clients():
                start_socket_client()
        except Exception as e:
            logger.warning(f"[CLIENT SOCKET] Connection check failed: {type(e).__name__}")
        if waited < interval:
            continue
        waited = 0
        payload = _last_ident_payload
        if payload is None or not _sio.connected:
            continue
        try:
            answered = _refresh_identify(payload).wait(IDENTIFY_REFRESH_ACK_SECONDS)
        except Exception as e:
            logger.warning(f"[CLIENT SOCKET] Identify refresh failed: {type(e).__name__}")
            continue
        unanswered = 0 if answered else unanswered + 1
        if unanswered:
            logger.warning(f"[CLIENT SOCKET] No answer to the identify repeat ({unanswered} in a row)")
        if unanswered >= IDENTIFY_REFRESH_MISSES:
            unanswered = 0
            logger.warning("[CLIENT SOCKET] The connection answers nothing — starting a new one")
            try:
                for i in range(len(_sio.clients)):
                    _sio.replace_client(i)
                start_socket_client()
            except Exception as e:
                logger.warning(f"[CLIENT SOCKET] Reconnect failed: {type(e).__name__}")


def connect():  # pragma: no cover - network side effect
    try:
        load_config()
    except Exception as e:
        logger.error(f"[CLIENT SOCKET] Failed to load config: {e}")
        pass
    # Connected without a key while activated: the admin let us in keyless, so
    # ask for one now rather than waiting for the next app start.
    try:
        if is_activated() and not device_identity.device_key():
            _refresh_device_key_async("connected without a key")
    except Exception:
        pass
    current_version = _installed_version()

    ident_payload = {
        "command": "identify",
        "info": {
            "activateID": client_info.get("activateID"),
            "mac_address": get_mac_address(),
            "current_version": current_version,
            "version": current_version
        }
    }
    global _IDENTIFY_GENERATION, _identify_retries_left
    _IDENTIFIED.clear()
    _IDENTIFY_GENERATION += 1
    _identify_retries_left = IDENTIFY_ACK_RETRIES
    try:
        _emit_identify(ident_payload, _IDENTIFY_GENERATION)
        # Not the payload itself: the activation code and MAC address would sit
        # in a log file anyone on the Mac can read.
        logger.info(f"[CLIENT SOCKET] Sent identify (version {current_version})")
    except Exception as e:
        logger.error(f"[CLIENT SOCKET] Identify emit failed: {e}")
    threading.Thread(target=_identify_watchdog, args=(_IDENTIFY_GENERATION, ident_payload),
                     daemon=True).start()
    global _last_ident_payload
    _last_ident_payload = ident_payload
    if not _identify_refresher_started.is_set():
        _identify_refresher_started.set()
        threading.Thread(target=_identify_refresher, daemon=True).start()

    # The assigned version arrives in the 'identified' event (on_identified).
    # GET {API_SERVER_HOST}/version-manager used to be fetched here too, but that
    # is the admin's HTML page, not an API, and answers 403 once branch IPs are
    # off the whitelist.


def _installed_version():
    """Version this bundle is running — the compiled-in one first (version_store)."""
    from data_service.configs.version_store import read_client_version
    return read_client_version(os.path.dirname(os.path.abspath(__file__)), default="1.0.0")


def _listening_ports():
    """Ports this user's processes listen on, from one lsof call."""
    try:
        out = subprocess.run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-Fn"], capture_output=True,
                             text=True, timeout=10).stdout
    except Exception:
        return set()
    ports = set()
    for line in out.splitlines():
        if line.startswith("n") and ":" in line:
            try:
                ports.add(int(line.rsplit(":", 1)[1]))
            except ValueError:
                pass
    return ports


def _write_app_status_forever():
    """What the menu bar icon shows (menubar_app.py), every few seconds.

    In this user's private run folder (runtime_paths.APP_STATUS_FILE): the icon
    is a separate process and cannot ask the socket or the service table itself.
    """
    import json
    import runtime_paths
    while True:
        try:
            status = {
                "time": time.time(),
                "activated": is_activated(),
                "server": bool(_sio and _sio.connected),
                "version": _installed_version(),
            }
            try:
                halt = device_identity.key_requests_halted()
                if halt:
                    # The server picks this text: shown only as a short error code, so no key or activation code can ride along.
                    status["key_halt"] = halt if re.fullmatch(r"[a-z_]{1,40}", halt) else "refused"
            except Exception:
                pass
            try:
                ports = {name: row["port"] for name, row in port_store.get_config()["API_PORTS"].items()}
                listening = _listening_ports()
                status["services_total"] = len(ports)
                status["services_down"] = sorted(n for n, port in ports.items() if port not in listening)
            except Exception:
                pass
            runtime_paths.ensure_run_dir()
            tmp = runtime_paths.APP_STATUS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(status, f)
            os.replace(tmp, runtime_paths.APP_STATUS_FILE)
        except Exception:
            pass
        time.sleep(5)


def _socket_refused():
    """True while the admin has refused this machine and only the admin can fix it.

    Set by a 401/404/409 on activate or an unknown_activation_code on connect
    (device_identity.halt_key_requests). The admin disconnects such a socket on
    purpose and expects it to stay away, so no connect is attempted — each one
    would only cost the admin a verify-device call. The pause is stored in the
    identity store, so a restart does not lift it; it lifts itself after
    device_identity.HALT_SECONDS.
    """
    try:
        return bool(device_identity.key_requests_halted())
    except Exception:
        return False


def _socket_auth():
    """Socket.IO connect auth (contract §2). Never log the return value.

    Read from the store, not client_info: connect() reloads client_info before
    it sends identify, so a code changed while running (re-activation) would
    otherwise go out as the old one here and the new one in identify — and
    the server drops a session whose two codes differ.
    """
    try:
        code = device_identity.activation_code() or client_info.get("activateID")
    except Exception:
        code = client_info.get("activateID")
    auth = {"activation_code": code}
    try:
        key = device_identity.device_key()
    except Exception:
        key = None
    if key:
        auth["device_key"] = key
    global _LAST_AUTH_KEY
    _LAST_AUTH_KEY = key
    return auth


# The Device Key the last connect attempt presented (None: it went out keyless).
_LAST_AUTH_KEY = None


def on_connect_error(data):  # pragma: no cover - network side effect
    reason = (data or {}).get("message") if isinstance(data, dict) else None
    if reason == "invalid_device_key":
        # Contract §5: drop the key and activate again. If the server still holds
        # a key for this machine that answers 409, which pauses further attempts.
        halted = device_identity.key_requests_halted()
        current = device_identity.device_key()
        if current and current != _LAST_AUTH_KEY:
            # The refused attempt went out without this key: the startup key
            # request and the first connect run side by side, and once the key
            # is issued the admin refuses a keyless connect. That key was never
            # tried, so keep it — dropping it here left the admin holding a key
            # the machine no longer had (409 on every request, branch offline).
            logger.info("[CLIENT SOCKET] Refused a connect made before the Device Key arrived — "
                        "reconnecting with the key")
        elif current:
            logger.warning("[CLIENT SOCKET] Server rejected the Device Key — requesting a new one")
            try:
                device_identity.clear_device_key()
            except Exception as e:
                logger.error(f"[CLIENT SOCKET] Could not clear Device Key: {e}")
            _refresh_device_key_async("invalid_device_key")
        elif halted:
            # Every reconnect attempt lands here while paused; say what is
            # actually happening instead of claiming a request is being made.
            logger.warning(f"[CLIENT SOCKET] Server refuses this machine without a Device Key; key requests "
                           f"paused ({halted}) — the admin must clear this machine's key")
        else:
            logger.warning("[CLIENT SOCKET] Server wants a Device Key — requesting one")
            _refresh_device_key_async("invalid_device_key")
    elif reason == "unknown_activation_code":
        logger.error("[CLIENT SOCKET] Server does not know this activation code — contact the admin")
        try:
            device_identity.halt_key_requests("unknown_activation_code")
        except Exception:
            pass
    else:
        logger.warning(f"[CLIENT SOCKET] Connection error ({type(data).__name__})")

def on_identified(data):  # pragma: no cover
    if _IDENTIFIED.is_set():   # the acknowledgement and the event both carry the answer
        return
    # Not the whole response: it carries this branch's activation code.
    _assigned = data.get("assigned_version") if isinstance(data, dict) else None
    logger.info(f"[CLIENT SOCKET] Identified (assigned_version={_assigned})")
    _sio.is_identified = True
    _IDENTIFIED.set()
    _save_assigned_version(data)


def _save_assigned_version(data):
    """Keep the version the admin assigned, from any answer to an identify that carries it.

    Saved even when empty: the update check reads it (the admin's branch APIs
    are office-only now), and a stale value would advertise an update the
    admin already withdrew. Kept in the database, not a file a shop can edit.
    """
    if not (isinstance(data, dict) and "assigned_version" in data):
        return
    assigned_version = data.get("assigned_version")
    try:
        from data_service.db.crud.client_state_crud import set_state, ASSIGNED_VERSION
        set_state(ASSIGNED_VERSION, str(assigned_version) if assigned_version else None)
    except Exception as e:
        logger.error(f"[CLIENT SOCKET] Failed to save assigned_version: {e}")

def on_check_for_updates(data):  # pragma: no cover
    manifest = get_client_manifest()
    _sio.emit('manifest', manifest)
    logger.info(f"[CLIENT SOCKET] Sent manifest with {len(manifest)} entries")

def on_delete_file(data):  # pragma: no cover
    rel = data.get('filename') if isinstance(data, dict) else None
    if not rel:
        return
    target = os.path.join(MANAGED_PATH, rel)
    try:
        if os.path.exists(target):
            os.remove(target)
            parent = os.path.dirname(os.path.abspath(target))
            root_abs = os.path.abspath(MANAGED_PATH)
            while parent.startswith(root_abs) and parent != root_abs:
                try:
                    if not os.listdir(parent):
                        os.rmdir(parent)
                        parent = os.path.dirname(parent)
                    else:
                        break
                except Exception:
                    break
            logger.debug("[CLIENT SOCKET] Deleted a delivered file")
    except Exception as e:
        logger.error(f"[CLIENT SOCKET] Failed deleting a delivered file: {type(e).__name__}")

def on_add_file(data):  # pragma: no cover
    global _DLC_LAST_FILE_AT
    if not isinstance(data, dict):
        return
    rel = data.get('filename')
    if rel:
        _DLC_LAST_FILE_AT = time.monotonic()
        with _UPDATED_FOLDERS_LOCK:
            _UPDATED_FOLDERS.add(rel.split('/')[0])
    content_b64 = data.get('content')
    expected_hash = data.get('hash')
    is_encrypted = data.get('enc') is True
    # No file name: the list of delivered files is a map of the services.
    logger.debug(f"[CLIENT SOCKET] on_add_file (enc={is_encrypted}, hash={bool(expected_hash)})")
    if not rel or content_b64 is None:
        return
    try:
        raw = base64.b64decode(content_b64.encode('ascii'))
    except Exception:
        return
    plaintext = raw
    if is_encrypted:
        cipher = get_dlc_cipher()
        if cipher is None:
            logger.error(f"[CLIENT SOCKET] STATIC_LEGACY_KEY not set; cannot decrypt a file of {rel.split('/')[0]}")
            return
        try:
            plaintext = cipher.decrypt(raw)
        except Exception:
            logger.error(f"[CLIENT SOCKET] Decryption failed for a file of {rel.split('/')[0]}")
            return
    if expected_hash:
        sha = hashlib.sha256(); sha.update(plaintext)
        if sha.hexdigest() != expected_hash:
            logger.warning(f"[CLIENT SOCKET] Hash mismatch for a file of {rel.split('/')[0]}; skipping write")
            return
    dest = os.path.join(MANAGED_PATH, rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    # The .app keeps a service's .py exactly as it arrived (encrypted); the import
    # hook decrypts it in memory (dlc_import_hook.py). Writing the plaintext left
    # every service's source readable in the customer's Library folder. A branch
    # installed by git clone has no hook in its spawned children, so it still
    # writes plaintext.
    keep_encrypted = is_encrypted and rel.endswith('.py') and _dlc_kept_encrypted()
    try:
        with open(dest, 'wb') as f:
            f.write(raw if keep_encrypted else plaintext)
    except Exception:
        return


# A large DLC file (a model) arrives in parts: add_file_begin, add_file_chunk...,
# add_file_end. One message per file used to hold the whole file in memory on
# both sides and starve the connection's pings until it dropped halfway. Each
# handler's return value is the admin's acknowledgement: it sends the next part
# only after True, and stops the file on anything else. Parts are written to a
# .dlcpart file beside the destination, which replaces it only once size and
# sha256 match; a transfer that stops leaves the old file in place.
_PART_SUFFIX = ".dlcpart"
_CHUNKED_FILES = {}
_CHUNKED_LOCK = threading.Lock()


def _managed_dest(rel):
    """Where a delivered file goes; None when its name points outside managed_services."""
    if not isinstance(rel, str) or not rel:
        return None
    root = os.path.realpath(MANAGED_PATH)
    dest = os.path.realpath(os.path.join(root, rel))
    return dest if dest.startswith(root + os.sep) else None


def _drop_chunked(rel):
    with _CHUNKED_LOCK:
        state = _CHUNKED_FILES.pop(rel, None)
    if state:
        try:
            state["file"].close()
        except Exception:
            pass
        try:
            os.remove(state["part"])
        except OSError:
            pass


def on_dlc_caps(data=None):  # pragma: no cover
    """What this client can receive, asked by the admin before an update with large files.

    A client from before files in parts does not answer, and the admin then holds
    the whole update back from it: new services expect models it could not receive,
    so the branch keeps running the services it has until its client is updated.
    """
    return {"chunked_files": True}


def on_add_file_begin(data):  # pragma: no cover
    global _DLC_LAST_FILE_AT
    rel = data.get('filename') if isinstance(data, dict) else None
    dest = _managed_dest(rel)
    if dest is None or not isinstance(data.get('hash'), str) or not isinstance(data.get('size'), int):
        return False
    _drop_chunked(rel)
    _DLC_LAST_FILE_AT = time.monotonic()
    with _UPDATED_FOLDERS_LOCK:
        _UPDATED_FOLDERS.add(rel.split('/')[0])
    part = dest + _PART_SUFFIX
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        handle = open(part, 'wb')
    except Exception as e:
        logger.error(f"[CLIENT SOCKET] Cannot receive a large file of {rel.split('/')[0]}: {type(e).__name__}")
        return False
    with _CHUNKED_LOCK:
        _CHUNKED_FILES[rel] = {"file": handle, "part": part, "dest": dest, "sha": hashlib.sha256(),
                               "next": 0, "received": 0}
    logger.info(f"[CLIENT SOCKET] Receiving a large file of {rel.split('/')[0]} "
                f"({data['size'] / 1e6:.1f} MB in {data.get('parts')} parts)")
    return True


def on_add_file_chunk(data):  # pragma: no cover
    global _DLC_LAST_FILE_AT
    rel = data.get('filename') if isinstance(data, dict) else None
    with _CHUNKED_LOCK:
        state = _CHUNKED_FILES.get(rel)
    if state is None or data.get('index') != state["next"]:
        _drop_chunked(rel)
        return False
    try:
        block = base64.b64decode(data.get('content') or b'')
        state["file"].write(block)
    except Exception:
        _drop_chunked(rel)
        return False
    state["sha"].update(block)
    state["received"] += len(block)
    state["next"] += 1
    _DLC_LAST_FILE_AT = time.monotonic()
    return True


def on_add_file_end(data):  # pragma: no cover
    global _DLC_LAST_FILE_AT
    rel = data.get('filename') if isinstance(data, dict) else None
    with _CHUNKED_LOCK:
        state = _CHUNKED_FILES.pop(rel, None)
    if state is None:
        return False
    try:
        state["file"].close()
        if state["received"] != data.get('size') or state["sha"].hexdigest() != data.get('hash'):
            logger.warning(f"[CLIENT SOCKET] A large file of {rel.split('/')[0]} arrived damaged; "
                           f"kept the old one")
            os.remove(state["part"])
            return False
        os.replace(state["part"], state["dest"])
    except Exception as e:
        logger.error(f"[CLIENT SOCKET] Could not keep a large file of {rel.split('/')[0]}: {type(e).__name__}")
        try:
            os.remove(state["part"])
        except OSError:
            pass
        return False
    _DLC_LAST_FILE_AT = time.monotonic()
    logger.info(f"[CLIENT SOCKET] Received a large file of {rel.split('/')[0]}")
    return True


def on_main_update_begin(data):
    # Prepare temp_update directory for main client update
    temp_dir = os.path.join(os.path.dirname(__file__), 'temp_update')
    if os.path.exists(temp_dir):
        try:
            shutil.rmtree(temp_dir)
        except Exception as e:
            logger.error(f"[CLIENT SOCKET] Failed to clear temp_update: {e}")
    os.makedirs(temp_dir, exist_ok=True)
    logger.info("[CLIENT SOCKET] Starting main client update. temp_update folder prepared.")

def on_main_update_file(data):
    if not isinstance(data, dict): return
    rel = data.get('filename')
    content_b64 = data.get('content')
    if not rel or not content_b64: return
    
    try:
        raw = base64.b64decode(content_b64.encode('ascii'))
    except Exception: return
    
    dest = os.path.join(os.path.dirname(__file__), 'temp_update', rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    try:
        with open(dest, 'wb') as f:
            f.write(raw)
        logger.debug("[CLIENT SOCKET] Received an update file")
    except Exception as e:
        logger.error(f"[CLIENT SOCKET] Failed to save an update file: {type(e).__name__}")

def on_main_update_complete(data):
    logger.info("[CLIENT SOCKET] Main client update download complete")
    # Write VERSION file if admin provided it
    version = data.get('version') if isinstance(data, dict) else None
    if version:
        try:
            version_path = os.path.join(os.path.dirname(__file__), 'temp_update', 'VERSION')
            os.makedirs(os.path.dirname(version_path), exist_ok=True)
            with open(version_path, 'w') as f:
                f.write(version + '\n')
        except Exception as e:
            logger.error(f"[CLIENT SOCKET] Failed to write VERSION: {e}")
        # Write client_version.json immediately so re-identify reports new version —
        # unless the payload carries its version compiled in (_build_info.so),
        # which is what the client reads first once the update is applied.
        from data_service.configs.version_store import has_baked_module
        if not has_baked_module(os.path.join(os.path.dirname(__file__), 'temp_update')):
            try:
                vpath = os.path.join(os.path.dirname(__file__), 'client_version.json')
                import json as _j
                with open(vpath, 'w') as f:
                    _j.dump({"version": version}, f)
                logger.info(f"[CLIENT SOCKET] Updated client_version.json to {version}")
            except Exception as e:
                logger.error(f"[CLIENT SOCKET] Failed to update client_version.json: {e}")
    _safe_socket_disconnect("main_update_complete")
    
    root_dir = os.path.dirname(__file__)
    updater_path = os.path.join(root_dir, 'updater.py')
    if os.path.exists(updater_path):
        import platform
        if platform.system() == "Windows":
            subprocess.Popen([sys.executable, 'updater.py'], cwd=root_dir, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        else:
            subprocess.Popen([sys.executable, 'updater.py'], cwd=root_dir, start_new_session=True)
        logger.info("[CLIENT SOCKET] Stopping all services before terminating...")
        stop_all_services()
        # Hard exit — updater.py will restart everything
        import os as _os
        _os._exit(0)
    else:
        logger.error("[CLIENT SOCKET] updater.py not found! Cannot apply update.")

def on_delete_folder(data):
    folder = data.get('folder') if isinstance(data, dict) else None
    if not folder:
        return
    target = os.path.join(MANAGED_PATH, folder)
    if os.path.isdir(target):
        try:
            import shutil as _sh
            _sh.rmtree(target)
        except Exception:
            pass
    # The admin took this service away from the branch, so it gives up its row
    # too — otherwise it would sit in the port page forever, permanently
    # offline, offering to edit the port of something that cannot run.
    try:
        port_store.unregister(folder.split('/')[0])
    except Exception as e:
        logger.error(f"[PORTS] Could not unregister removed folder '{folder}': {e}")
    # The supervisor stops the service and drops its runner in its next restart round,
    # whatever events follow (a Save sends dlc_update_complete after its deletions);
    # the socket thread never touches the process table.
    with _UPDATED_FOLDERS_LOCK:
        _DELETED_FOLDERS.add(folder.split('/')[0])
    RESTART_EVENT.set()

def on_service_update_begin(data):
    pass

on_dlc_update_begin = on_service_update_begin

def on_service_folder_begin(data):
    folder = data.get('folder') if isinstance(data, dict) else None
    if not folder:
        return
    with _UPDATED_FOLDERS_LOCK:
        _UPDATED_FOLDERS.add(folder.split('/')[0])

def on_service_folder_complete(data):
    pass

def on_service_update_complete(data):
    """The transfer is complete: its folders are handed to the next restart round."""
    global _DLC_LAST_FILE_AT
    with _UPDATED_FOLDERS_LOCK:
        _DLC_LAST_FILE_AT = None
        folders_snapshot = set(_UPDATED_FOLDERS)
        _UPDATED_FOLDERS.clear()
    # Auto-install dependencies if requirements.txt exists in the updated DLC
    for folder in folders_snapshot:
        req_path = os.path.join(MANAGED_PATH, folder, 'requirements.txt')
        if os.path.exists(req_path):
            logger.info(f"[DLC] Found requirements.txt in {folder}. Installing dependencies...")
            try:
                subprocess.run([sys.executable, '-m', 'pip', 'install', '-r', req_path], check=True)
                logger.info(f"[DLC] Successfully installed dependencies for {folder}")
            except subprocess.CalledProcessError as e:
                logger.error(f"[DLC] Failed to install dependencies for {folder}: {e}")
            except Exception as e:
                logger.error(f"[DLC] Error running pip for {folder}: {e}")
    with _UPDATED_FOLDERS_LOCK:
        _COMPLETED_FOLDERS.update(folders_snapshot)
    logger.info(f"[CLIENT] DLC update complete: {sorted(folders_snapshot)}")
    # Disconnect the socket so we only reconnect after services are restarted
    _safe_socket_disconnect("service_update_complete")
    RESTART_EVENT.set()


def _apply_dlc_changes(deleted, completed):
    """In a restart round (supervisor thread): drop the runners of removed services and
    register delivered folders. Returns the services to restart and whether that is every
    DLC service."""
    stop = set()
    for folder in deleted:
        runner = FOLDER_TO_SERVICE_FUNC.get(folder)
        if isinstance(runner, ManagedServiceRunner) and not os.path.isdir(os.path.join(MANAGED_PATH, folder)):
            del FOLDER_TO_SERVICE_FUNC[folder]
            if runner in ACTIVE_SERVICE:
                ACTIVE_SERVICE.remove(runner)
            _stop_for_restart(runner)
            logger.info(f"[CLIENT] '{folder}' removed by the admin — {runner.__name__} stopped for good.")
    if not completed:
        return stop, False
    # A folder landing here is the moment this branch actually owns that
    # service, so that — not install time — is when it earns a row in the port
    # table. Registration only ever inserts: a branch that moved this service's
    # port keeps its own number when a new version of the folder arrives.
    # A folder that just arrived may be a service this build has no runner for;
    # give it a generic one first so it earns a port row and gets started.
    _discover_managed_services()
    every_dlc = False
    for folder in completed:
        if not os.path.isdir(os.path.join(MANAGED_PATH, folder)):
            continue  # removed, not delivered: no row back, nothing to restart
        try:
            port_store.register_delivered(folder, set(FOLDER_TO_SERVICE_FUNC))
        except Exception as e:
            logger.error(f"[PORTS] Could not register delivered folder '{folder}': {e}")
        fn = FOLDER_TO_SERVICE_FUNC.get(folder)
        if fn:
            stop.add(fn)
        else:  # no entry point = shared code (configs, events, ai_camera_db) every DLC service imports
            every_dlc = True
    return stop, every_dlc

on_dlc_update_complete = on_service_update_complete


def _finish_stalled_dlc_transfer(clock=time.monotonic):
    """Apply a DLC transfer whose dlc_update_complete never arrived.

    The admin sends every file and then that one event. On the branches
    upgraded so far the socket dropped ("packet queue is empty, aborting")
    seconds after the last file, the event was lost with it, and the delivered
    services sat on disk unregistered and unstarted until someone restarted
    the client by hand. Once no file has arrived for a while, do what the
    event would have done.
    """
    last = _DLC_LAST_FILE_AT
    if last is None or clock() - last < DLC_COMPLETE_GRACE_SECONDS:
        return
    with _UPDATED_FOLDERS_LOCK:
        pending = sorted(_UPDATED_FOLDERS)
    if not pending:
        return
    for rel in list(_CHUNKED_FILES):
        _drop_chunked(rel)
    logger.warning(f"[CLIENT] DLC transfer ended without dlc_update_complete "
                   f"({DLC_COMPLETE_GRACE_SECONDS}s since the last file) — applying {pending}")
    on_service_update_complete(None)

def on_update_complete(data):  # pragma: no cover
    logger.info("[CLIENT SOCKET] Update cycle complete")
    # Signal main loop to restart services to pick up code changes
    for fn in ACTIVE_SERVICE:
        _UPDATED_SERVICE_FUNCS.add(fn)
    # Optional: also perform a disconnect in generic update_complete (kept for consistency)
    _safe_socket_disconnect("update_complete")
    RESTART_EVENT.set()

def on_trigger_build(data):
    """Triggered by Admin Server to run build_mac_app.sh and upload the zip payload."""
    logger.info("[BUILD FARM] Received trigger_build command")
    
    upload_url = data.get("upload_url")
    if not upload_url:
        logger.error("[BUILD FARM] No upload_url provided in trigger_build payload.")
        return

    def build_worker():
        root_dir = os.path.dirname(__file__)
        build_script = os.path.join(root_dir, "build_mac_app.sh")
        if not os.path.exists(build_script):
            logger.error("[BUILD FARM] build_mac_app.sh not found!")
            return
            
        logger.info("[BUILD FARM] Running build_mac_app.sh...")
        try:
            subprocess.run(["bash", "build_mac_app.sh"], cwd=root_dir, check=True)
            logger.info("[BUILD FARM] Build complete. Creating zip payload...")
            
            # The build script puts the app bundle in release/AICamera.app
            release_dir = os.path.join(root_dir, "release")
            app_bundle = os.path.join(release_dir, "AICamera.app")
            if not os.path.exists(app_bundle):
                logger.error("[BUILD FARM] AICamera.app not found in release directory.")
                return
                
            # Zip the app bundle
            zip_path = os.path.join(release_dir, "AICamera_update.zip")
            if os.path.exists(zip_path):
                os.remove(zip_path)
                
            subprocess.run(["zip", "-r", "AICamera_update.zip", "AICamera.app"], cwd=release_dir, check=True)
            
            # Upload to the provided URL
            logger.info("[BUILD FARM] Uploading the update zip ...")
            with open(zip_path, 'rb') as f:
                files = {'file': ('AICamera_update.zip', f, 'application/zip')}
                # Only attached when upload_url is the admin host (contract §3).
                response = requests.post(upload_url, files=files,
                                         headers=device_identity.admin_auth_headers(upload_url))
                
            if response.status_code == 200:
                logger.info("[BUILD FARM] Upload successful!")
            elif response.status_code == 401:
                logger.error("[BUILD FARM] Upload refused: Device Key rejected — requesting a new one")
                device_identity.clear_device_key()
                _refresh_device_key_async("build upload 401")
            else:
                logger.error(f"[BUILD FARM] Upload failed: HTTP {response.status_code}")
                
        except subprocess.CalledProcessError as e:
            logger.error(f"[BUILD FARM] Build or zip process failed: {e}")
        except Exception as e:
            logger.error(f"[BUILD FARM] Unexpected error during build/upload: {e}")

    # Run in background to avoid blocking the socket thread
    threading.Thread(target=build_worker, daemon=True).start()

def disconnect():  # pragma: no cover
    logger.info("[CLIENT SOCKET] Disconnected from update server")

def _reset_http_session(sio_instance):
    """Give the client a brand-new HTTP session and drop the old pool.

    A store's Wi-Fi drops for a second and the process never recovers: every
    later attempt keeps failing with EHOSTUNREACH while ping, curl and a fresh
    process on the same machine all reach the server fine. The dead state is
    therefore inside this session's connection pool, not on the network, and
    restarting the whole app was the only thing that cleared it. Rebuilding the
    one object that holds it does the same job without a restart.
    """
    eio = getattr(sio_instance, 'eio', None)
    if eio is None or not hasattr(eio, 'http'):
        return
    try:
        old_session = eio.http
        if old_session is not None:
            try:
                old_session.close()
            except Exception:
                pass
        fresh = requests.Session()
        fresh.verify = False
        eio.http = fresh
        logger.info("[CLIENT SOCKET] Rebuilt HTTP session after a failed round of attempts")
    except Exception as e:
        logger.warning(f"[CLIENT SOCKET] Could not rebuild HTTP session: {e}")


OWN_RECONNECT_WAIT_SECONDS = 60


def _wait_for_own_reconnect(sio_instance, limit=OWN_RECONNECT_WAIT_SECONDS) -> bool:
    """Leave a reconnect python-socketio is running by itself alone. True once it got through.

    It marks the client connected only after connect() has run, so meanwhile the
    loop below saw "not connected" with Engine.IO up and disconnected it — under the
    reconnect, which then marked a dead client connected (UNS089, 30 Oct). A reconnect
    still failing after `limit` seconds is stopped, and the loop takes over: its
    rounds also rebuild the HTTP session, which python-socketio's own never does.
    """
    deadline = time.monotonic() + limit
    while _reconnecting(sio_instance) and time.monotonic() < deadline:
        time.sleep(1.0)
    if _link_up(sio_instance):
        return True
    if _reconnecting(sio_instance):
        logger.warning("[CLIENT SOCKET] Reconnect by python-socketio still failing — taking over")
        try:
            sio_instance._reconnect_abort.set()
            sio_instance._reconnect_task.join(10)
        except Exception:
            pass
    return False


def _start_single_client(sio_instance, max_attempts: int = 5):
    cooldown = 30
    base_delay = 1.0
    
    while True:
        delay = base_delay
        for attempt in range(1, max_attempts + 1):
            if sio_instance.connected:
                return
            if _socket_refused():
                logger.warning("[CLIENT SOCKET] Admin refused this machine — not reconnecting until the admin fixes it")
                return
            if _wait_for_own_reconnect(sio_instance):
                return
            try:
                eio_state = sio_instance.eio.state if (hasattr(sio_instance, 'eio') and sio_instance.eio) else 'unknown'
                
                if sio_instance.connected or eio_state != 'disconnected':
                    try:
                        sio_instance.disconnect()
                        time.sleep(1.0)
                    except Exception as e:
                        pass
                
                url = sio_instance.server_info['url']
                path = sio_instance.server_info['path']
                logger.info(f"[CLIENT SOCKET] Connecting to the server (attempt {attempt}/{max_attempts})...")
                # A function, not a dict: python-socketio calls it again on every
                # reconnect, so a key fetched after the first attempt is picked up.
                sio_instance.connect(url, socketio_path=path, wait=True, auth=_socket_auth)
                logger.info("[CLIENT SOCKET] Connected to the server.")
                return
            except Exception as e:
                if sio_instance.connected:
                    # python-socketio reconnected on its own while this attempt was
                    # running; connect() then raises "Already connected". Dropping that
                    # socket here left the branch connected but unknown to the admin.
                    logger.info("[CLIENT SOCKET] Already connected — keeping the connection")
                    return
                logger.error(f"[CLIENT SOCKET] Connection attempt {attempt} failed: {type(e).__name__}")
                try:
                    sio_instance.disconnect()
                except:
                    pass
                if attempt < max_attempts:
                    time.sleep(delay)
                    delay = min(delay * 2, 10)
                else:
                    logger.warning(f"[CLIENT SOCKET] Max attempts reached. Cooling down {cooldown}s...")
                    _reset_http_session(sio_instance)
        try:
            time.sleep(cooldown)
        except KeyboardInterrupt:
            return

_CONNECT_THREADS = {}   # id(sio client) -> thread currently trying to connect it


def start_socket_client(max_attempts: int = 5):
    """Start all Socket.IO clients in parallel.

    Safe to call repeatedly: a client that already has a connect loop running
    is left alone, so the watchdog in the store-open loop can call this every
    few seconds without stacking up parallel connect attempts.
    """
    global _sio
    _sio = get_sio()
    _sio.replace_dead_clients()

    for client in _sio.clients:
        if client.connected:
            continue
        running = _CONNECT_THREADS.get(id(client))
        if running and running.is_alive():
            continue
        t = threading.Thread(target=_start_single_client, args=(client, max_attempts), daemon=True)
        _CONNECT_THREADS[id(client)] = t
        t.start()

ACTIVATION_API_PORT = port_store.get_port("activation_service", 8001)

client_info = {}
RESTART_EVENT = threading.Event()
_UPDATED_FOLDERS_LOCK = threading.Lock()
_UPDATED_FOLDERS = set()
# When the last DLC file of a transfer arrived (None: no transfer pending).
_DLC_LAST_FILE_AT = None
# How long after the last file a transfer counts as over without dlc_update_complete.
DLC_COMPLETE_GRACE_SECONDS = 120
_UPDATED_SERVICE_FUNCS = set()
# What socket threads ask of the next restart round, under _UPDATED_FOLDERS_LOCK
# (only the supervisor thread changes the process table):
_COMPLETED_FOLDERS = set()   # folders of transfers that are complete
_DELETED_FOLDERS = set()     # folders the admin removed
_RESTART_EVERY_DLC = False   # the Device Key changed




# --------------------------------------------------------------------------------------
# FastAPI application for activation service
# --------------------------------------------------------------------------------------
from pydantic import BaseModel as _ActivationBaseModel


class ActivationRequest(_ActivationBaseModel):
    """Body of POST /trigger_activate.

    Module level on purpose. Cython keeps annotations as strings, so FastAPI
    has to evaluate "item: ActivationRequest" against the module globals to
    decide whether it is a body or a query parameter. Declared inside
    run_activation_server() the name is not there to be found, FastAPI falls
    back to treating it as a query parameter, and the endpoint answers every
    activation attempt with {"loc": ["query", "item"], "msg": "Field required"}
    — i.e. a compiled build cannot be activated at all, while running the same
    file as plain .py works fine.
    """
    activateID: str


def run_activation_server():
    """Starts the Uvicorn server for the activation API."""
    import uvicorn
    from fastapi import FastAPI

    # Loopback-only + no API docs: this API is reached through the frontend
    # proxy on the same machine, never directly from the LAN.
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/trigger_activate")
    async def create_item(item: ActivationRequest):
        # A notification, not an instruction: data_service has already stored
        # the code (and Device Key) the admin accepted. The body used to be
        # saved as-is, so any local process could POST a made-up code here and
        # the supervisor would start every service as if activated.
        client_info = load_config()
        if not device_identity.is_activated():
            return {"status": False, "message": "Not activated."}
        activate_id = client_info.get("activateID")

        # Trigger identity update so the central server marks it as Online
        logger.info("[API_SERVER] Forcing socket reconnect with the new activation code")
        try:
            if _sio and _sio.connected:
                _sio.disconnect()
        except Exception as e:
            logger.error(f"[API_SERVER] Disconnect failed: {e}")
            
        return {"status": True , "message": "Activation ID has been queued for processing."}

    logger.info(f"🚀 Starting Activation API server on port {ACTIVATION_API_PORT}...")
    try:
        uvicorn.run(app, host="127.0.0.1", port=ACTIVATION_API_PORT)
    except Exception as e:
        logger.error(f"[API_SERVER] A fatal error occurred: {e}")




# --------------------------------------------------------------------------------------
# Encryption and Decryption Functions
# --------------------------------------------------------------------------------------
def encrypt_json_file(data_dict, filepath):
    """
    Serializes a Python dictionary to a JSON string, then encrypts and saves it.
    
    Args:
        data_dict (dict): The Python dictionary to encrypt.
        filepath (str): The path to the file where the encrypted data will be saved.
    """
    json_string = json.dumps(data_dict, indent=4)
    content_bytes = json_string.encode('utf-8')
    cipher = get_dlc_cipher()
    if cipher is None:
        raise RuntimeError("STATIC_LEGACY_KEY not set; cannot encrypt")
    encrypted_bytes = cipher.encrypt(content_bytes)
    encrypted_content = encrypted_bytes.decode('utf-8')
    
    with open(filepath, 'w') as f:
        f.write(encrypted_content)
    logger.info("JSON data successfully encrypted and saved")

def decrypt_json_file(filepath):
    """
    Reads and decrypts a file, then parses the JSON string into a Python dictionary.
    
    Args:
        filepath (str): The path to the encrypted file.
        
    Returns:
        dict: The decrypted and parsed Python dictionary.
    """
    with open(filepath, 'r') as f:
        encrypted_content = f.read()
    cipher = get_dlc_cipher()
    if cipher is None:
        raise RuntimeError("STATIC_LEGACY_KEY not set; cannot decrypt")
    decrypted_bytes = cipher.decrypt(encrypted_content.encode('utf-8'))
    decrypted_json_string = decrypted_bytes.decode('utf-8')
    data_dict = json.loads(decrypted_json_string)
    return data_dict

def encrypt_file(content_to_encrypt, filepath):
    """Encrypts a string, encodes it, and saves it to a file."""
    content_bytes = content_to_encrypt.encode('utf-8')
    cipher = get_dlc_cipher()
    if cipher is None:
        raise RuntimeError("STATIC_LEGACY_KEY not set; cannot encrypt")
    encrypted_bytes = cipher.encrypt(content_bytes)
    encrypted_content = encrypted_bytes.decode('utf-8')
    with open(filepath, 'w') as f:
        f.write(encrypted_content)




# --------------------------------------------------------------------------------------
# Configuration Management Functions
# --------------------------------------------------------------------------------------
def load_config():
    """Refresh client_info from the encrypted identity store and return it.

    client_info holds the public fields only (client_id, mac, serial_number,
    activateID, start_date, activated) — never device_key, so logging it stays
    safe. The store itself creates/migrates on first use, including the old
    client_config.json + device_info.json.
    """
    global client_info
    try:
        client_info = device_identity.load()
    except Exception as e:
        logger.error(f"[CLIENT] Could not read identity store: {e}")
        client_info = dict(client_info)
    _export_identity_env()
    return client_info


def save_config():
    """Kept for callers that change client_info; only the fields the supervisor owns are written."""
    try:
        device_identity.update(**{k: client_info.get(k) for k in ("client_id", "start_date") if client_info.get(k)})
    except Exception as e:
        logger.error(f"[CLIENT] Error saving identity store: {e}")


_EXPORTED_KEY_FINGERPRINT = None


def _export_identity_env():
    """Hand the activation code + Device Key to the DLC services through the environment.

    They are separate processes (multiprocessing inherits os.environ at start);
    letting them read the store — or the Keychain — themselves is what the
    contract rules out. A key that changes while services are up restarts them
    so none keeps uploading with the old one.
    """
    global _EXPORTED_KEY_FINGERPRINT, _RESTART_EVERY_DLC
    code = client_info.get("activateID") or ""
    try:
        key = device_identity.device_key() or ""
    except Exception:
        key = ""
    for name, value in (("AI_CAMERA_ACTIVATION_CODE", code), ("AI_CAMERA_DEVICE_KEY", key)):
        if value:
            os.environ[name] = value
        else:
            os.environ.pop(name, None)
    fingerprint = hashlib.sha256(key.encode()).hexdigest() if key else ""
    if _EXPORTED_KEY_FINGERPRINT is not None and fingerprint != _EXPORTED_KEY_FINGERPRINT:
        logger.info("[CLIENT] Device Key changed — restarting DLC services so they pick it up")
        with _UPDATED_FOLDERS_LOCK:
            _RESTART_EVERY_DLC = True
        RESTART_EVENT.set()
    _EXPORTED_KEY_FINGERPRINT = fingerprint


_KEY_REFRESH_LOCK = threading.Lock()


def _refresh_device_key_async(reason: str):
    """Ask the admin for a Device Key in the background (never blocks the socket thread)."""
    def worker():
        if not _KEY_REFRESH_LOCK.acquire(blocking=False):
            return
        try:
            from data_service.utils.validate_user_credentials import refresh_device_key
            outcome = refresh_device_key()
            if outcome == "ok":
                logger.info(f"[CLIENT] Device Key received ({reason})")
                load_config()
                _safe_socket_disconnect("device key refreshed")
            elif outcome == "halt":
                logger.error(f"[CLIENT] Admin refused a Device Key ({reason}): "
                             f"{device_identity.key_requests_halted()}. Contact the admin to clear this "
                             f"machine's key; automatic requests are paused for 24h.")
            elif outcome == "retry":
                logger.warning(f"[CLIENT] Device Key request failed ({reason}); will retry later")
        except Exception as e:
            logger.error(f"[CLIENT] Device Key refresh error: {e}")
        finally:
            _KEY_REFRESH_LOCK.release()
    threading.Thread(target=worker, daemon=True).start()




# --------------------------------------------------------------------------------------
# DLC Management Functions
# --------------------------------------------------------------------------------------
def get_file_hash(filepath):
    if not os.path.exists(filepath): return None
    sha256_hash = hashlib.sha256()
    with open(filepath, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()

def get_client_manifest():
    os.makedirs(MANAGED_PATH, exist_ok=True)
    # A .dlcpart is a large file still arriving, or one that stopped halfway.
    return dlc_manifest.tree_sha256(MANAGED_PATH, get_dlc_cipher,
                                    skip=lambda key: key.endswith(_PART_SUFFIX))



# --------------------------------------------------------------------------------------
# Service Management Functions
# --------------------------------------------------------------------------------------
FILE_PATHS = {
    "data_service": {
        "main": "ai-camera-client/data_service/main.py",
        "api": {
            "cameras": "ai-camera-client/data_service/api/cameras.py",
            "data": "ai-camera-client/data_service/api/data.py",
            "store": "ai-camera-client/data_service/api/store.py",
            "users": "ai-camera-client/data_service/api/users.py"
        },
        "configs": {
            "client_api_config": "ai-camera-client/data_service/configs/client_api_config.py",
            "db_config": "ai-camera-client/data_service/configs/db_config.py",
            "search_camera_config": "ai-camera-client/data_service/configs/search_camera_config.py",
            "server_api_config": "ai-camera-client/data_service/configs/server_api_config.py",
            "static_path_config": "ai-camera-client/data_service/configs/static_path_config.py"

        },
        "db": {
            "session": "ai-camera-client/data_service/db/session.py",
            "crud": {
                "camera_crud": "ai-camera-client/data_service/db/crud/camera_crud.py",
                "store_crud": "ai-camera-client/data_service/db/crud/store_crud.py",
                "user_db": "ai-camera-client/data_service/db/crud/user_db.py"
            }

        },
        "schemas": {
            "camera_schemas": "ai-camera-client/data_service/schemas/camera_schemas.py",
            "store_schemas": "ai-camera-client/data_service/schemas/store_schemas.py",
            "user_schemas": "ai-camera-client/data_service/schemas/user_schemas.py"

        },
        "utils": {
            "data_api_utils": "ai-camera-client/data_service/utils/data_api_utils.py",
            "search_camera_utils": "ai-camera-client/data_service/utils/search_camera_utils.py",
            "security_utils": "ai-camera-client/data_service/utils/security_utils.py",
            "validate_user_credentials": "ai-camera-client/data_service/utils/validate_user_credentials.py"
            
        }
    },
    "frontend_service": {
        "main": "frontend",
    }
}


def _service_port(service_key: str, default_port: int) -> int:
    """Port for a service, read from the port table at every start.

    The module-reload dance this replaced existed because the config was an
    imported .py; a row is read fresh every call instead. A service with no
    row yet (its DLC has never been delivered here) falls back to the
    documented default rather than refusing to start.
    """
    return port_store.get_port(service_key, default_port)

# Docs endpoints are stripped from every service app at load time — services
# ship compiled (no source to edit) and must not publish an API map on the LAN.
_DOCS_ROUTE_PATHS = {"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"}

def _strip_docs_routes(fastapi_app):
    """Remove /docs, /redoc and /openapi.json from a FastAPI app instance."""
    try:
        routes = fastapi_app.router.routes
        kept = [r for r in routes if getattr(r, "path", None) not in _DOCS_ROUTE_PATHS]
        if len(kept) != len(routes):
            fastapi_app.router.routes[:] = kept
            logger.info("🔒 Docs endpoints (/docs /redoc /openapi.json) disabled for loaded service")
    except Exception as e:
        logger.warning(f"⚠️ Could not strip docs routes: {e}")

def _service_bind_host(default: str = "127.0.0.1") -> str:
    """Bind address for internal services — loopback; see port_store.API_HOST."""
    host = str(getattr(port_store, "API_HOST", "") or "").strip()
    return host or default

def run_data_service():
    _load_service_app('data_service', 'data_service', 'data_service', _service_port('data_service', 8000))

def run_frontend_service():
    logger.info("Attempting to run service: frontend_service (Python)")
    base_dir = os.path.dirname(os.path.abspath(__file__))
    frontend_root = os.path.join(base_dir, "frontend_web")
    if not os.path.isdir(frontend_root):
        logger.error(f"❌ Frontend Python path missing: {frontend_root}")
        return
    if frontend_root not in sys.path:
        sys.path.insert(0, frontend_root)
    try:
        import importlib
        import uvicorn
        import socket as _sock
        import threading as _threading
        
        if "main" in sys.modules:
            del sys.modules["main"]
        mod = importlib.import_module("main")
        app = mod.app
        _port = _service_port('frontend_web', 3000)
        
        # Loopback only, like every other service here. This page is the store's
        # own console — it is opened on the machine itself (or through a remote
        # desktop session onto it), never from another host — and binding every
        # interface put a login form in front of anyone on the shop's Wi-Fi.
        _host = _service_bind_host()
        logger.info(f"🚀 Starting Python frontend server on {_host}:{_port}...")
        uvicorn.run(app, host=_host, port=_port, log_level="warning")
    except ImportError as e:
        logger.error(f"❌ Missing dependency for frontend service: {e}")
    except Exception as e:
        logger.error(f"❌ Failed to run frontend service: {e}")



class ManagedServiceRunner:
    """Starts a DLC-delivered service that this build has no runner function for.

    Every `run_<name>_service` above is the same two lines — resolve a port,
    hand the folder to `_load_service_app` — so a service the admin pushes
    after this build shipped needs no new code, only its folder name. That is
    what makes "deliver, restart, it runs" true: `_discover_managed_services()`
    turns each unknown folder holding a main.py/.so into one of these.

    A class rather than a closure or a partial because multiprocessing's spawn
    mode pickles the target: `__reduce__` keeps that working after Cython
    compiles this module, and `__name__` keeps the supervisor's log lines
    ("Started run_x_service (PID …)") identical to the hand-written ones.
    Equality is by folder so SERVICE_PROCESSES can key on it across restarts.
    """

    def __init__(self, folder: str):
        self.folder = folder
        self.__name__ = f"run_{folder}"

    def __call__(self):
        # Port 0 is never used: a delivered folder always has a row by now
        # (register_delivered runs on delivery), and _service_port falls back
        # to the row's value. The default only matters if the row went missing.
        _load_service_app(self.folder, self.folder, self.folder, _service_port(self.folder, 0))

    def __reduce__(self):
        return (ManagedServiceRunner, (self.folder,))

    def __eq__(self, other):
        return isinstance(other, ManagedServiceRunner) and other.folder == self.folder

    def __hash__(self):
        return hash(("ManagedServiceRunner", self.folder))

    def __repr__(self):
        return f"<ManagedServiceRunner {self.folder}>"


def _managed_service_main(folder: str):
    """Path of a delivered folder's entry point, or None if it has none."""
    for name in ("main.so", "main.py"):
        candidate = os.path.join(MANAGED_PATH, folder, name)
        if os.path.exists(candidate):
            return candidate
    return None


def _discover_managed_services():
    """Register a runner for every delivered folder this build has no runner for.

    Called before the port table is built and before services start, so a
    folder that arrived while the client was down is picked up on the next
    boot with no code change.
    """
    found = {}
    if not os.path.isdir(MANAGED_PATH):
        return found
    try:
        folders = sorted(os.listdir(MANAGED_PATH))
    except OSError as e:
        logger.error(f"[CLIENT] Could not list managed services: {e}")
        return found
    for folder in folders:
        if folder.startswith('.') or folder in FOLDER_TO_SERVICE_FUNC:
            continue
        if not os.path.isdir(os.path.join(MANAGED_PATH, folder)):
            continue
        if not _managed_service_main(folder):
            logger.info(f"[CLIENT] '{folder}' delivered but has no entry point — not starting it.")
            continue
        runner = ManagedServiceRunner(folder)
        FOLDER_TO_SERVICE_FUNC[folder] = runner
        if runner not in ACTIVE_SERVICE:
            ACTIVE_SERVICE.append(runner)
        found[folder] = runner
        logger.info(f"[CLIENT] Discovered delivered service '{folder}' — will run it generically.")
    return found


FOLDER_TO_SERVICE_FUNC = {
    'data_service': run_data_service,
}

ACTIVE_SERVICE = [
    run_data_service,
    run_frontend_service,
]

SERVICE_PROCESSES = {}


# camera_service grows ~850 MB/hour and never gives it back. Once it ran at
# all hours it ran for days: at one store it reached 85 GB on a 16 GB Mac and
# macOS killed it ("killing largest compressed process"), leaving 8003 offline.
# So it is restarted (a few seconds without tracking) twice a day while the store
# is closed: at closing time, and RECYCLE_BEFORE_OPEN before opening. Neither costs
# the intrusion watch anything: right after closing it is still waiting for a quiet
# half hour, and before opening it has already stopped. It is also restarted at
# any hour if it goes past the memory backstop — a store with more cameras grows
# faster. Both limits read the environment, not .env (the app deletes that file):
# `open --env CAMERA_SERVICE_RESTART_HOURS=0.05 -a AICamera` restarts it on a
# fixed interval instead of at closing, which is how to test it.
_RECYCLE_AT_CLOSED_TIMES = {'camera_service'}
RECYCLE_BEFORE_OPEN = timedelta(minutes=30)
_RECYCLE_AFTER_HOURS = {
    'camera_service': float(os.getenv("CAMERA_SERVICE_RESTART_HOURS", "0")),
}
_MEMORY_LIMITS_MB = {
    'camera_service': int(os.getenv("CAMERA_SERVICE_MAX_MB", "12288")),
}
_MEMORY_CHECK_INTERVAL = 60  # seconds
_last_memory_check = 0.0
# pid -> when the check first saw it alive (good to a minute, which is plenty).
_RECYCLE_SEEN_AT = {}


def _memory_footprint_mb(pid):
    """What Activity Monitor calls Memory for `pid`, in MB, or None.

    phys_footprint, not RSS: RSS leaves out compressed pages, which is where a
    leaking process ends up — the one macOS killed showed 85 GB compressed.
    """
    import ctypes
    import struct
    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        buf = ctypes.create_string_buffer(512)
        if libproc.proc_pid_rusage(int(pid), 0, buf) != 0:  # 0 = RUSAGE_INFO_V0
            return None
        # rusage_info_v0: 16-byte uuid, then u64 fields; phys_footprint is the 8th.
        return struct.unpack_from("<Q", buf, 72)[0] / (1024 * 1024)
    except Exception:
        return None


def _last_closed_recycle_point(open_t, close_t, now_dt):
    """The latest closing time or RECYCLE_BEFORE_OPEN-before-opening not after now_dt,
    or None while the store is open (or never closes)."""
    if open_t <= close_t:
        is_open = open_t <= now_dt.time() <= close_t
    else:
        is_open = now_dt.time() >= open_t or now_dt.time() <= close_t
    if is_open:
        return None
    points = []
    for days in (1, 0, -1):  # tomorrow too: opening at 00:15 means a point at 23:45 today
        day = now_dt.date() + timedelta(days=days)
        points.append(datetime.combine(day, close_t))
        points.append(datetime.combine(day, open_t) - RECYCLE_BEFORE_OPEN)
    past = [p for p in points if p <= now_dt]
    return max(past) if past else None


def _recycle_bloated_services(now=datetime.now):
    """Stop camera_service when it is due (store closed) or has grown too big; the
    watchdog starts it again at its next free slot."""
    global _last_memory_check
    now_dt = now()
    ts = now_dt.timestamp()
    if ts - _last_memory_check < _MEMORY_CHECK_INTERVAL:
        return
    _last_memory_check = ts
    recycle_point = None
    if any(getattr(t, 'folder', None) in _RECYCLE_AT_CLOSED_TIMES for t in SERVICE_PROCESSES):
        open_t, close_t = get_store_hours()
        recycle_point = _last_closed_recycle_point(open_t, close_t, now_dt)
    # Forget pids that are gone (died, restarted elsewhere) so a reused pid starts at 0.
    live = {p.pid for p in SERVICE_PROCESSES.values()}
    for pid in [pid for pid in _RECYCLE_SEEN_AT if pid not in live]:
        del _RECYCLE_SEEN_AT[pid]
    for target, proc in list(SERVICE_PROCESSES.items()):
        folder = getattr(target, 'folder', None)
        max_hours = _RECYCLE_AFTER_HOURS.get(folder)
        limit = _MEMORY_LIMITS_MB.get(folder)
        at_closed = folder in _RECYCLE_AT_CLOSED_TIMES and not max_hours
        if not max_hours and not limit and not at_closed:
            continue
        if not proc.is_alive():
            continue  # the watchdog handles this
        started = _RECYCLE_SEEN_AT.setdefault(proc.pid, ts)
        hours = (ts - started) / 3600
        used = _memory_footprint_mb(proc.pid)
        if max_hours and hours >= max_hours:
            reason = f"has run {hours:.1f} hours (restarts every {max_hours:g})"
        elif at_closed and recycle_point and started < recycle_point.timestamp():
            reason = f"has run {hours:.1f} hours; daily restart while the store is closed"
        elif limit and used is not None and used >= limit:
            reason = f"is using {used / 1024:.1f} GB (limit {limit / 1024:.1f} GB)"
        else:
            continue
        logger.warning(f"[CLIENT] {target.__name__} {reason}. Restarting it.")
        _stop_for_restart(target)


# The watchdog (B15, B30, B33). Every service that is not alive is started again, at
# any hour, one at a time and spaced as the opening start-up always was: 5 s apart,
# 30 s after cus_emp (it loads its models). A process that died young waits first,
# per service: 5 s, then 10, 20 ... up to 5 min. One that ran more than 10 minutes (a
# daily exit, a crash after a good run) takes the next free slot and starts the wait
# over. Every death follows this one rule: there is no exit code for a planned exit.
START_GAP = 5
START_GAP_AFTER = {'run_cus_emp_service': 30}
BACKOFF_FIRST = 5
BACKOFF_MAX = 300
HEALTHY_RUN = 600
_STARTED_AT = {}    # target -> clock() its current process started at
_BACKOFF = {}       # target -> what its next early death waits
_DUE_AT = {}        # target (dead) -> clock() it may start from
# Set by stop_all_services(), under _START_LOCK, so the watchdog cannot start a
# service once the shutdown has begun. It used to: an update arriving on the
# socket thread stopped every service, the watchdog on the supervisor thread saw
# data_service die and started it again four seconds later, and that one was
# never stopped — it outlived the app holding port 8000, and the app the updater
# brought back could not start data_service at all.
_SHUTTING_DOWN = threading.Event()
_START_LOCK = threading.Lock()
_next_start_at = 0.0


def _supervised(activated=True):
    """What the watchdog keeps alive, in start order: data_service and the frontend first,
    nothing else before activation, never a delivered folder without an entry point."""
    return [t for t in ACTIVE_SERVICE
            if not isinstance(t, ManagedServiceRunner)
            or (activated and _managed_service_main(t.folder))]


def _dlc_services():
    """Every DLC service runner (not data_service, not the frontend)."""
    return [t for t in ACTIVE_SERVICE if isinstance(t, ManagedServiceRunner)]


def _watchdog(activated=True, clock=time.monotonic):
    """Note each dead service's wait, then, if the slot is free, start the one due longest
    (never started or stopped on purpose first, then start order), so a crash-looping
    service cannot take every slot from the others."""
    global _next_start_at
    if _SHUTTING_DOWN.is_set():  # the app is going down: a dead service is no news
        return
    now = clock()
    due = []
    for target in _supervised(activated):
        proc = SERVICE_PROCESSES.get(target)
        if proc is not None and proc.is_alive():
            continue
        if proc is not None and target not in _DUE_AT:  # seen dead for the first time
            ran = now - _STARTED_AT.get(target, now)
            if ran > HEALTHY_RUN:
                wait = 0
                _BACKOFF[target] = BACKOFF_FIRST
            else:
                wait = _BACKOFF.get(target, BACKOFF_FIRST)
                _BACKOFF[target] = min(wait * 2, BACKOFF_MAX)
            _DUE_AT[target] = now + wait
            logger.warning(f"[CLIENT] {target.__name__} is dead (exit code {proc.exitcode}, "
                           f"ran {ran:.0f}s). Starting it again in {wait}s at the earliest.")
        if _DUE_AT.get(target, now) <= now:
            due.append(target)
    if not due or now < _next_start_at:
        return
    target = min(due, key=lambda t: _DUE_AT.get(t, float('-inf')))  # min keeps the first of a tie
    with _START_LOCK:
        if _SHUTTING_DOWN.is_set():
            return
        p = multiprocessing.Process(target=target)
        p.start()
        SERVICE_PROCESSES[target] = p
    _STARTED_AT[target] = now
    _DUE_AT.pop(target, None)
    _next_start_at = now + START_GAP_AFTER.get(target.__name__, START_GAP)
    logger.info(f"[CLIENT] Started {target.__name__} (PID: {p.pid})")


def _stop_for_restart(target):
    """Stop a service the Main Client restarts on purpose; the watchdog starts it at the
    next free slot (it is not a death, so no wait, and any back-off starts over: a folder
    that crash-looped may just have been fixed)."""
    proc = SERVICE_PROCESSES.pop(target, None)
    _BACKOFF.pop(target, None)
    _DUE_AT.pop(target, None)
    if not proc or not proc.is_alive():
        return
    proc.terminate()
    proc.join(timeout=10)
    if proc.is_alive():
        proc.kill()
        proc.join(timeout=3)
    _RECYCLE_SEEN_AT.pop(proc.pid, None)
    logger.info(f"[CLIENT] Stopped {target.__name__} (PID: {proc.pid}); the watchdog starts it again.")


def stop_all_services():
    """Terminate every service process (for final shutdown)."""
    logger.info("--- Stopping ALL services ---")
    # From here on the watchdog starts nothing; a start already under way finishes
    # and registers before this returns, so the loops below see it.
    with _START_LOCK:
        _SHUTTING_DOWN.set()
    
    global api_process
    if 'api_process' in globals() and api_process and api_process.is_alive():
        logger.info(f"Terminating Activation API (PID: {api_process.pid})...")
        api_process.terminate()
        api_process.join(timeout=3)
        if api_process.is_alive():
            api_process.kill()
            
    for target, p in list(SERVICE_PROCESSES.items()):
        if p.is_alive():
            logger.info(f"Terminating {target.__name__} (PID: {p.pid})...")
            p.terminate()
    for target, p in list(SERVICE_PROCESSES.items()):
        p.join(timeout=10)
        if p.is_alive():
            logger.info(f"Force killing {target.__name__} (PID: {p.pid})...")
            p.kill()
            p.join()
    SERVICE_PROCESSES.clear()
    logger.info("All services fully stopped.")



# --------------------------------------------------------------------------------------
# Main application logic
# --------------------------------------------------------------------------------------
def is_activated():
    """Activated = the admin accepted this machine's code (recorded in the encrypted store)."""
    return bool(client_info.get("activated")) and bool(str(client_info.get("activateID") or "").strip())

def ensure_database():
    DB_USER = os.getenv("DB_USER")
    DB_PASSWORD = os.getenv("DB_PASSWORD")
    DB_HOST = os.getenv("DB_HOST", "localhost")
    DB_PORT = int(os.getenv("DB_PORT", 3306))
    DB_NAME = os.getenv("DB_NAME")
    
    # Root credentials for initial setup (database/user creation)
    DB_ROOT_USER = os.getenv("DB_ROOT_USER", "root")
    DB_ROOT_PASSWORD = os.getenv("DB_ROOT_PASSWORD", "")
   
    def get_root_conn(db=None):
        """Connect as root for privileged operations (create db/user)."""
        kwargs = {
            "user": DB_ROOT_USER,
            "password": DB_ROOT_PASSWORD,
            "database": db
        }
        if DB_HOST in ("localhost", "127.0.0.1"):
            kwargs["unix_socket"] = "/tmp/mysql.sock"
        else:
            kwargs["host"] = DB_HOST
            kwargs["port"] = DB_PORT
        return mariadb.connect(**kwargs)
    
    def get_conn(db=None):
        """Connect as application user for normal operations."""
        kwargs = {
            "user": DB_USER,
            "password": DB_PASSWORD,
            "database": db
        }
        if DB_HOST in ("localhost", "127.0.0.1"):
            kwargs["unix_socket"] = "/tmp/mysql.sock"
        else:
            kwargs["host"] = DB_HOST
            kwargs["port"] = DB_PORT
        return mariadb.connect(**kwargs)
    
    def create_tables():
        conn = get_conn(DB_NAME)
        cur = conn.cursor()

        tables = [

            # ---------------- MASTER ----------------
            """
            CREATE TABLE IF NOT EXISTS camera_master (
            id INT NOT NULL AUTO_INCREMENT,
            ip VARCHAR(45) NOT NULL,
            username VARCHAR(64),
            password TEXT,
            name VARCHAR(128),
            is_scanned TINYINT(1) NOT NULL DEFAULT 0,
            channel_count TINYINT UNSIGNED NOT NULL DEFAULT 16,
            PRIMARY KEY (id),
            UNIQUE KEY uq_ip_name (ip, name)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """,

            """
            CREATE TABLE IF NOT EXISTS store (
            id VARCHAR(100) NOT NULL,
            name VARCHAR(100) NOT NULL,
            region VARCHAR(100) NOT NULL,
            province VARCHAR(100) NOT NULL,
            open_time TIME NOT NULL,
            close_time TIME NOT NULL,
            staff_check_minutes SMALLINT UNSIGNED DEFAULT NULL,
            PRIMARY KEY (id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """,

            """
            CREATE TABLE IF NOT EXISTS account_config (
            username VARCHAR(64) NOT NULL,
            password VARCHAR(128) NOT NULL,
            is_activated TINYINT(1) NOT NULL DEFAULT 0,
            credentials_changed TINYINT(1) NOT NULL DEFAULT 0,
            PRIMARY KEY (username)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """,

            # Small named values the app keeps for itself (client_state_crud.py),
            # e.g. the version the admin assigned — no longer loose JSON files.
            """
            CREATE TABLE IF NOT EXISTS client_state (
            name VARCHAR(64) NOT NULL,
            value TEXT,
            PRIMARY KEY (name)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """,

            # ---------------- CAMERA ----------------
            """
            CREATE TABLE IF NOT EXISTS camera_channel (
            ip VARCHAR(45) NOT NULL,
            channel TINYINT UNSIGNED NOT NULL,
            status TINYINT(1) DEFAULT 1,
            camera_master_id INT DEFAULT NULL,
            PRIMARY KEY (ip, channel)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """,

            # Inside or outside the shop, per channel (camera_placement_crud.py): the
            # admin server's answer until a person chooses. No row = not asked yet.
            """
            CREATE TABLE IF NOT EXISTS camera_placement (
            ip VARCHAR(45) NOT NULL,
            channel TINYINT UNSIGNED NOT NULL,
            placement ENUM('inside','outside','uncertain') DEFAULT NULL,
            set_by ENUM('ai','user') DEFAULT NULL,
            ai_choice ENUM('inside','outside','uncertain') DEFAULT NULL,
            ai_confidence DECIMAL(4,3) DEFAULT NULL,
            ai_probabilities TEXT,
            ai_area VARCHAR(32) DEFAULT NULL,
            ai_model VARCHAR(64) DEFAULT NULL,
            ai_at DATETIME DEFAULT NULL,
            ai_error VARCHAR(128) DEFAULT NULL,
            user_at DATETIME DEFAULT NULL,
            PRIMARY KEY (ip, channel)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """,

            # The part of each channel's picture that shows the outside of the shop (glass front,
            # street), from the admin server's Outdoor Area (camera_outside_area_crud.py). Asked
            # once per channel, and again each time a person presses its inside/outside switch.
            """
            CREATE TABLE IF NOT EXISTS camera_outside_area (
            ip VARCHAR(45) NOT NULL,
            channel TINYINT UNSIGNED NOT NULL,
            state ENUM('wanted','done','refused') NOT NULL DEFAULT 'wanted',
            reason ENUM('first','switch') NOT NULL DEFAULT 'first',
            polygons MEDIUMTEXT,
            has_outdoor DECIMAL(4,3) DEFAULT NULL,
            box VARCHAR(160) DEFAULT NULL,
            box_confidence DECIMAL(4,3) DEFAULT NULL,
            picture_w SMALLINT UNSIGNED DEFAULT NULL,
            picture_h SMALLINT UNSIGNED DEFAULT NULL,
            model VARCHAR(64) DEFAULT NULL,
            error VARCHAR(128) DEFAULT NULL,
            wanted_at DATETIME(6) DEFAULT NULL,
            answered_at DATETIME DEFAULT NULL,
            PRIMARY KEY (ip, channel)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """,

            """
            CREATE TABLE IF NOT EXISTS floorplan (
            id INT NOT NULL AUTO_INCREMENT,
            name VARCHAR(255) NOT NULL,
            description VARCHAR(500) DEFAULT '',
            image_url TEXT DEFAULT '',
            image_updated_at DOUBLE NULL,
            previous_image_url TEXT NULL,
            floorplan_group INT NOT NULL DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """,

            """
            CREATE TABLE IF NOT EXISTS camera_calibration (
            id INT NOT NULL AUTO_INCREMENT,
            floorplan_id INT NOT NULL,
            camera_ip VARCHAR(45) NOT NULL,
            camera_channel TINYINT UNSIGNED NOT NULL,
            src_points JSON,
            dst_points JSON,
            matrix JSON,
            roi_zones JSON,
            hotzone_floor JSON,
            PRIMARY KEY (id),
            UNIQUE KEY unique_calibration (floorplan_id, camera_ip, camera_channel),
            CONSTRAINT fk_calibration_floorplan
                FOREIGN KEY (floorplan_id) REFERENCES floorplan(id),
            CONSTRAINT fk_calibration_camera
                FOREIGN KEY (camera_ip, camera_channel) REFERENCES camera_channel(ip, channel)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        ]

        for sql in tables:
            cur.execute(sql)

        # --- Migrations for existing deployments ---
        migrations = [
            "ALTER TABLE floorplan ADD COLUMN IF NOT EXISTS description VARCHAR(500) DEFAULT '' AFTER name",
            "ALTER TABLE floorplan ADD COLUMN IF NOT EXISTS floorplan_group INT NOT NULL DEFAULT 1 AFTER image_url",
            # When the picture was last replaced (unix seconds; NULL = before this
            # column). The admin's preprocess splits pipeline data at it so data is
            # drawn on the picture of its time. Recalibrating does not touch it.
            "ALTER TABLE floorplan ADD COLUMN IF NOT EXISTS image_updated_at DOUBLE NULL AFTER image_url",
            # The picture before the current one, kept (with its file) instead of
            # dropped, so data recorded on it still has its picture.
            "ALTER TABLE floorplan ADD COLUMN IF NOT EXISTS previous_image_url TEXT NULL AFTER image_updated_at",
            # Where each hot zone (Zone Area's Count Zone / Check Out) sits on this
            # floor plan, set by hand on Calibrate Hot Zone when the mapping from the
            # camera puts it in the wrong place (a zone on a table top, a corner with
            # no reference points). Only zones moved by hand are kept here.
            "ALTER TABLE camera_calibration ADD COLUMN IF NOT EXISTS hotzone_floor JSON NULL AFTER roi_zones",
        ]
        for sql in migrations:
            cur.execute(sql)

        # --- Migration: allow duplicate IPs with different names ---
        def _fk_exists(table, constraint_name):
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND CONSTRAINT_NAME = %s",
                (table, constraint_name),
            )
            return cur.fetchone()[0] > 0

        def _index_exists(table, index_name):
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.STATISTICS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND INDEX_NAME = %s",
                (table, index_name),
            )
            return cur.fetchone()[0] > 0

        if _fk_exists("camera_channel", "camera_channel_ibfk_1"):
            cur.execute("ALTER TABLE camera_channel DROP FOREIGN KEY camera_channel_ibfk_1")
        if _index_exists("camera_master", "ip") and not _index_exists("camera_master", "uq_ip_name"):
            cur.execute("ALTER TABLE camera_master DROP INDEX ip, ADD UNIQUE KEY uq_ip_name (ip, name)")

        def _column_exists(table, column):
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
                (table, column),
            )
            return cur.fetchone()[0] > 0

        if not _column_exists("camera_channel", "camera_master_id"):
            cur.execute("ALTER TABLE camera_channel ADD COLUMN camera_master_id INT DEFAULT NULL")
            # Backfill: for existing single-IP setups, link to the only camera_master row
            cur.execute("""
                UPDATE camera_channel cc
                JOIN camera_master cm ON cc.ip = cm.ip
                SET cc.camera_master_id = cm.id
            """)

        # How many channels Camera Selection lists for an NVR (8, 16 or 32), chosen on
        # Create network. A network made before the choice existed keeps every channel
        # it already had: the smallest size that holds its highest one.
        if not _column_exists("camera_master", "channel_count"):
            cur.execute("ALTER TABLE camera_master ADD COLUMN channel_count TINYINT UNSIGNED NOT NULL DEFAULT 16")
            cur.execute("""
                UPDATE camera_master cm
                JOIN (SELECT ip, MAX(channel) AS top FROM camera_channel GROUP BY ip) t ON t.ip = cm.ip
                SET cm.channel_count = CASE WHEN t.top > 16 THEN 32 ELSE 16 END
            """)

        # Security on Edit Store: how long before opening monitor_service's after-hours
        # watch waits for staff to switch the lights on before alerting. NULL: its default.
        if not _column_exists("store", "staff_check_minutes"):
            cur.execute("ALTER TABLE store ADD COLUMN staff_check_minutes SMALLINT UNSIGNED DEFAULT NULL")

        # Outdoor Area: the box Jev drew, kept even when it is not used (tools/show_outdoor_area.py
        # shows both), for a table made before it was kept.
        if not _column_exists("camera_outside_area", "box"):
            cur.execute("ALTER TABLE camera_outside_area ADD COLUMN box VARCHAR(160) DEFAULT NULL AFTER has_outdoor")

        conn.commit()
        conn.close()

    def create_user():
        try:
            conn = get_root_conn()
            cur = conn.cursor()
            try:
                cur.execute("FLUSH PRIVILEGES")
                # Least privilege: the app account only exists for loopback
                # hosts and only reaches its own database. The admin center
                # never connects to this DB, so no '%' host is created —
                # any leftover one from older installs is revoked below.
                cur.execute(f"CREATE USER IF NOT EXISTS '{DB_USER}'@'localhost' IDENTIFIED BY '{DB_PASSWORD}'")
                cur.execute(f"GRANT ALL PRIVILEGES ON {DB_NAME}.* TO '{DB_USER}'@'localhost'")
                cur.execute(f"CREATE USER IF NOT EXISTS '{DB_USER}'@'127.0.0.1' IDENTIFIED BY '{DB_PASSWORD}'")
                cur.execute(f"GRANT ALL PRIVILEGES ON {DB_NAME}.* TO '{DB_USER}'@'127.0.0.1'")
                # Read-only account for store staff: they can look at the data
                # without being handed the credentials the app runs on. SELECT
                # only, and localhost only, so it cannot change anything and
                # cannot be used from another machine.
                viewer = os.getenv("DB_VIEWER_USER")
                viewer_pw = os.getenv("DB_VIEWER_PASSWORD")
                if viewer and viewer_pw and viewer != DB_USER:
                    try:
                        for host in ("localhost", "127.0.0.1"):
                            cur.execute(
                                f"CREATE USER IF NOT EXISTS '{viewer}'@'{host}' IDENTIFIED BY '{viewer_pw}'")
                            cur.execute(
                                f"ALTER USER '{viewer}'@'{host}' IDENTIFIED BY '{viewer_pw}'")
                            # REVOKE first: an older build may have handed this
                            # name more than SELECT, and GRANT only ever adds.
                            try:
                                cur.execute(f"REVOKE ALL PRIVILEGES ON {DB_NAME}.* FROM '{viewer}'@'{host}'")
                            except Exception:
                                pass
                            cur.execute(f"GRANT SELECT ON {DB_NAME}.* TO '{viewer}'@'{host}'")
                        logger.info(f"[DB SETUP] Read-only account '{viewer}' ready (SELECT on {DB_NAME})")
                    except Exception as e:
                        logger.warning(f"[DB SETUP] Could not set up read-only account: {e}")

                try:
                    cur.execute(f"DROP USER '{DB_USER}'@'%'")
                    logger.info(f"[DB SETUP] Removed remote account '{DB_USER}'@'%' (remote access closed)")
                except Exception:
                    pass
                cur.execute("FLUSH PRIVILEGES")
            except Exception as e:
                logger.info(f"[DB SETUP] Skipping user creation (likely --skip-grant-tables): {e}")
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"[CLIENT] User creation failed: {e}")

    def create_database():
        try:
            logger.info("[DB SETUP] Creating database...")
            conn = get_root_conn()
            cursor = conn.cursor()
            cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{DB_NAME}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;")
            conn.commit()
            cursor.close()
            conn.close()
        except Exception as e:
            logger.error(f"[CLIENT] Database creation failed: {e}")

    # Order: create user first, then database, then grant privileges work, then tables
    create_user()
    create_database()
    create_tables()
    # Pick up folders delivered after this build shipped first, so they count
    # as runnable below and get a port row like any other service.
    _discover_managed_services()
    # The port registry lives in the same database. It starts out holding only
    # the services baked into the app itself; everything else earns its row as
    # its DLC folder is delivered (port_store.register_delivered).
    # FOLDER_TO_SERVICE_FUNC is what this build can actually launch. A service
    # whose files were delivered but has no runner here is left out of the
    # table entirely: the client does not know that service, so the port page
    # must not show it — not even its name.
    try:
        port_store.ensure_schema(set(FOLDER_TO_SERVICE_FUNC))
    except Exception as e:
        logger.error(f"[CLIENT] Port table setup failed: {e}")
    logger.info("✅ Database & tables created successfully")

def get_store_hours():
    """Fetches store opening and closing times from the database.
       Defaults to 08:00 - 20:00 if retrieval fails.
    """

    default_open = dt_time(8, 0)
    default_close = dt_time(20, 0)
    
    conn = None
    try:
        import pymysql
        conn = pymysql.connect(
            user=DB_USER,
            password=DB_PASSWORD,
            host=DB_HOST,
            port=DB_PORT,
            database=DB_NAME
        )
        cursor = conn.cursor()
        cursor.execute("SELECT open_time, close_time FROM store LIMIT 1")
        result = cursor.fetchone()
        
        if result:
            o_time, c_time = result
            # Handle timedelta if returned by driver
            if isinstance(o_time, timedelta):
                o_time = (datetime.min + o_time).time()
            if isinstance(c_time, timedelta):
                c_time = (datetime.min + c_time).time()
                
            return o_time, c_time
            
    except Exception as e:
        logger.error(f"[CLIENT] Failed to fetch store hours: {e}")
    finally:
        if conn:
            conn.close()
            
    return default_open, default_close

_browser_launched = False

def _remove_legacy_plain_files():
    """Delete readable files older versions left in App Support; the database holds them now."""
    for name in ("assigned_version.json",):
        path = os.path.join(APP_SUPPORT_DIR, name)
        try:
            if os.path.exists(path):
                os.remove(path)
                logger.info(f"[CLIENT] Removed {os.path.basename(path)} (kept in the database now)")
        except Exception as e:
            logger.warning(f"[CLIENT] Could not remove {os.path.basename(path)}: {type(e).__name__}")
    # VERSION / client_version.json in the bundle, once the version is compiled in.
    try:
        from data_service.configs.version_store import remove_plain_version_files
        for path in remove_plain_version_files(os.path.dirname(os.path.abspath(__file__))):
            logger.info(f"[CLIENT] Removed {os.path.basename(path)} (the version is compiled into _build_info)")
    except Exception as e:
        logger.warning(f"[CLIENT] Could not remove the plain version files: {e}")
    app_dir = os.path.dirname(os.path.abspath(__file__))
    for leftover in (os.path.join(app_dir, "person.png"),):
        try:
            os.remove(leftover)
        except OSError:
            pass


# --------------------------------------------------------------------------------------
# Process manager: main()'s loop, one pass at a time
# --------------------------------------------------------------------------------------
# Out of main() so a test can drive them (seam 4): `now` is the wall clock (Store
# Hours, camera_service's daily restart), `clock` the watchdog's monotonic one. Store
# Hours stop and start nothing (B26); the watchdog keeps every service up at any hour.
def _pre_activation_pass(base_dir, clock=time.monotonic):
    """Before activation: act on .restart_required, keep data_service and the frontend up."""
    load_config()
    
    # Check for restart-required flag (e.g. from port change or system page)
    _restart_req_flag = os.path.join(APP_SUPPORT_DIR, ".restart_required")
    if os.path.exists(_restart_req_flag):
        try:
            with open(_restart_req_flag, "r") as f:
                flag_content = f.read().strip()
            os.remove(_restart_req_flag)
            if flag_content == "PORT_CHANGE":
                logger.info("[CLIENT] Port Change flag received. Triggering restart allowing browser to open...")
                subprocess.Popen([sys.executable, 'updater.py', '--port-change-restart'], cwd=base_dir, start_new_session=True)
            else:
                logger.info("[CLIENT] Restart flag received. Triggering full app restart via updater.py...")
                subprocess.Popen([sys.executable, 'updater.py', '--restart-only'], cwd=base_dir, start_new_session=True)
            stop_all_services()
            import os as _os
            _os._exit(0)
        except Exception as e:
            logger.error(f"[CLIENT] Restart req flag error in pre-activation: {e}")

    # Only the UI before activation: delivered services have nothing to work for
    # until the store is activated.
    _watchdog(activated=False, clock=clock)


def _closed_hours_pass(open_t, close_t, now=datetime.now, clock=time.monotonic):
    """Store closed: run a DLC restart round if one is due, keep every service up, act on
    the flags, then wait 5 s (or until a restart is asked for)."""
    logger.info(f"[CLIENT] Store CLOSED (Time: {now().strftime('%H:%M')}, Window: {open_t}-{close_t}). Waiting...")
    _restart_round(clock)
    _watchdog(clock=clock)
    _recycle_bloated_services(now)
    # Check for restart-required flag (e.g. from port change)
    _restart_req_flag = os.path.join(APP_SUPPORT_DIR, ".restart_required")
    if os.path.exists(_restart_req_flag):
        try:
            os.remove(_restart_req_flag)
            logger.info("[CLIENT] Restart flag received. Triggering full app restart via updater.py...")
            subprocess.Popen([sys.executable, 'updater.py', '--restart-only'], cwd=os.path.dirname(__file__), start_new_session=True)
            stop_all_services()
            import os as _os
            _os._exit(0)
        except Exception as e:
            logger.error(f"[CLIENT] Restart req flag error: {e}")

    # Check for update request flag
    update_flag = os.path.join(os.path.dirname(__file__), ".update_requested")
    if os.path.exists(update_flag):
        try:
            os.remove(update_flag)
            logger.info("[CLIENT] UI requested an update while closed. Emitting request_update to Admin Server...")
            current_version = _installed_version()
            _sio.is_updating = True
            get_sio().emit("request_update", {"mac_address": get_mac_address(), "current_version": current_version})
        except Exception as e:
            logger.error(f"[CLIENT] Failed to process update flag: {e}")

    RESTART_EVENT.wait(timeout=5)


def _open_hours_tick(base_dir, now=datetime.now, clock=time.monotonic):
    """One round of the open-hours loop: 'restart' (a DLC restart round ran), 'closing', or None."""
    RESTART_EVENT.wait(timeout=5)
    if _restart_round(clock):
        return 'restart'
    # python-socketio only reconnects by itself after a network
    # drop. When the server ends the session (e.g. it revoked
    # this machine's Device Key) nothing used to dial again until
    # the next open/close cycle — which, with the hours check
    # disabled, meant never. A manual disconnect (DLC update)
    # is left alone: the restart that follows reconnects.
    if not _MANUAL_DISCONNECT.is_set() and not (_sio and _sio.connected) and not _socket_refused():
        start_socket_client()
    # Check for restart-all flag from port_manager
    _restart_flag = os.path.join(APP_SUPPORT_DIR, ".restart_required")
    if os.path.exists(_restart_flag):
        try:
            with open(_restart_flag, "r") as f:
                flag_content = f.read().strip()
            os.remove(_restart_flag)
            
            if flag_content == "PORT_CHANGE":
                logger.info("[CLIENT] Port Change flag received. Triggering restart allowing browser to open...")
                subprocess.Popen([sys.executable, 'updater.py', '--port-change-restart'], cwd=base_dir, start_new_session=True)
            else:
                logger.info("[CLIENT] Restart flag received. Triggering full app restart via updater.py...")
                subprocess.Popen([sys.executable, 'updater.py', '--restart-only'], cwd=base_dir, start_new_session=True)
            
            # Cleanly terminate current processes
            stop_all_services()
            
            # Exit client_logic so bootstrap can also exit
            import os as _os
            _os._exit(0)
        except Exception as e:
            logger.error(f"[CLIENT] Restart flag error: {e}")
    _watchdog(clock=clock)
    _recycle_bloated_services(now)
    # Periodic time check — re-read store hours so changes
    # made via the frontend take effect immediately
    open_t, close_t = get_store_hours()
    now_t = now().time()
    
    # Check for manual update trigger from UI
    update_flag = os.path.join(os.path.dirname(__file__), ".update_requested")
    if os.path.exists(update_flag):
        try:
            os.remove(update_flag)
            logger.info("[CLIENT] UI requested an update. Emitting request_update to Admin Server...")
            current_version = _installed_version()
            # Send socket request
            _sio.is_updating = True
            get_sio().emit("request_update", {"mac_address": get_mac_address(), "current_version": current_version})
        except Exception as e:
            logger.error(f"[CLIENT] Failed to process update flag: {e}")
    if open_t <= close_t:
        should_be_open = open_t <= now_t <= close_t
    else:
        should_be_open = now_t >= open_t or now_t <= close_t
    
    if not should_be_open:
         logger.info(f"[CLIENT] Closing time reached ({close_t}). Services keep running.")
         return 'closing'
    return None


def _restart_round(clock=time.monotonic):
    """At any hour: finish a DLC transfer that stalled, then, if a restart was asked for (a
    DLC, a Device Key change), stop the services on the list (the watchdog starts them again
    one at a time), clear what it took and both flags, and reconnect the socket the DLC
    dropped. Socket threads only record what they want (see _COMPLETED_FOLDERS); this,
    on the supervisor thread, changes the process table. True when a round ran."""
    global _RESTART_EVERY_DLC
    _finish_stalled_dlc_transfer(clock)
    if not RESTART_EVENT.is_set():
        return False
    RESTART_EVENT.clear()
    with _UPDATED_FOLDERS_LOCK:  # take and clear in one go; folders still arriving stay pending
        funcs = set(_UPDATED_SERVICE_FUNCS)
        deleted, completed = set(_DELETED_FOLDERS), set(_COMPLETED_FOLDERS)
        every_dlc = _RESTART_EVERY_DLC
        _UPDATED_SERVICE_FUNCS.clear()
        _DELETED_FOLDERS.clear()
        _COMPLETED_FOLDERS.clear()
        _RESTART_EVERY_DLC = False
    stop, shared = _apply_dlc_changes(deleted, completed)
    funcs |= stop
    if every_dlc or shared:
        funcs.update(_dlc_services())
    logger.info(f"[CLIENT] Restart round: {[f.__name__ for f in funcs]}")
    for fn in funcs:
        _stop_for_restart(fn)
    _MANUAL_DISCONNECT.clear()
    start_socket_client()
    return True


def _supervisor_pass(base_dir, now=datetime.now, clock=time.monotonic):
    """One pass of main()'s loop. An open-hours pass lasts until closing or a DLC restart."""
    if not is_activated():
        _pre_activation_pass(base_dir, clock)
    else:
        # ------------------------------------------------------------------
        # Auto Start/Stop Logic based on Store Hours
        # ------------------------------------------------------------------
        # Connect socket (even when closed — we want admin connectivity)
        if not _sio or not _sio.connected:
            threading.Thread(target=start_socket_client, daemon=True).start()
        
        # Check store hours
        open_t, close_t = get_store_hours()
        now_t = now().time()
        
        # Check if current time is within open-close window
        if open_t <= close_t:
            is_open = open_t <= now_t <= close_t
        else: # Crosses midnight
            is_open = now_t >= open_t or now_t <= close_t
        
        if not is_open:
            _closed_hours_pass(open_t, close_t, now, clock)
            return # Loop back to check activation and time again

        # Store IS OPEN: nothing to start, the watchdog in every tick keeps services up,
        # and a tick runs a DLC restart round as a closed pass does.
        logger.info(f"[CLIENT] Store OPEN ({open_t}-{close_t}).")
        _MANUAL_DISCONNECT.clear()
        while True:
            outcome = _open_hours_tick(base_dir, now, clock)
            if outcome == 'closing':
                return  # nothing is stopped at closing
            if outcome == 'restart':
                break

    logger.info("==================================================\n            RESTARTING IN 5 SECONDS             \n==================================================")
    time.sleep(5)


OPEN_FILES_WANTED = 8192


def _raise_open_files_limit():
    """Lift the open-files cap before any service starts; children inherit it.

    A Mac app launched by launchd gets a soft limit of 256. camera_service
    alone holds ~250 (shared memory per camera, Metal, RTSP, websockets), so
    within minutes accept() fails with EMFILE and every request to its port is
    reset — Detection Setup shows no picture while Live Display looks fine.
    """
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        want = OPEN_FILES_WANTED if hard == resource.RLIM_INFINITY else min(OPEN_FILES_WANTED, hard)
        if soft == resource.RLIM_INFINITY or soft >= want:
            return
        resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
        logger.info(f"[CLIENT] open files limit {soft} -> {want}")
    except (ImportError, ValueError, OSError) as e:
        logger.warning(f"[CLIENT] could not raise open files limit: {e}")


def main():
    logger.info("[CLIENT] main() started")
    _raise_open_files_limit()
    # The menu bar's restart notice belongs to the run that just ended.
    try:
        import runtime_paths
        os.remove(runtime_paths.RESTART_NOTICE_FILE)
    except OSError:
        pass
    try:
        _create_backend_env()
    except Exception as e:
        logger.error(f"[CLIENT] _create_backend_env failed: {e}", exc_info=True)
        return
    try:
        ensure_database()
    except Exception as e:
        logger.error(f"[CLIENT] ensure_database failed: {e}", exc_info=True)
        return
    _remove_legacy_plain_files()

    # Launch menubar app
    if hasattr(sys, '_MEIPASS'):
        base_dir = os.path.join(sys._MEIPASS, 'app')
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))
    menubar_py = os.path.join(base_dir, "menubar_app.py")
    menubar_so = os.path.join(base_dir, "menubar_app.so")
    
    if os.path.exists(menubar_py) or os.path.exists(menubar_so):
        import subprocess
        logger.info("Starting the menu bar icon...")
        try:
            subprocess.run(["pkill", "-f", "menubar_app"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        if hasattr(sys, '_MEIPASS'):
            # In PyInstaller macOS app bundle, sys._MEIPASS is Contents/MacOS
            # python_env is located in Contents/Resources/python_env
            python_bin = os.path.join(os.path.dirname(sys._MEIPASS), "Resources", "python_env", "bin", "python3.12")
        else:
            python_bin = os.path.join(os.path.dirname(base_dir), "python_env", "bin", "python3.12")
            
        if not os.path.exists(python_bin):
            python_bin = sys.executable
            
        if python_bin == sys.executable:
            # sys.executable is PyInstaller bundle which ignores -c, so we must run it directly if fallback happens
            logger.warning("Bundled Python not found, menubar might not work correctly if using bundle executable")
        
        # Hand the resolved frontend port down so the menubar knows where to
        # point the browser without opening its own database connection.
        menubar_env = os.environ.copy()
        menubar_env["AICAMERA_FRONTEND_PORT"] = str(_service_port('frontend_web', 3000))
        threading.Thread(target=_write_app_status_forever, name="app-status", daemon=True).start()
        subprocess.Popen([python_bin, "-c", "import menubar_app; menubar_app.setup_menubar()"],
                         cwd=base_dir, env=menubar_env)

    # The "Starting System" splash that stays up until the UI answers. bootstrap.py
    # shows it for the .app (and marks itself with sys._machine_cipher); a branch
    # installed by git clone starts here instead, so show it from here.
    splash = os.path.join(base_dir, ".loading_gui.py")
    if not hasattr(sys, '_machine_cipher') and os.path.exists(splash):
        import subprocess
        try:
            subprocess.Popen([sys.executable, splash], cwd=base_dir, start_new_session=True,
                             env=dict(os.environ, AICAMERA_FRONTEND_PORT=str(_service_port('frontend_web', 3000))),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            logger.warning(f"[CLIENT] Could not show the startup window: {e}")

    global api_process
    api_process = multiprocessing.Process(target=run_activation_server, daemon=True)
    api_process.start()

    os.makedirs(MANAGED_PATH, exist_ok=True)
    _encrypt_managed_services_at_rest()
    load_config()
    client_id = client_info.get("client_id", "Unknown-ID")
    logger.info(f"[CLIENT] Starting up ({'activated' if client_info.get('client_id') else 'no activation code yet'})")
    # Activated before Device Keys existed (or the key was lost): fetch one
    # now, so no one has to visit the store (contract §1.2).
    if is_activated():
        _refresh_device_key_async("startup")

    try:
        while True:
            _supervisor_pass(base_dir)

    except KeyboardInterrupt:
        logger.info("[CLIENT] Shutdown signal received.")
    except Exception as e:
        logger.error(f"[CLIENT] A fatal error occurred in main: {e}")
    finally:
        stop_all_services()
        logger.info("[CLIENT] Application has shut down.")
        time.sleep(5)

if __name__ == '__main__':
    main()