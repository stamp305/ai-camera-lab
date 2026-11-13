import base64
import ctypes
import hashlib
import os
import sys
import platform

if platform.system() == "Darwin" and os.environ.get('OBJC_DISABLE_INITIALIZE_FORK_SAFETY') != 'YES':
    os.environ['OBJC_DISABLE_INITIALIZE_FORK_SAFETY'] = 'YES'
    os.execv(sys.executable, [sys.executable] + sys.argv)

# The bundled Python's OpenSSL was built with a default CA file path baked in
# from the build machine (e.g. /opt/miniconda3/ssl/cert.pem) that does not
# exist on a client Mac, so every TLS connection this process or its spawned
# children make — including the Socket.IO client to the update server — fails
# cert verification with "unable to get local issuer certificate", even though
# curl/macOS succeed (they use the system keychain, not this). Point OpenSSL at
# the certifi bundle shipped alongside this interpreter instead.
try:
    import certifi
    os.environ.setdefault('SSL_CERT_FILE', certifi.where())
except Exception:
    pass

import importlib.abc
import importlib.machinery
import json
import logging
import logging.config
from logging.handlers import TimedRotatingFileHandler
import email
import email.message
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import time
import threading
import webbrowser
import tkinter as tk
from tkinter import ttk
from types import ModuleType

from cryptography.fernet import Fernet, InvalidToken

from mac_utils import get_canonical_mac_address

# --------------------------------------------------------------------------------------
# Paths Configuration (App Support Directory)
# --------------------------------------------------------------------------------------
APP_SUPPORT_DIR = os.path.expanduser('~/Library/Application Support/AICamera')
DB_DIR = os.path.join(APP_SUPPORT_DIR, 'db')
# MariaDB's built-in superuser. Only used as a fallback now: the account the
# app actually authenticates as comes from DB_ROOT_USER.
LEGACY_DB_ADMIN_USER = 'root'
LOGS_DIR = os.path.join(APP_SUPPORT_DIR, 'logs')
MANAGED_DIR = os.path.join(APP_SUPPORT_DIR, 'managed_services')
ENV_FILE = os.path.join(APP_SUPPORT_DIR, '.env')
GATEKEEPER_FLAG = os.path.join(APP_SUPPORT_DIR, '.gatekeeper_fixed')
# The pid file and what the startup window (loading_gui.py) shows live in this
# user's private temporary folder, not in App Support (runtime_paths.py).
import runtime_paths
# Before any logger exists: spawned services re-run this file's top level, so
# every process of the app gets them — camera URLs and folders out of every
# line (log_redact.py). Log sealing (log_seal.py) is paused: install() only moves
# old sealed files aside and logs are written in plain text.
try:
    import log_redact
    log_redact.install()
except ImportError:
    pass
import log_seal
log_seal.install()
PID_FILE = runtime_paths.PID_FILE
STARTUP_STATUS_FILE = runtime_paths.STARTUP_STATUS_FILE

os.makedirs(APP_SUPPORT_DIR, exist_ok=True)
os.makedirs(DB_DIR, exist_ok=True)
runtime_paths.ensure_run_dir()
for _legacy in (runtime_paths.LEGACY_STATUS_FILE,):
    try:
        os.remove(_legacy)
    except OSError:
        pass
os.makedirs(LOGS_DIR, exist_ok=True)
os.makedirs(MANAGED_DIR, exist_ok=True)

# --------------------------------------------------------------------------------------
# Logging Setup
# --------------------------------------------------------------------------------------
def setup_logging():
    log_file = os.path.join(LOGS_DIR, 'bootstrap.log')
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    
    file_handler = TimedRotatingFileHandler(log_file, when='midnight', interval=1, backupCount=7, encoding='utf-8')
    file_handler.setFormatter(formatter)
    
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    
    logger = logging.getLogger('AICamera_Bootstrap')
    logger.setLevel(logging.INFO)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger

