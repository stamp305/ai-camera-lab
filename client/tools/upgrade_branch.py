"""Bring a branch installed by git clone up to the current client, after `git pull`.

    bash tools/upgrade_branch.sh            upgrade (safe to run again)
    bash tools/upgrade_branch.sh --check    report only, change nothing
    bash tools/upgrade_branch.sh --stop     stop the client and its LaunchAgent
    bash tools/upgrade_branch.sh --start    start them again

Each step checks what is already in place and leaves it alone, so a branch that
is half done, or done, can run this again. It stops at the first step that fails
and says why; nothing after that step has been touched.

Never deleted, only moved aside into the backup folder: stale compiled modules,
an identity store without the activation. Every run backs up the database first.

What it cannot do: the admin still has to push the branch's services (DLC) once
the client is running with its Device Key.
"""
import datetime
import glob
import os
import re
import shutil
import subprocess
import sys
import time

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_SUPPORT = os.path.expanduser("~/Library/Application Support/AICamera")
DB_SOCKET = os.environ.get("AICAM_DB_SOCKET", "/tmp/mysql.sock")
LAUNCHD_LABEL = os.environ.get("AICAM_LAUNCHD_LABEL", "com.advice.aicamera.client")
PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{LAUNCHD_LABEL}.plist")
LEGACY_COMMIT = "2ac7e5c"   # the last release before this one; its client.py holds the DLC key
DLC_KEY_FINGERPRINT = "b39ede90535c"
APP_USER, APP_PASSWORD = "advice", os.environ.get("AICAM_DB_PASSWORD", "")   # the same on every branch
LEGACY_ROOT_PASSWORD = os.environ.get("AICAM_LEGACY_ROOT_PASSWORD", "")   # what the store setup guide told every branch to set

CHECK_ONLY = "--check" in sys.argv
BACKUP_DIR = os.path.expanduser(
    f"~/aicamera_upgrade_backups/{datetime.datetime.now():%Y%m%d-%H%M%S}")

SECRETS_TEMPLATE = '''DB_USER = "advice"
DB_PASSWORD = os.environ.get("AICAM_DB_PASSWORD", "")
DB_NAME = "ai_camera"
DB_HOST = "localhost"
DB_PORT = 3306
DB_ROOT_USER = "advice"
DB_ROOT_PASSWORD = os.environ.get("AICAM_DB_ROOT_PASSWORD", "")
DB_VIEWER_USER = "client"
DB_VIEWER_PASSWORD = os.environ.get("AICAM_DB_VIEWER_PASSWORD", "")
API_SERVER_HOST = "https://app.advice.th/ai-camera/"
API_SERVER_PORT = 443
SOCKET_SERVERS = [{{"url": "https://app.advice.th:443", "path": "/ai-camera/socket.io"}}]
SOCKET_SSL_VERIFY = False
STATIC_LEGACY_KEY = b'{key}'
CAMERA_SECRET_KEY = ""
'''

# Build output that shadows a .py of the same name when it is left beside it
# (Python loads the .so first). The one compiled module the client needs is kept.
KEEP_COMPILED = re.compile(r"^data_service/configs/secure_env(\.cpython-[^/]*\.so|\.c)$")
SKIP_DIRS = {".git", "dlc_local", "build", "python_env", "node_modules", "frontend"}


class Stop(Exception):
    pass


def ok(msg):
    print(f"  ✅ {msg}")


def warn(msg):
    print(f"  ⚠️  {msg}")


def step(title):
    print(f"\n── {title}")


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def backup(path, name=None):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    dest = os.path.join(BACKUP_DIR, name or os.path.basename(path))
    shutil.copy2(path, dest)
    return dest


# ------------------------------------------------------------------ database
def _connect(user, password):
    import mariadb
    return mariadb.connect(unix_socket=DB_SOCKET, user=user, password=password, connect_timeout=5)


def _can_login(user, password):
    try:
        _connect(user, password).close()
        return True
    except Exception:
        return False


def _legacy_env():
    """The old version's .env as a dict ({} when there is none)."""
    env = {}
    try:
        for line in open(os.path.join(APP_DIR, ".env")):
            key, sep, value = line.strip().partition("=")
            if sep:
                env[key] = value.strip()
    except OSError:
        pass
    return env