logger = setup_logging()


def _report_status(text, state='running', **extra):
    """Tell the startup window what is happening. Never fails the launch."""
    try:
        tmp = STARTUP_STATUS_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(dict(extra, state=state, text=text), f, ensure_ascii=False)
        os.replace(tmp, STARTUP_STATUS_FILE)
    except Exception:
        pass

# --------------------------------------------------------------------------------------
# 2-Layer Key Derivation (SHARED_KEY & MACHINE_KEY)
# --------------------------------------------------------------------------------------
_E1 = [0x8a ^ 0x42, 0x3f ^ 0x42, 0x5a ^ 0x42, 0x37 ^ 0x42, 0x39 ^ 0x42, 0x5f ^ 0x42, 0x35 ^ 0x42, 0x64 ^ 0x42]
_E2 = [0x47 ^ 0x7B, 0xb2 ^ 0x7B, 0x8f ^ 0x7B, 0xc1 ^ 0x7B, 0x34 ^ 0x7B, 0x56 ^ 0x7B, 0x78 ^ 0x7B, 0x90 ^ 0x7B]
# Legacy DLC key: real value comes from .env (STATIC_LEGACY_KEY); the
# placeholder below only keeps source-only installs from crashing before
# the operator fills in .env.
STATIC_LEGACY_KEY = os.getenv("STATIC_LEGACY_KEY", "").encode() or b''

def _derive_shared_key() -> bytes:
    s1 = bytes(b ^ 0x42 for b in _E1)
    s2 = bytes(b ^ 0x7B for b in _E2)
    raw = s1 + s2
    derived = hashlib.pbkdf2_hmac('sha256', raw, s2, 100_000)
    return base64.urlsafe_b64encode(derived[:32])

def _get_serial_number() -> str:
    try:
        cmd = "system_profiler SPHardwareDataType | grep 'Serial Number' | awk -F': ' '{print $2}'"
        res = subprocess.check_output(cmd, shell=True, text=True).strip()
        if res:
            
            return res
    except Exception:
        pass
    try:
        return subprocess.check_output(['hostname']).decode('utf-8').strip()
    except Exception:
        return 'Unknown'

def _get_mac_address() -> str:
    return get_canonical_mac_address()

def _derive_machine_key() -> bytes:
    serial = _get_serial_number()
    mac = _get_mac_address()
    raw = serial.encode('utf-8') + mac.encode('utf-8') + b"_aicamera_machine_salt_v1"
    salt = hashlib.sha256(b"salt_v2_" + serial.encode('utf-8')).digest()
    derived = hashlib.pbkdf2_hmac('sha256', raw, salt, 100_000)
    return base64.urlsafe_b64encode(derived[:32])

shared_cipher = Fernet(_derive_shared_key())
machine_cipher = Fernet(_derive_machine_key())
legacy_cipher = Fernet(STATIC_LEGACY_KEY) if STATIC_LEGACY_KEY else None

# device_info.json (plaintext serial + MAC) used to be written here for other
# processes to read. Those values now live in the encrypted identity store
# (device_identity.py), which also migrates and removes the old file.

def _migrate_old_encryption(file_path: str):
    if not os.path.isfile(file_path):
        return
    with open(file_path, 'rb') as f:
        data = f.read()
    if not data.startswith(b'gAAAA'):
        return
    
    try:
        machine_cipher.decrypt(data)
        return  # Already MACHINE_KEY
    except InvalidToken:
        pass
    
    plaintext = None
    # Not legacy_cipher: that is the admin's DLC key, and managed services are now
    # KEPT encrypted with it on disk (dlc_import_hook.py decrypts them at import).
    # Re-encrypting them with the machine key would make them unimportable.
    for cipher in [shared_cipher]:
        if cipher is None:
            continue
        try:
            plaintext = cipher.decrypt(data)
            break
        except InvalidToken:
            continue
            
    if plaintext is not None:
        new_encrypted = machine_cipher.encrypt(plaintext)
        with open(file_path, 'wb') as f:
            f.write(new_encrypted)
        logger.info("[MIGRATION] Re-encrypted a file with MACHINE_KEY")
    # Nothing logged when no key fits: that is every DLC file (kept under the
    # admin's key on purpose, see above), and each warning listed a file name.

# --------------------------------------------------------------------------------------
# System Integration & Anti-debug
# --------------------------------------------------------------------------------------
_caffeinate_proc = None

def _anti_debug():
    if platform.system() == 'Darwin':
        try:
            ctypes.CDLL(None).ptrace(31, 0, 0, 0)
        except Exception as e:
            logger.debug(f"[ANTI-DEBUG] ptrace unavailable: {e}")

def _start_caffeinate():
    global _caffeinate_proc
    try:
        _caffeinate_proc = subprocess.Popen(['caffeinate', '-i', '-m', '-s'])
        logger.info(f"[BOOTSTRAP] Caffeinate started (PID {_caffeinate_proc.pid})")
    except Exception as e:
        logger.warning(f"[BOOTSTRAP] Failed to start caffeinate: {e}")

def _stop_caffeinate():
    global _caffeinate_proc
    if _caffeinate_proc:
        try:
            _caffeinate_proc.terminate()
            _caffeinate_proc = None
        except Exception:
            pass

# How long the pid is also written at the old App Support path. The updater of
# the version being upgraded FROM is the one that relaunches the app, and it
# only looks there: without it, it decides the app never came back and opens it
# twice more. It waits 20 s per try, so two minutes covers it; then the file goes.
_LEGACY_PID_SECONDS = 120


def _remove_legacy_pid():
    try:
        os.remove(runtime_paths.LEGACY_PID_FILE)
    except OSError:
        pass


def _check_duplicate():
    current_pid = os.getpid()
    # The old path too: an older version may be the one still running.
    for path in (PID_FILE, runtime_paths.LEGACY_PID_FILE):
        if not os.path.exists(path):
            continue
        try:
            with open(path, 'r') as f:
                old_pid = int(f.read().strip())
            if old_pid == current_pid:
                continue
            os.kill(old_pid, 0)
            logger.warning(f"[BOOTSTRAP] AICamera already running (PID {old_pid}). Exiting.")
            _report_status("Already running. Opening it...",
                           state='already_running', port=_frontend_port())
            sys.exit(0)
        except (OSError, ValueError):
            pass
    with open(PID_FILE, 'w') as f:
        f.write(str(current_pid))
    try:
        with open(runtime_paths.LEGACY_PID_FILE, 'w') as f:
            f.write(str(current_pid))
        t = threading.Timer(_LEGACY_PID_SECONDS, _remove_legacy_pid)
        t.daemon = True
        t.start()
    except OSError:
        pass

def _fix_gatekeeper():
    if not os.path.exists(GATEKEEPER_FLAG):
        try:
            app_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
            if app_path.endswith('.app'):
                logger.info("[GATEKEEPER] Clearing the quarantine attribute on the app...")
                _report_status("Checking app files (first launch only, may take a few minutes)...", progress=0.15)
                subprocess.run(['xattr', '-d', 'com.apple.quarantine', app_path], stderr=subprocess.DEVNULL)
                subprocess.run(['xattr', '-cr', app_path], stderr=subprocess.DEVNULL)
            with open(GATEKEEPER_FLAG, 'w') as f:
                f.write("fixed\n")
        except Exception as e:
            logger.warning(f"[GATEKEEPER] Auto-fix warning: {e}")