def _can_create_users(user, password):
    try:
        conn = _connect(user, password)
    except Exception:
        return False
    try:
        cur = conn.cursor()
        cur.execute("SHOW GRANTS")
        grants = " ".join(row[0] for row in cur.fetchall()).upper()
        return "ALL PRIVILEGES ON *.*" in grants or "CREATE USER" in grants
    finally:
        conn.close()


def db_admin_login():
    """The first account that can create DB users: the new one, then what the old install used.

    Older releases kept root's password in .env as DB_ROOT_PASSWORD; the earliest
    ones connected as DB_USER itself (root, on those branches). The store setup
    guide told every branch to set root to <legacy root password>.
    """
    old = _legacy_env()
    tried = []
    for user, pw in ((APP_USER, APP_PASSWORD),
                     (old.get("DB_ROOT_USER") or "root", old.get("DB_ROOT_PASSWORD")),
                     (old.get("DB_USER"), old.get("DB_PASSWORD")),
                     ("root", LEGACY_ROOT_PASSWORD), ("root", "")):
        if not user or pw is None or (user, pw) in tried:
            continue
        tried.append((user, pw))
        if _can_create_users(user, pw):
            return user, pw
    raise Stop("เข้า MariaDB ด้วยสิทธิ์สร้าง user ไม่ได้ (ลอง advice, บัญชีใน .env เดิม, root/<legacy root password>, root ไม่มีรหัส) — "
               "ต้องรู้รหัส root ของเครื่องนี้ก่อน")


# ------------------------------------------------------------------ steps
def check_python():
    step("1. Python")
    if sys.version_info[:2] < (3, 10):
        raise Stop(f"ต้องใช้ Python 3.10 ขึ้นไป แต่ได้ {sys.version.split()[0]}")
    try:
        import mariadb  # noqa: F401
        import fastapi  # noqa: F401
    except ImportError as e:
        raise Stop(f"env นี้ขาด package ({e.name}) — ต้องใช้ env ai-camera")
    ok(f"{sys.executable} ({sys.version.split()[0]})")


def check_repo():
    step("2. โค้ด")
    if not os.path.exists(os.path.join(APP_DIR, "client_logic.py")):
        raise Stop("ยังไม่ได้ git pull (ไม่มี client_logic.py)")
    head = run(["git", "-C", APP_DIR, "log", "-1", "--format=%h %s"]).stdout.strip()
    ok(f"commit {head}")
    changed = [l[3:] for l in run(["git", "-C", APP_DIR, "status", "--porcelain",
                                   "--untracked-files=no"]).stdout.splitlines()]
    if changed and CHECK_ONLY:
        # --check changes nothing, so a local edit is only worth reporting —
        # stopping here hid every later check on a branch carrying a hand fix.
        warn("มีไฟล์ใน git ถูกแก้ไว้ (ตรวจต่อ แต่ต้องจัดการก่อนอัปเกรดจริง):\n      "
             + "\n      ".join(changed))
        return
    if changed:
        raise Stop("มีไฟล์ใน git ถูกแก้ไว้ — ต้องดูก่อนว่าเป็นของสาขาหรือไม่ แล้ว "
                   "git checkout -- <ไฟล์> ถ้าทิ้งได้:\n      " + "\n      ".join(changed))
    ok("ไม่มีไฟล์ถูกแก้")


def stale_compiled():
    found = []
    for root, dirs, files in os.walk(APP_DIR):
        rel_root = os.path.relpath(root, APP_DIR)
        if rel_root == ".":
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f.endswith((".so", ".c")):
                rel = os.path.normpath(os.path.join(rel_root, f))
                if not KEEP_COMPILED.match(rel):
                    found.append(rel)
    return sorted(found)