def _register_login_item():
    # bootstrap.py lives at AICamera.app/Contents/Resources/app/bootstrap.py —
    # three levels up (app -> Resources -> Contents -> AICamera.app) reaches
    # the bundle itself. Two levels (the old code) landed on Contents/, which
    # never ends in '.app', so this silently no-op'd in every real build.
    app_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
    if app_path.endswith('.app'):
        _report_status("Setting the app to open at login... "
                       "If macOS asks for permission, click OK.", progress=0.2)
        try:
            script = f'''
            tell application "System Events"
                if not (exists login item "AICamera") then
                    make new login item at end with properties {{path:"{app_path}", hidden:false}}
                end if
            end tell
            '''
            # Bounded: an unanswered permission prompt used to hold the whole
            # launch here with nothing on screen to say why.
            subprocess.run(['osascript', '-e', script], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=60)
            logger.info("[LOGIN_ITEM] Verified login item registration.")
        except subprocess.TimeoutExpired:
            logger.warning("[LOGIN_ITEM] Not registered: the macOS permission prompt was not answered in time")
        except Exception as e:
            # Not str(e): a failed command repeats the whole script, app path included.
            logger.warning(f"[LOGIN_ITEM] Not registered: {type(e).__name__}")

def _take_over_git_clone():
    """The app installed over a branch that ran from a git clone: stop the clone, bring
    in its activation, services and uploaded pictures (legacy_clone.py). App only: a git
    clone started through this file is the clone itself."""
    bundle = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
    if not bundle.endswith('.app'):
        return
    try:
        import legacy_clone
        code_dir = os.path.dirname(os.path.abspath(__file__))
        done = legacy_clone.take_over(code_dir, MANAGED_DIR, APP_SUPPORT_DIR)
    except Exception as e:
        logger.warning(f"[CLONE] Git-clone check failed: {type(e).__name__}")
        return
    if done.get("clone"):
        logger.info("[CLONE] Git-clone install found: stopped=%s, activation carried=%s, services carried=%d, "
                    "pictures carried=%d", bool(done.get("stopped")), bool(done.get("identity")),
                    len(done.get("services") or []), int(done.get("pictures") or 0))

# --------------------------------------------------------------------------------------
# Portable MariaDB Lifecycle
# --------------------------------------------------------------------------------------
_mariadb_proc = None

def get_app_dir() -> str:
    """Returns the base app resources/code directory (handles PyInstaller bundle vs standalone script)."""
    if getattr(sys, 'frozen', False):
        exe_dir = os.path.dirname(sys.executable)  # Contents/MacOS
        resources_dir = os.path.abspath(os.path.join(exe_dir, '..', 'Resources'))
        if os.path.exists(resources_dir):
            return resources_dir
    return os.path.dirname(os.path.abspath(__file__))

def _setup_python_env():
    """Injects bundled Python environment stdlib & site-packages into sys.path."""
    app_d = get_app_dir()
    py_lib = os.path.abspath(os.path.join(app_d, 'python_env', 'lib', 'python3.12'))
    site_pkgs = os.path.join(py_lib, 'site-packages')
    dynload = os.path.join(py_lib, 'lib-dynload')
    
    for p in [site_pkgs, py_lib, dynload]:
        if os.path.exists(p) and p not in sys.path:
            sys.path.insert(0, p)
            logger.info(f"[BOOTSTRAP] Injected Python lib path: {p}")

_setup_python_env()

def _get_binary_path(name: str) -> str:
    app_d = get_app_dir()
    res_bin = os.path.join(app_d, 'bin', name)
    if os.path.exists(res_bin) and os.access(res_bin, os.X_OK):
        return res_bin
    rel_bin = os.path.abspath(os.path.join(os.path.dirname(__file__), 'bin', name))
    if os.path.exists(rel_bin) and os.access(rel_bin, os.X_OK):
        return rel_bin
    return shutil.which(name) or name

def _db_root_password():
    """The root password client_logic will authenticate with.

    bootstrap runs before client_logic, so the compiled secure env has not
    been loaded and a bare os.getenv() falls back to the placeholder — which
    initialises MariaDB with a password the app then never uses, and every
    connection afterwards fails with "Access denied ... using password: YES".
    A fresh machine (no brew MariaDB, bundled mariadbd, empty datadir) hits
    this on its very first boot.

    load_secure_env() assigns straight into os.environ, so the environment is
    snapshotted and put back: client_logic captures its own overrides from
    os.environ at import time, and it is started as a child of this process.
    """
    pw = os.getenv('DB_ROOT_PASSWORD')
    if pw:
        return pw
    saved = dict(os.environ)
    try:
        from data_service.configs.secure_env import load_secure_env
        load_secure_env()
        pw = os.environ.get('DB_ROOT_PASSWORD')
    except Exception as e:
        logger.warning(f"[DB] Could not read the secure env for the root password: {e}")
        pw = None
    finally:
        os.environ.clear()
        os.environ.update(saved)
    if not pw:
        logger.warning("[DB] No root password in the secure env")
    return pw or ''


def _db_root_user():
    """The privileged DB user client_logic authenticates as (DB_ROOT_USER).

    Read the same way as the password so the two can never disagree. Falls
    back to the MariaDB built-in account, which is what every install created
    before this became configurable.
    """
    user = os.getenv('DB_ROOT_USER')
    if user:
        return user
    saved = dict(os.environ)
    try:
        from data_service.configs.secure_env import load_secure_env
        load_secure_env()
        user = os.environ.get('DB_ROOT_USER')
    except Exception as e:
        logger.warning(f"[DB] Could not read the secure env for the admin user: {e}")
        user = None
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return user or LEGACY_DB_ADMIN_USER


def _sq(value):
    """Quote a value for inline SQL (single quotes doubled)."""
    return str(value).replace("'", "''")


def _ensure_admin_user():
    """Make sure the configured admin user exists and its password matches.

    Three situations have to end the same way:
      - already correct                     -> nothing to do
      - account exists with no password     -> set it
      - account does not exist yet          -> create it, using the legacy
        'root' account that every older install still has

    The last one is the migration: a store that was set up before DB_ROOT_USER
    became configurable has only 'root', so the new account is created from it
    on the next launch, with no manual step per machine. 'root' is left in
    place on purpose — dropping it here would strip the only fallback if the
    new account ever fails to authenticate.
    """
    try:
        import mariadb
    except Exception:
        return
    user = _db_root_user()
    pw = _db_root_password()

    try:
        c = mariadb.connect(user=user, password=pw, host='127.0.0.1', port=3306, connect_timeout=3)
        c.close()
        return
    except Exception:
        pass

    for sock in ('/tmp/mysql.sock', os.path.join(APP_SUPPORT_DIR, 'mysql.sock')):
        if not os.path.exists(sock):
            continue
        # Ordered by how little they assume: the account itself with no
        # password, then the legacy account with either password. The password
        # older builds shipped in plain code is no longer tried: it would have
        # to sit in this file, readable with `strings`.
        attempts = [(user, '')]
        if user != LEGACY_DB_ADMIN_USER:
            attempts += [(LEGACY_DB_ADMIN_USER, pw), (LEGACY_DB_ADMIN_USER, '')]
        for as_user, as_pw in attempts:
            try:
                c = mariadb.connect(unix_socket=sock, user=as_user, password=as_pw)
                cur = c.cursor()
                cur.execute(
                    "CREATE USER IF NOT EXISTS '" + _sq(user) + "'@'localhost' "
                    "IDENTIFIED BY '" + _sq(pw) + "'")
                cur.execute(
                    "ALTER USER '" + _sq(user) + "'@'localhost' "
                    "IDENTIFIED BY '" + _sq(pw) + "'")
                cur.execute(
                    "GRANT ALL PRIVILEGES ON *.* TO '" + _sq(user) + "'@'localhost' "
                    "WITH GRANT OPTION")
                cur.execute('FLUSH PRIVILEGES')
                c.close()
                if as_user == user:
                    logger.info("[DB] Repaired the DB admin password for TCP auth.")
                else:
                    logger.info(
                        "[DB] Migrated DB admin: created the configured account "
                        "from the legacy one.")
                return
            except Exception as e:
                last = e
        logger.warning(f"[DB] Admin user setup failed: {type(last).__name__}")