def clear_stale_compiled():
    step("3. ไฟล์ compile ค้าง (.so/.c)")
    found = stale_compiled()
    if not found:
        ok("ไม่มี")
        return
    if CHECK_ONLY:
        warn(f"เจอ {len(found)} ไฟล์ — จะทำให้ Python โหลดโค้ดเก่าแทนโค้ดใหม่ เช่น {found[0]}")
        return
    dest_root = os.path.join(BACKUP_DIR, "stale_compiled")
    for rel in found:
        dest = os.path.join(dest_root, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(os.path.join(APP_DIR, rel), dest)
    ok(f"ย้าย {len(found)} ไฟล์ไปที่ {dest_root}")


# `python client.py` as a branch runs it (relative), with a full path, or the
# supervisor's multiprocessing children.
_CLIENT_CMD = re.compile(r"(^|[\s/])(client|client_logic|bootstrap)\.py(\s|$)|multiprocessing\.(spawn|resource_tracker)")
# The old version's UI: `bun run build` / `bun start` (Next.js) under frontend/.
_OLD_UI_EXES = ("bun", "node", "next-server")


def client_pids():
    """This folder's client, the services it started, and the old version's UI — nothing else.

    A process counts only when its working directory is this folder (the UI: its
    frontend/ folder) and it is one of those programs, so a terminal sitting in the
    folder, or a client of another checkout, is left alone.
    """
    pids = []
    for line in run(["ps", "-axo", "pid=,command="]).stdout.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        if not pid.isdigit() or int(pid) == os.getpid():
            continue
        exe = os.path.basename(cmd.split(" ", 1)[0])
        is_client = exe.startswith("python") and _CLIENT_CMD.search(cmd)
        is_old_ui = exe in _OLD_UI_EXES or exe.startswith("next-server")
        if not (is_client or is_old_ui):
            continue
        cwd = run(["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"]).stdout.partition("\nn")[2].strip()
        if is_client and cwd == APP_DIR:
            pids.append(int(pid))
        elif is_old_ui and (cwd == APP_DIR or cwd.startswith(os.path.join(APP_DIR, "frontend"))):
            pids.append(int(pid))
    return pids


def _port_holders():
    """{port: 'pid command'} for the ports this client binds that something still holds."""
    held = {}
    for port in (3000, 8000, 8001):
        out = run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpc"]).stdout.split()
        if out:
            held[port] = " ".join(x[1:] for x in out[:2])
    return held


def stop_client():
    step("4. หยุด client ที่รันอยู่")
    if CHECK_ONLY:
        n = len(client_pids())
        ok(f"กำลังรันอยู่ {n} โปรเซส") if n else warn("client ไม่ได้รันอยู่")
        return
    if os.path.exists(PLIST):
        run(["launchctl", "bootout", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"])
    pids = client_pids()
    for pid in pids:
        cmd = run(["ps", "-o", "command=", "-p", str(pid)]).stdout.strip()
        print(f"      หยุด {pid}: {cmd[:100]}")
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            pass
    # SIGTERM makes the supervisor stop its services but not exit (it waits for a
    # second signal), so whatever is still up after the grace period is killed.
    for _ in range(15):
        if not client_pids():
            break
        time.sleep(1)
    for pid in client_pids():
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass
    time.sleep(1)
    if client_pids():
        raise Stop(f"หยุดไม่ได้: {client_pids()}")
    held = {} if os.environ.get("AICAM_SKIP_PORT_CHECK") else _port_holders()
    if held:
        raise Stop("port ยังถูกโปรแกรมอื่นใช้อยู่ — client ใหม่จะเปิดไม่ได้: "
                   + ", ".join(f"{p} ({who})" for p, who in held.items()))
    ok(f"หยุดแล้ว ({len(pids)} โปรเซส)" if pids else "ไม่ได้รันอยู่")


def backup_data():
    step("5. สำรองข้อมูล")
    if CHECK_ONLY:
        ok("(ข้ามตอน --check)")
        return
    user, pw = db_admin_login()
    # A year of logs is a few GB of dump; running out of disk half way would also
    # starve MariaDB and the recorder, so check first.
    conn = _connect(user, pw)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name = 'ai_camera'")
    has_db = int(cur.fetchone()[0]) > 0
    cur.execute("SELECT COALESCE(SUM(data_length + index_length), 0) FROM information_schema.tables"
                " WHERE table_schema = 'ai_camera'")
    db_bytes = int(cur.fetchone()[0])
    conn.close()
    if not has_db:   # a new machine: client.py creates the schema on its first boot
        ok("ยังไม่มี DB ai_camera (เครื่องใหม่) — ไม่มีอะไรให้สำรอง")
        os.makedirs(BACKUP_DIR, exist_ok=True)
        for name in ("client_config.json", ".env", "_secrets.py"):
            if os.path.exists(os.path.join(APP_DIR, name)):
                backup(os.path.join(APP_DIR, name))
        return
    free = shutil.disk_usage(os.path.expanduser("~")).free
    need = db_bytes * 2 + 2 * 1024 ** 3
    if free < need:
        raise Stop(f"พื้นที่ดิสก์ไม่พอสำรอง DB: ว่าง {free // 1024 ** 3} GB ต้องการประมาณ "
                   f"{need // 1024 ** 3} GB — ลบ backup เก่าใน ~/aicamera_upgrade_backups ก่อน")
    os.makedirs(BACKUP_DIR, exist_ok=True)
    dump = os.path.join(BACKUP_DIR, "ai_camera.sql")
    tool = shutil.which("mariadb-dump") or "/opt/homebrew/bin/mariadb-dump"
    with open(dump, "w") as out:
        r = subprocess.run([tool, f"--socket={DB_SOCKET}", "-u", user, "ai_camera"],
                           stdout=out, stderr=subprocess.PIPE, text=True,
                           env=dict(os.environ, MYSQL_PWD=pw))
    tables = open(dump).read().count("CREATE TABLE")
    if r.returncode != 0 or tables == 0:
        raise Stop(f"สำรอง DB ไม่สำเร็จ: {r.stderr.strip()[:200]}")
    ok(f"DB {tables} ตาราง → {dump}")
    for name in ("client_config.json", ".env", "_secrets.py"):
        if os.path.exists(os.path.join(APP_DIR, name)):
            backup(os.path.join(APP_DIR, name))
    ok(f"client_config.json, .env, _secrets.py → {BACKUP_DIR}")


def install_packages():
    step("6. Python packages")
    if CHECK_ONLY or os.environ.get("AICAM_SKIP_PIP"):
        missing = [m for m in ("pymysql", "Cython") if run([sys.executable, "-c", f"import {m}"]).returncode]
        warn(f"ยังไม่มี: {', '.join(missing)}") if missing else ok("pymysql, Cython มีแล้ว")
        return
    log = os.path.join(BACKUP_DIR, "pip.log")
    os.makedirs(BACKUP_DIR, exist_ok=True)
    with open(log, "w") as out:
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-r",
                            os.path.join(APP_DIR, "requirements.txt"), "Cython"],
                           stdout=out, stderr=subprocess.STDOUT)
    if r.returncode != 0:
        tail = open(log).read().strip().splitlines()[-5:]
        raise Stop("pip install ไม่ผ่าน:\n      " + "\n      ".join(tail))
    ok("ติดตั้งครบ")


def _dlc_key():
    old = run(["git", "-C", APP_DIR, "show", f"{LEGACY_COMMIT}:client.py"]).stdout
    keys = re.findall(r"""ENCRYPTION_KEY\s*=\s*b?['"]([A-Za-z0-9_\-]{43}=)['"]""", old)
    if not keys:
        raise Stop(f"หา key ถอดรหัส DLC ใน {LEGACY_COMMIT} ไม่เจอ")
    return keys[0]


def _fingerprint(key):
    import hashlib
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def _read_secrets():
    ns = {}
    exec(open(os.path.join(APP_DIR, "_secrets.py")).read(), ns)
    return ns


def ensure_secrets():
    step("7. _secrets.py")
    path = os.path.join(APP_DIR, "_secrets.py")
    if not os.path.exists(path):
        if CHECK_ONLY:
            warn("ยังไม่มี")
            return
        key = _dlc_key()
        if _fingerprint(key) != DLC_KEY_FINGERPRINT:
            raise Stop("key ถอดรหัส DLC ไม่ตรงกับของ admin")
        with open(path, "w") as f:
            f.write(SECRETS_TEMPLATE.format(key=key))
        os.chmod(path, 0o600)
        ok("สร้างใหม่")
    s = _read_secrets()
    key = s.get("STATIC_LEGACY_KEY") or b""
    key = key.decode() if isinstance(key, bytes) else key
    if s.get("DB_USER") != APP_USER or s.get("DB_ROOT_USER") != APP_USER:
        raise Stop(f"_secrets.py มี DB_USER={s.get('DB_USER')} — ต้องเป็น {APP_USER} "
                   "(ลบไฟล์แล้วรันใหม่เพื่อสร้างให้ ของเดิมอยู่ใน backup)")
    if not key:
        if CHECK_ONLY:
            warn("ไม่มี STATIC_LEGACY_KEY — จะรับ service จาก admin ไม่ได้")
            return
        key = _dlc_key()
        text = open(path).read()
        if re.search(r"^STATIC_LEGACY_KEY\s*=", text, flags=re.M):
            text = re.sub(r"^STATIC_LEGACY_KEY\s*=.*$", f"STATIC_LEGACY_KEY = b'{key}'", text, flags=re.M)
        else:
            text += f"\nSTATIC_LEGACY_KEY = b'{key}'\n"
        open(path, "w").write(text)
        ok("ใส่ STATIC_LEGACY_KEY แล้ว")
    if _fingerprint(key) != DLC_KEY_FINGERPRINT:
        raise Stop("STATIC_LEGACY_KEY ไม่ตรงกับของ admin")
    ok(f"DB_USER={APP_USER}, DLC key {DLC_KEY_FINGERPRINT}")


def _baked_env():
    code = ("from data_service.configs.secure_env import load_secure_env as l; import os, json; l();"
            "print(json.dumps({k: os.environ[k] for k in ('DB_USER', 'DB_ROOT_USER', 'SOCKET_SERVER_HOST')}))")
    r = run([sys.executable, "-c", code], cwd=APP_DIR)
    if r.returncode != 0:
        return None
    import json
    return json.loads(r.stdout.strip().splitlines()[-1])


def build_secure_env():
    step("8. secure_env (compile)")
    so = glob.glob(os.path.join(APP_DIR, "data_service/configs/secure_env.cpython-*.so"))
    secrets_path = os.path.join(APP_DIR, "_secrets.py")
    if not os.path.exists(secrets_path):   # --check before the file is created
        warn("ยังไม่มี _secrets.py — จะ build ตอนอัปเกรดจริง")
        return
    fresh = so and all(os.path.getmtime(p) >= os.path.getmtime(secrets_path) for p in so)
    baked = _baked_env() if fresh else None
    want = {"DB_USER": APP_USER, "DB_ROOT_USER": APP_USER, "SOCKET_SERVER_HOST": "https://app.advice.th:443"}
    if baked == want:
        ok("build แล้ว ค่าถูกต้อง")
        return
    if CHECK_ONLY:
        warn(f"ต้อง build ใหม่ (ตอนนี้: {baked})")
        return
    # The generator takes an environment variable over _secrets.py; a stray
    # DB_USER in this shell must not end up baked in.
    env = {k: v for k, v in os.environ.items()
           if not re.match(r"(DB_|API_SERVER|SOCKET_|CAMERA_SECRET)", k)}
    for cmd in ([sys.executable, "generate_secure_env.py"],
                [sys.executable, "setup_secure_env.py", "build_ext", "--inplace"]):
        r = run(cmd, cwd=APP_DIR, env=env)
        if r.returncode != 0:
            raise Stop(f"{' '.join(cmd[1:])} ไม่ผ่าน: {(r.stderr or r.stdout).strip()[-300:]}")
    baked = _baked_env()
    if baked != want:
        raise Stop(f"build แล้วแต่ค่าไม่ตรง: {baked}")
    ok("build ใหม่แล้ว ค่าถูกต้อง")


def ensure_db_user():
    step("9. DB user advice")
    password = APP_PASSWORD
    if _can_login(APP_USER, password):
        ok("เข้าได้แล้ว")
        return
    if CHECK_ONLY:
        warn("ยังเข้าไม่ได้")
        return
    user, pw = db_admin_login()
    conn = _connect(user, pw)
    cur = conn.cursor()
    for host in ("localhost", "127.0.0.1"):
        cur.execute(f"CREATE USER IF NOT EXISTS '{APP_USER}'@'{host}' IDENTIFIED BY '{password}'")
        cur.execute(f"ALTER USER '{APP_USER}'@'{host}' IDENTIFIED BY '{password}'")
        cur.execute(f"GRANT ALL PRIVILEGES ON *.* TO '{APP_USER}'@'{host}' WITH GRANT OPTION")
    cur.execute("FLUSH PRIVILEGES")
    conn.close()
    if not _can_login(APP_USER, password):
        raise Stop("สร้างแล้วแต่ยังเข้าไม่ได้")
    ok(f"สร้างด้วย {user}@localhost แล้ว")


def ensure_identity():
    step("10. การ activate")
    legacy_path = os.path.join(APP_DIR, "client_config.json")
    legacy_code = ""
    if os.path.exists(legacy_path):
        import json
        legacy_code = str(json.load(open(legacy_path)).get("activateID") or "").strip()
    store = os.path.join(APP_SUPPORT, ".lcache")
    sys.path.insert(0, APP_DIR)
    import device_identity as d
    if os.path.exists(store):
        state = d.load()
        if d.is_activated():
            ok(f"activate แล้ว ({state.get('activateID')}) | Device Key: {'มี' if d.device_key() else 'ยังไม่มี'}")
            return
        if not legacy_code:
            warn("ยังไม่ได้ activate และไม่มี client_config.json เดิม — ต้อง activate จากหน้าเว็บ")
            return
        if CHECK_ONLY:
            warn("ไฟล์ identity ไม่มีการ activate ทั้งที่ client_config.json มี — รันโดยไม่ใส่ --check เพื่อแก้")
            return
        # Made by a run before client_config.json was in place: holds no activation,
        # and the client only migrates when there is no store at all.
        os.makedirs(BACKUP_DIR, exist_ok=True)
        shutil.move(store, os.path.join(BACKUP_DIR, ".lcache"))
        warn("ย้ายไฟล์ identity ที่ไม่มีการ activate ไปไว้ใน backup")
    if not legacy_code:
        warn("ไม่มี client_config.json ที่มี activateID — ต้อง activate จากหน้าเว็บ")
        return
    if CHECK_ONLY:
        warn("ยังไม่ได้ย้ายการ activate")
        return
    os.makedirs(APP_SUPPORT, exist_ok=True)
    shutil.copy2(legacy_path, os.path.join(APP_SUPPORT, "client_config.json"))
    state = d.load()
    if not d.is_activated():
        raise Stop("ย้ายแล้วแต่ยังไม่ activate")
    ok(f"ย้ายจาก client_config.json แล้ว ({state.get('activateID')})")


def carry_over_services():
    """Bring services the old client kept in dlc_local/ into managed_services/.

    The old client ran its services out of <repo>/dlc_local; this one runs them out of
    App Support/managed_services and never looks at dlc_local again. The admin only
    re-sends the services assigned to the branch now, so anything else the shop ran
    (in_pass, hotzone, rate, log on one branch — Live Display went blank) vanished
    with the upgrade. A folder the admin already delivered with its main is left as
    it is; one that arrived without its main (a push cut off half way) is moved to the
    backup folder and replaced. .py files are written in plain text, as this client
    writes what the admin sends on a git-clone branch. dlc_local itself is not touched.
    """
    step("10b. service เดิมใน dlc_local")
    src_root = os.path.join(APP_DIR, "dlc_local")
    managed = os.path.join(APP_SUPPORT, "managed_services")
    if not os.path.isdir(src_root):
        ok("ไม่มี dlc_local (ติดตั้งใหม่ ไม่มีของเดิมให้ย้าย)")
        return

    def has_main(folder):
        return bool(glob.glob(os.path.join(folder, "main.*")))

    todo = []
    for name in sorted(os.listdir(src_root)):
        src = os.path.join(src_root, name)
        if name.startswith((".", "__")) or not os.path.isdir(src):
            continue
        dest = os.path.join(managed, name)
        if has_main(src):
            if has_main(dest):
                continue
        elif os.path.isdir(dest):  # configs/, events/: shared files, no main
            continue
        todo.append(name)
    if not todo:
        ok("service ใน dlc_local มีใน managed_services ครบแล้ว")
        return
    if CHECK_ONLY:
        warn(f"ยังไม่ได้ย้าย: {', '.join(todo)} (รันโดยไม่ใส่ --check เพื่อย้าย)")
        return

    from cryptography.fernet import Fernet, InvalidToken
    cipher = Fernet(_read_secrets()["STATIC_LEGACY_KEY"])
    os.makedirs(managed, exist_ok=True)
    moved, failed = [], []
    for name in todo:
        src = os.path.join(src_root, name)
        tmp = os.path.join(managed, f".carry-{name}")
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            for root, dirs, files in os.walk(src):
                dirs[:] = [d for d in dirs if d != "__pycache__"]
                for fname in files:
                    if fname.endswith(".pyc") or fname == ".DS_Store":
                        continue
                    path = os.path.join(root, fname)
                    out = os.path.join(tmp, os.path.relpath(path, src))
                    os.makedirs(os.path.dirname(out), exist_ok=True)
                    with open(path, "rb") as f:
                        data = f.read()
                    if fname.endswith(".py") and data.startswith(b"gAAAA"):
                        data = cipher.decrypt(data.strip())
                    with open(out, "wb") as f:
                        f.write(data)
                    shutil.copystat(path, out)
        except (InvalidToken, OSError) as e:
            shutil.rmtree(tmp, ignore_errors=True)
            failed.append(f"{name} ({type(e).__name__})")
            continue
        dest = os.path.join(managed, name)
        if os.path.isdir(dest):  # delivered without its main: keep it in the backup
            os.makedirs(BACKUP_DIR, exist_ok=True)
            shutil.move(dest, os.path.join(BACKUP_DIR, f"managed_{name}"))
        os.rename(tmp, dest)
        moved.append(name)
    if moved:
        ok(f"ย้ายแล้ว: {', '.join(moved)} (dlc_local เดิมยังอยู่)")
    if failed:
        warn(f"ย้ายไม่ได้: {', '.join(failed)} — ให้ admin ส่ง service เหล่านี้ให้สาขา")


def _plist():
    logs = os.path.join(APP_SUPPORT, "logs")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>{APP_DIR}/tools/run_client.sh</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict><key>AICAM_PYTHON</key><string>{sys.executable}</string></dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>30</integer>
    <key>StandardOutPath</key><string>{logs}/launchd.log</string>
    <key>StandardErrorPath</key><string>{logs}/launchd.log</string>
</dict>
</plist>
"""


def report_runtime():
    """What only a running client can show: services delivered and up, no decrypt errors."""
    step("12. client ที่รันอยู่")
    # client.log is sealed for the server now (log_seal.py); the client keeps the
    # lines checked here in a small events file instead (port_store.ServiceEventTap).
    sys.path.insert(0, APP_DIR)
    import port_store
    log = port_store.EVENTS_FILE
    lines = open(log, errors="replace").read().splitlines() if os.path.exists(log) else []
    starts = [i for i, l in enumerate(lines) if "[CLIENT] main() started" in l]
    since = lines[starts[-1]:] if starts else []
    if not since:
        warn("ยังไม่เคยรัน client เวอร์ชั่นนี้")
        return
    bad = [l for l in since if "Decryption failed" in l or "not set; cannot decrypt" in l]
    ok("ไม่มีไฟล์ service ที่ถอดรหัสไม่ผ่าน") if not bad else warn("มีไฟล์ service ที่ถอดรหัสไม่ผ่าน (รายละเอียดอยู่ใน log ที่ต้องเปิดที่ server)")
    if any("Admin refused a Device Key" in l for l in since):
        warn("admin ปฏิเสธ Device Key (รายละเอียดอยู่ใน log ที่ต้องเปิดที่ server)")
    managed = os.path.join(APP_SUPPORT, "managed_services")
    folders = sorted(f for f in os.listdir(managed) if not f.startswith(".")) if os.path.isdir(managed) else []
    ready = [f for f in folders if glob.glob(os.path.join(managed, f, "main.*"))]
    if not ready:
        warn("ยังไม่มี service — ให้ admin ส่ง service ให้สาขานี้")
    else:
        ok(f"service ที่มี main: {len(ready)} ({', '.join(ready)})")
    try:
        conn = _connect(APP_USER, APP_PASSWORD)
        cur = conn.cursor()
        cur.execute("SELECT name, port FROM ai_camera.service_ports WHERE type = 'api' ORDER BY port")
        ports = cur.fetchall()
        conn.close()
    except Exception as e:
        warn(f"อ่านตาราง service_ports ไม่ได้: {e}")
        return
    # Only what this branch should run: the core, and the services it was sent.
    wanted = [(n, p) for n, p in ports
              if n in ready or n in ("data_service", "activation_service", "frontend_web")]
    down = [f"{n}:{p}" for n, p in wanted
            if run(["lsof", "-nP", f"-iTCP:{p}", "-sTCP:LISTEN"]).returncode != 0]
    ok(f"port ขึ้นครบ {len(wanted)} ตัว") if not down else warn(f"port ไม่ขึ้น: {', '.join(down)}")


def launchd_loaded():
    return run(["launchctl", "print", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"]).returncode == 0


def ensure_launch_agent():
    step("11. เปิดอัตโนมัติ (LaunchAgent)")
    want = _plist()
    current = open(PLIST).read() if os.path.exists(PLIST) else None
    if CHECK_ONLY:
        if current != want:
            warn("ยังไม่ได้ติดตั้ง หรือไม่ตรงกับเวอร์ชั่นนี้")
        else:
            ok(f"ติดตั้งแล้ว | {'ทำงานอยู่' if launchd_loaded() else 'ยังไม่ได้เปิด'}")
        return
    os.makedirs(os.path.dirname(PLIST), exist_ok=True)
    os.makedirs(os.path.join(APP_SUPPORT, "logs"), exist_ok=True)
    if current != want:
        with open(PLIST, "w") as f:
            f.write(want)
    lint = run(["plutil", "-lint", PLIST])
    if lint.returncode != 0:
        raise Stop(f"plist ไม่ถูกต้อง: {lint.stdout.strip()}")
    ok(PLIST)


def start():
    if os.environ.get("AICAM_SKIP_LAUNCHCTL"):
        warn("ข้ามการเปิด (AICAM_SKIP_LAUNCHCTL)")
        return
    service = f"gui/{os.getuid()}/{LAUNCHD_LABEL}"
    run(["launchctl", "bootout", service])
    r = run(["launchctl", "bootstrap", f"gui/{os.getuid()}", PLIST])
    if r.returncode != 0:
        raise Stop(f"launchctl bootstrap ไม่ผ่าน: {r.stderr.strip()}")
    # bootstrap right after bootout can load the job without starting it (seen on a
    # branch: loaded, "runs = 0", no client) even with RunAtLoad. kickstart starts it
    # whatever launchd decided; without -k it leaves a job that is already running.
    run(["launchctl", "kickstart", service])
    for _ in range(10):
        time.sleep(1)
        info = run(["launchctl", "print", service]).stdout
        if re.search(r"^\s*pid = \d+", info, flags=re.M):
            ok("เปิดแล้ว client จะขึ้นภายในไม่กี่วินาที (หลังบูตเครื่องรอ 60 วินาที)")
            return
    raise Stop("launchd โหลดแล้วแต่ไม่ได้เปิด client — ลอง: launchctl kickstart -k " + service)


def main():
    if "--stop" in sys.argv:
        stop_client()
        return 0
    if "--start" in sys.argv:
        start()
        return 0

    print(f"═══ อัปเกรดสาขา {'(ตรวจอย่างเดียว)' if CHECK_ONLY else ''}\nโฟลเดอร์: {APP_DIR}")
    try:
        check_python()
        check_repo()
        clear_stale_compiled()
        stop_client()
        backup_data()
        install_packages()
        ensure_secrets()
        build_secure_env()
        ensure_db_user()
        ensure_identity()
        carry_over_services()
        ensure_launch_agent()
        if CHECK_ONLY:
            report_runtime()
        else:
            step("12. เปิด client")
            start()
    except Stop as e:
        print(f"  ❌ {e}\n\nหยุดที่ขั้นนี้ ขั้นต่อจากนี้ยังไม่ได้ทำ")
        if os.path.isdir(BACKUP_DIR):
            print(f"backup อยู่ที่ {BACKUP_DIR}")
        return 1
    print("\nเสร็จ" + ("" if CHECK_ONLY else f" | backup: {BACKUP_DIR}"))
    if not CHECK_ONLY:
        print("ต่อไป: ให้ admin ส่ง service ให้สาขานี้ แล้วรัน  bash tools/upgrade_branch.sh --check")
    return 0


if __name__ == "__main__":
    sys.exit(main())