def _start_mariadb():
    global _mariadb_proc
    try:
        # Run socket connection in a subprocess to avoid CFNetwork initialization in the parent process
        check_code = "import socket,sys; s=socket.create_connection(('127.0.0.1', 3306), timeout=0.5); s.close(); sys.exit(0)"
        result = subprocess.run([sys.executable, '-c', check_code], capture_output=True)
        if result.returncode == 0:
            logger.info("[DB] The database is already running.")
            # A server we did not start still needs the admin account. A store
            # that already runs its own MariaDB on 3306 never reaches the
            # bootstrap path below, so without this the account is never
            # created and every launch dies with "Access denied for user".
            _ensure_admin_user()
            return
    except Exception:
        pass

    # --- Auto Installer (Check if MariaDB exists) ---
    os.environ['PYTORCH_ENABLE_MPS_FALLBACK'] = '1'
    if platform.system() == 'Darwin':
        mariadbd_sys = shutil.which('mariadbd')
        has_brew_mariadb = mariadbd_sys or os.path.exists('/opt/homebrew/bin/mariadbd') or os.path.exists('/usr/local/bin/mariadbd')
        
        if not has_brew_mariadb:
            logger.info("[DB] Using the bundled database.")

    datadir = DB_DIR
    mariadbd_bin = _get_binary_path('mariadbd')
    
    env = os.environ.copy()
    app_d = get_app_dir()
    lib_path = os.path.abspath(os.path.join(app_d, 'python_env', 'lib'))
    paths = [p for p in [lib_path] if os.path.exists(p)]
    if paths:
        env['DYLD_LIBRARY_PATH'] = ":".join(paths) + ":" + env.get('DYLD_LIBRARY_PATH', '')

    sys_tables = os.path.abspath(os.path.join(app_d, 'db_support', 'share', 'mysql', 'mariadb_system_tables.sql'))
    if not os.path.exists(sys_tables):
        sys_tables = os.path.abspath(os.path.join(app_d, 'app', 'db_support', 'share', 'mysql', 'mariadb_system_tables.sql'))

    # MariaDB 10.4+ keeps accounts in mysql.global_priv (mysql.user is a view with no
    # .MAD file), so checking user.MAD alone re-ran this "first launch" step on every
    # launch. Either file means the system tables already exist.
    _mysql_dir = os.path.join(datadir, 'mysql')
    if not any(os.path.exists(os.path.join(_mysql_dir, f)) for f in ('global_priv.MAD', 'user.MAD')):
        logger.info("[DB] Initializing the database system tables...")
        _report_status("Creating the database (first launch only)...", progress=0.3)
        try:
            sql_init = f"CREATE DATABASE IF NOT EXISTS mysql;\nUSE mysql;\n"
            if os.path.exists(sys_tables):
                with open(sys_tables, 'r', encoding='utf-8') as f:
                    sql_init += f.read()
            sql_init += (
                "\nFLUSH PRIVILEGES;\n"
                "CREATE USER IF NOT EXISTS '" + _sq(_db_root_user()) + "'@'localhost' "
                "IDENTIFIED BY '" + _sq(_db_root_password()) + "';\n"
                "GRANT ALL PRIVILEGES ON *.* TO '" + _sq(_db_root_user()) + "'@'localhost' "
                "WITH GRANT OPTION;\nFLUSH PRIVILEGES;\n")
            
            p = subprocess.Popen([mariadbd_bin, f'--datadir={datadir}', '--bootstrap'],
                                 stdin=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
            _, err = p.communicate(input=sql_init.encode('utf-8'))
            for line in err.decode('utf-8', 'replace').splitlines():
                if _db_error_line(line):
                    logger.error(f"[DB] {_db_error_line(line)}")
            logger.info("[DB] Database bootstrap completed.")
        except Exception as e:
            logger.error(f"[DB] Initialization error: {e}")

    logger.info("[DB] Starting the database...")
    _report_status("Starting the database...", progress=0.4)
    cmd = [
        mariadbd_bin,
        f'--datadir={datadir}',
        '--port=3306',
        '--bind-address=127.0.0.1',
        '--max-connections=100'
    ]
    try:
        # Read through the logger (log_redact.py, which strips the data folder and
        # binary paths they name) into bootstrap.log: the launcher discards stderr.
        _mariadb_proc = subprocess.Popen(cmd, env=env, stderr=subprocess.PIPE)
        threading.Thread(target=_forward_mariadb_output, args=(_mariadb_proc.stderr,), daemon=True).start()
        start_time = time.time()
        while time.time() - start_time < 30:
            try:
                check_code = "import socket,sys; s=socket.create_connection(('127.0.0.1', 3306), timeout=1.0); s.close(); sys.exit(0)"
                result = subprocess.run([sys.executable, '-c', check_code], capture_output=True)
                if result.returncode == 0:
                    logger.info("[DB] The database is up.")
                    break
                time.sleep(1)
            except Exception:
                time.sleep(1)
        else:
            logger.warning("[DB] The database did not come up in time.")
    except Exception as e:
        logger.error(f"[DB] Launch error: {e}")
    _ensure_admin_user()

def _db_error_line(line):
    """The message of an [ERROR] line from mariadbd, else ''.

    Only errors are worth a line in the log. The [Note]/[Warning] lines name the
    server's version and build, its socket and port, and every account that
    failed to log in — this Mac's user name among them.
    """
    if "[ERROR]" not in line:
        return ""
    return line.split("[ERROR]", 1)[1].strip()


def _forward_mariadb_output(stream):
    # Must keep reading for as long as mariadbd runs: a full pipe would block it.
    for raw in iter(stream.readline, b''):
        try:
            line = _db_error_line(raw.decode('utf-8', 'replace'))
            if line:
                logger.error(f"[DB] {line}")
        except Exception:
            pass


def _stop_mariadb():
    global _mariadb_proc
    if _mariadb_proc:
        logger.info("[DB] Stopping the database...")
        try:
            _mariadb_proc.terminate()
            _mariadb_proc.wait(timeout=5)
        except Exception:
            try:
                _mariadb_proc.kill()
            except Exception:
                pass
        _mariadb_proc = None
# --------------------------------------------------------------------------------------
# Shutdown Signal Cleanup Handler
# --------------------------------------------------------------------------------------
_is_shutting_down = False

def _cleanup_and_exit(signum=None, frame=None):
    global _is_shutting_down
    if _is_shutting_down:
        return
    _is_shutting_down = True
    
    logger.info("[BOOTSTRAP] Shutdown signal received. Cleaning up resources...")
    try:
        _stop_caffeinate()
        _stop_mariadb()
    except Exception:
        pass
        
    if os.path.exists(PID_FILE):
        try:
            os.remove(PID_FILE)
        except Exception:
            pass
    _remove_legacy_pid()
            
    # Forcefully terminate all child processes in the group (e.g. data_service, frontend)
    try:
        import signal as _sig
        # Get process group id instead of using 0 which might be buggy on some systems
        pgid = os.getpgid(0)
        os.killpg(pgid, _sig.SIGKILL)
    except Exception as e:
        logger.error(f"Error killing process group: {e}")
    finally:
        os._exit(0)

# --------------------------------------------------------------------------------------
# Main Execution
# --------------------------------------------------------------------------------------
def _frontend_port():
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import port_store
        return port_store.get_port('frontend_web', 3000)
    except Exception:
        return 3000


def run_loading_gui(port_num):
    # The launcher already put the window up the moment the app was opened.
    if os.environ.get('AICAMERA_SPLASH_SHOWN') == '1':
        return
    app_d = get_app_dir()
    gui_path = os.path.join(app_d, ".loading_gui.py")
    if not os.path.exists(gui_path):
        return
    python_bin = os.path.join(app_d, "python_env", "bin", "python3.12")
    if not os.path.exists(python_bin):
        python_bin = sys.executable

    # Launch GUI in a completely separate process to avoid Tcl_AsyncDelete fork issues
    # and to prevent socket CFNetwork poisoning in the parent process
    subprocess.Popen([python_bin, gui_path],
                     env=dict(os.environ, AICAMERA_FRONTEND_PORT=str(port_num)))


def _log_uncaught_errors():
    """Uncaught errors go to bootstrap.log: the launcher discards stderr now, so
    a crash would otherwise leave no trace. The log filter (log_redact.py)
    strips paths from the traceback."""
    def on_error(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            return sys.__excepthook__(exc_type, exc, tb)
        logger.critical("Uncaught error", exc_info=(exc_type, exc, tb))

    def on_thread_error(args):
        if args.exc_type is SystemExit:
            return
        logger.error(f"Uncaught error in thread {getattr(args.thread, 'name', '?')}",
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = on_error
    threading.excepthook = on_thread_error


def main():
    _log_uncaught_errors()
    _anti_debug()
    _check_duplicate()
    
    signal.signal(signal.SIGINT, _cleanup_and_exit)
    signal.signal(signal.SIGTERM, _cleanup_and_exit)

    logger.info("==================================================")
    logger.info("🚀 Starting AICamera Standalone Bootstrap Launcher")
    logger.info("==================================================")

    _report_status("Preparing your workspace...", progress=0.1)
    _fix_gatekeeper()
    _register_login_item()
    _start_caffeinate()
    _take_over_git_clone()

    _start_mariadb()
    
    # Scan and migrate DLC folder files if needed
    _report_status("Checking modules...", progress=0.5)
    for dirpath, _, filenames in os.walk(MANAGED_DIR):
        for fname in filenames:
            _migrate_old_encryption(os.path.join(dirpath, fname))

    # Launch client_logic.py
    app_d = get_app_dir()
    for p in [app_d, os.path.join(app_d, 'app')]:
        if os.path.exists(p) and p not in sys.path:
            sys.path.insert(0, p)
    client_logic_path = os.path.join(app_d, 'app', 'client_logic.py')
    if not os.path.exists(client_logic_path):
        client_logic_path = os.path.join(app_d, 'client_logic.py')
    if os.path.exists(client_logic_path):
        logger.info("[BOOTSTRAP] Launching the supervisor...")
        try:
            import multiprocessing
            multiprocessing.freeze_support()
            multiprocessing.set_start_method('spawn', force=True)
        except Exception:
            pass
        
        with open(client_logic_path, 'rb') as f:
            first_chunk = f.read(40)
        
        sys._machine_cipher = machine_cipher
        global_scope = {'__name__': '__main__', '__file__': client_logic_path}
        
        # Launch the GUI subprocess
        port_num = _frontend_port()
        _report_status("Starting services (the first launch may take a few minutes)...", port=port_num, progress=0.6)
        run_loading_gui(port_num)
        
        import runpy
        try:
            runpy.run_path(client_logic_path, init_globals=global_scope, run_name='__main__')
        except SystemExit:
            pass
        
        # Prevent macOS LaunchServices from reopening the app.
        logger.info("[BOOTSTRAP] client_logic.py exited. Suspending to prevent re-launch loop.")
        import signal as _sig
        _sig.pause()
    else:
        logger.error("[BOOTSTRAP] client_logic.py not found!")
        _report_status("Some app files are missing (client_logic not found). Please reinstall the app.", state='error')

if __name__ == '__main__':
    main()
