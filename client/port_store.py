"""Port registry backed by MariaDB (replaces client_port_config.py).

Single source of truth for which services this branch has and what port each
one listens on. Lives in the store's own database so an edit from the web UI
is the same write the app reads back on its next boot — there is no file to
regenerate and nothing to keep in sync.

Why a table and not the old module
----------------------------------
`client_port_config.py` shipped inside the bundle, so every update overwrote
it: a branch that had moved a port lost that change on the next release. Rows
here are only ever *added*, never rewritten by an update, so a port the shop
set stays set.

Rows appear on delivery, not on install
---------------------------------------
The table starts empty apart from the core services that are part of the app
itself. A service gets its row when the admin actually pushes its DLC folder
down (see `register_delivered` — called from client_logic when a folder
finishes downloading), which is what makes the port page list exactly the
services this branch owns rather than the whole catalogue.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from typing import Optional

import runtime_paths

logger = logging.getLogger(__name__)

# Services baked into the app itself — they exist on every branch from the
# first boot, so they are registered up-front. Everything else arrives by DLC.
CORE_SERVICES = {
    "frontend_web": 3000,
    "data_service": 8000,
    "activation_service": 8001,
}

# Ports we hand out when a DLC folder shows up, so a delivered service keeps
# the number the rest of the system already expects (admin push scripts, the
# CLAUDE.md port map, support docs). A folder not listed here gets the first
# free port from _FALLBACK_RANGE instead.
KNOWN_SERVICE_PORTS = {
    "camera_service": 8003,
    "in_pass_service": 8004,
    "cus_emp_service": 8005,
    "rate_service": 8006,
    "charge_service": 8007,
    "hotzone_service": 8008,
    "face_service": 8009,
    "log_service": 8010,
    "monitor_service": 8011,
    "recorder_service": 8012,
    "consumer_service": 8013,
    "analyzer_service": 8014,
    "report_service": 8015,
}

# Socket listeners — not HTTP services, but the port page edits them too.
SOCKET_PORTS = {
    "client_update": 65432,
    "update_control": 65433,
    "stream_frame_socket": 8050,
    "tracking_stream_socket": 8052,
}

# Right after the documented ports, so a new service reads as the next one in
# line (8016, 8017, ...). Numbers already in the table — the socket ports at
# 8050 and 8052 included — or held by another program are skipped (_next_free_port).
_FALLBACK_RANGE = range(max(KNOWN_SERVICE_PORTS.values()) + 1, 8200)

# Services bind to loopback only: the browser reaches everything through the
# frontend proxy, so raw services (no auth, no CORS) never face the shop LAN.
# This was configurable through the old module and nothing ever changed it.
API_HOST = "127.0.0.1"

TABLE_NAME = "service_ports"

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS service_ports (
    id         INT NOT NULL AUTO_INCREMENT,
    name       VARCHAR(64) NOT NULL,
    type       ENUM('api','socket') NOT NULL DEFAULT 'api',
    port       INT NOT NULL,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                                   ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uniq_service_ports_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------
def _load_env_if_needed() -> None:
    """Populate the DB credentials when the caller has not already done it.

    client_logic and the frontend both call load_secure_env() before they use
    this module, but bootstrap reads the frontend port before either of them
    exists — and a bare os.getenv() there falls back to 'root' with no
    password, which every MariaDB refuses. Same trick bootstrap._db_root_*
    uses: load the compiled env only when the values really are missing.
    """
    if os.getenv("DB_USER") and os.getenv("DB_NAME"):
        return
    # Only fill in what is missing. load_secure_env() assigns every key it has,
    # and bootstrap gets here before client_logic captures the operator's
    # overrides (SOCKET_SERVER_HOST & co.), so a test address given at launch
    # was replaced by the compiled-in one and the socket always went to production.
    saved = dict(os.environ)
    try:
        from data_service.configs.secure_env import load_secure_env
        load_secure_env()
    except Exception as e:
        logger.warning(f"[PORTS] Could not load the secure env: {e}")
    finally:
        os.environ.update(saved)


def _db_settings() -> dict:
    """Credentials for the store's own database, from the process environment."""
    _load_env_if_needed()
    return {
        "user": os.getenv("DB_USER") or "root",
        "password": os.getenv("DB_PASSWORD") or "",
        "host": os.getenv("DB_HOST") or "localhost",
        "port": int(os.getenv("DB_PORT") or 3306),
        "database": os.getenv("DB_NAME") or "ai_camera",
    }


def _connect():
    """Open a connection, preferring the local socket like the rest of the app."""
    try:
        import mariadb  # provided by the bundled python_env
    except ImportError:  # git-clone installs use pymysql under the same name
        import pymysql
        pymysql.install_as_MySQLdb()
        mariadb = pymysql

    cfg = _db_settings()
    kwargs = {
        "user": cfg["user"],
        "password": cfg["password"],
        "database": cfg["database"],
    }
    if cfg["host"] in ("localhost", "127.0.0.1") and os.path.exists("/tmp/mysql.sock"):
        kwargs["unix_socket"] = "/tmp/mysql.sock"
    else:
        kwargs["host"] = cfg["host"]
        kwargs["port"] = cfg["port"]
    return mariadb.connect(**kwargs)


def _rows(sql: str, params: tuple = ()) -> list:
    conn = None
    try:
        conn = _connect()
        cur = conn.cursor()
        # Timestamps are the store's wall clock, which is what the port page
        # shows — MariaDB otherwise stamps rows in whatever zone the server
        # session defaults to.
        cur.execute("SET time_zone = 'SYSTEM'")
        cur.execute(sql, params)
        return list(cur.fetchall() or [])
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _execute(sql: str, params: tuple = ()) -> int:
    conn = None
    try:
        conn = _connect()
        cur = conn.cursor()
        cur.execute("SET time_zone = 'SYSTEM'")
        cur.execute(sql, params)
        conn.commit()
        return cur.rowcount
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Schema + registration
# ---------------------------------------------------------------------------
MANAGED_DIR = os.path.join(
    os.path.expanduser("~/Library/Application Support/AICamera"), "managed_services"
)


def ensure_schema(runnable: Optional[set] = None) -> None:
    """Create the table if missing and register what this branch already owns.

    Two sources, both add-only:
      - the core services, which are part of the app on every machine
      - every service folder already sitting in managed_services

    The second one is what carries an existing branch across this change. Those
    folders were delivered before there was a table to record them, and the
    delivery hook only fires on the *next* push — so without this a shop that
    has had 13 services for months would open the port page and see three.
    A folder on disk is the same evidence a delivery is: the admin put it there.

    ``runnable`` is the set of folders this build actually knows how to start
    (client_logic passes FOLDER_TO_SERVICE_FUNC). A delivered folder the app
    has no runner for is not listed at all — not even by name — because a port
    the client can never open is not a setting anyone can act on. The caller
    owns that list: only client_logic knows which services it can launch, and
    it cannot be imported from here without a cycle.
    """
    _execute(CREATE_TABLE_SQL)
    for name, port in CORE_SERVICES.items():
        register(name, port, "api")
    # Socket listeners belong to the app itself, not to any delivered service,
    # so they are always present — and data_service reads them at import time.
    for name, port in SOCKET_PORTS.items():
        register(name, port, "socket")

    try:
        existing = sorted(os.listdir(MANAGED_DIR))
    except OSError:
        return
    delivered = set()
    on_disk = set()
    for folder in existing:
        # managed_services also holds shared payloads like configs/ that are
        # not services and never listen on anything.
        if folder.endswith("_service") and os.path.isdir(os.path.join(MANAGED_DIR, folder)):
            on_disk.add(folder)
            if runnable is not None and folder not in runnable:
                # Files arrived, but this build has no code to start them. The
                # branch owns the payload without owning a running service, so
                # it gets no row: the port page lists what the client can
                # actually run, not what happens to be on disk.
                continue
            delivered.add(folder)
            register_delivered(folder, runnable)

    # ...and drop rows whose folder is gone, or whose service this build can no
    # longer run. The delete_folder event only reaches a branch that is online:
    # a service removed while the shop was switched off would otherwise keep
    # its row forever, listing a service the machine does not have. The folders
    # plus the runner map are the truth; this makes the table agree on every
    # start, including for rows written by an older build that listed
    # everything delivered.
    for name in get_all_ports():
        if name in CORE_SERVICES or name in SOCKET_PORTS:
            continue
        if name not in delivered:
            if name in on_disk:
                unregister(name, "this build has no runner for it")
            else:
                unregister(name)


def register(name: str, port: int, type_: str = "api") -> bool:
    """Add a service if it is new. Never touches a row that already exists.

    That is the whole point of the add-only rule: an update ships new service
    names, and the branch may have moved the ports of the ones it already had.
    Returns True when a row was actually inserted.
    """
    if not name:
        return False
    try:
        changed = _execute(
            "INSERT IGNORE INTO service_ports (name, type, port) VALUES (%s, %s, %s)",
            (name, type_, int(port)),
        )
    except Exception as e:
        logger.error(f"[PORTS] Could not register '{name}': {e}")
        return False
    if changed:
        logger.info(f"[PORTS] Registered {type_} service '{name}' on port {port}.")
    return bool(changed)


def unregister(name: str, reason: str = "its DLC folder was removed") -> bool:
    """Drop a service's row — this branch no longer runs that service.

    ``reason`` is logged as-is because there are two of them and they mean
    different things to whoever reads the log later: the folder is gone, or
    the folder is still there but this build has no runner for it. Saying
    "folder was removed" for the second case sends the reader hunting for a
    deletion that never happened.

    The counterpart to register_delivered: when a folder is deleted the branch
    no longer owns the service, so leaving the row behind would keep it in the
    port page forever, permanently offline, with an editable port for
    something that cannot run. Add it back later and it starts from the
    documented port again, which is the honest answer — the shop's old number
    belonged to a service it no longer had.
    """
    if not name:
        return False
    if name in CORE_SERVICES:
        # These are the app itself, not something the admin delivers. A stray
        # or mistyped folder name in a delete_folder event would otherwise
        # take out the frontend or the data service, and ensure_schema would
        # put it back at the default port on the next boot — quietly throwing
        # away a port the shop had changed.
        logger.warning(f"[PORTS] Refusing to unregister core service '{name}'.")
        return False
    try:
        removed = _execute("DELETE FROM service_ports WHERE name = %s AND type = 'api'", (name,))
    except Exception as e:
        logger.error(f"[PORTS] Could not unregister '{name}': {e}")
        return False
    if removed:
        logger.info(f"[PORTS] Unregistered '{name}' ({reason}).")
    return bool(removed)


def _next_free_port() -> int:
    """First port in the fallback range that no row and no process holds.

    The table alone is not enough: it only knows about services this app runs,
    so a number free in the table can still belong to Docker, a dev server or
    another copy of us. Handing that one out produces a service that never
    starts and a port page showing a conflict the shop has to fix by hand, so
    the candidate is checked against the machine as well.

    A second pass ignores the listener check: if every free number in the range
    is busy, a row with a clash the port page can show is still better than
    refusing to register the service at all — then it has no row, no name on
    the page, and nothing explaining why it never runs.
    """
    taken = {int(p) for _, p in get_all_ports().items()}
    free = [c for c in _FALLBACK_RANGE if c not in taken]
    for candidate in free:
        if _listener(candidate) is None:
            return candidate
    if free:
        logger.warning(
            f"[PORTS] Every free port in {_FALLBACK_RANGE.start}-{_FALLBACK_RANGE.stop - 1} "
            f"is in use by something; assigning {free[0]} anyway."
        )
        return free[0]
    raise RuntimeError("no free port left in the fallback range")


def register_delivered(folder: str, runnable: Optional[set] = None) -> bool:
    """Register a service whose DLC folder just landed on this machine.

    Called when a folder finishes downloading, which is the moment the branch
    genuinely owns that service. A folder we have a documented port for keeps
    that number; anything else is parked on the first free fallback port and
    can be moved from the web UI.

    ``runnable`` filters out payloads this build cannot start: delivering the
    files does not give the client a way to run them, and a row for such a
    service would offer a port that never opens.
    """
    if not folder or "/" in folder:
        return False
    if runnable is not None and folder not in runnable:
        logger.info(f"[PORTS] '{folder}' delivered but this build has no runner — not listing it.")
        return False
    port = KNOWN_SERVICE_PORTS.get(folder)
    if port is None:
        if folder in get_all_ports():
            return False
        try:
            port = _next_free_port()
        except Exception as e:
            logger.error(f"[PORTS] No port available for '{folder}': {e}")
            return False
        logger.info(f"[PORTS] '{folder}' has no documented port; assigning {port}.")
    return register(folder, port, "api")


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
def get_all_ports() -> dict:
    """{name: port} for every registered service, api and socket alike."""
    try:
        return {str(r[0]): int(r[1]) for r in _rows("SELECT name, port FROM service_ports")}
    except Exception as e:
        logger.error(f"[PORTS] Could not read the port table: {e}")
        return {}


def get_config() -> dict:
    """The port map split by type, shaped for the web UI."""
    api, socket_ = {}, {}
    try:
        for name, type_, port, updated in _rows(
            "SELECT name, type, port, updated_at FROM service_ports ORDER BY type, name"
        ):
            target = socket_ if str(type_) == "socket" else api
            target[str(name)] = {"port": int(port), "updated_at": str(updated)}
    except Exception as e:
        logger.error(f"[PORTS] Could not read the port table: {e}")
    return {"API_HOST": API_HOST, "API_PORTS": api, "SOCKET_PORTS": socket_}


def port_map() -> dict:
    """{name: "port"} for code that wants the whole map at once.

    Documented defaults first, database rows on top, so a lookup for a service
    that has no row yet still answers with the number the rest of the system
    expects instead of raising KeyError — the old module was a plain dict and
    its callers index it directly.
    """
    merged = {}
    merged.update(CORE_SERVICES)
    merged.update(KNOWN_SERVICE_PORTS)
    merged.update(SOCKET_PORTS)
    merged.update(get_all_ports())
    return {name: str(port) for name, port in merged.items()}


def get_port(name: str, default: int) -> int:
    """Port for one service, falling back to `default` if it has no row yet.

    Every caller passes the historical default, so a database that is briefly
    unreachable degrades to the old behaviour instead of failing to start.
    """
    try:
        rows = _rows("SELECT port FROM service_ports WHERE name = %s", (name,))
        if rows:
            return int(rows[0][0])
    except Exception as e:
        logger.error(f"[PORTS] Could not read port for '{name}': {e}")
    return int(default)


def set_port(name: str, port: int) -> bool:
    """Change a registered service's port (the web UI's save button)."""
    try:
        return bool(_execute(
            "UPDATE service_ports SET port = %s WHERE name = %s", (int(port), name)
        ))
    except Exception as e:
        logger.error(f"[PORTS] Could not update '{name}': {e}")
        return False


# ---------------------------------------------------------------------------
# Live status
# ---------------------------------------------------------------------------
_LSOF_LINE = re.compile(r"^(?P<cmd>\S+)\s+(?P<pid>\d+)\s")


def _listener(port: int):
    """(pid, command) of whoever is listening on `port`, or None."""
    try:
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{int(port)}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception as e:
        logger.warning(f"[PORTS] lsof failed for port {port}: {e}")
        return None
    for line in out.splitlines()[1:]:
        m = _LSOF_LINE.match(line)
        if m:
            return int(m.group("pid")), m.group("cmd")
    return None


_APP_ROOT = os.path.dirname(os.path.abspath(__file__))


def _is_ours(pid: int) -> bool:
    """Whether `pid` belongs to this installation.

    Two tests, because neither alone is enough. The process group catches the
    services client_logic spawns; the executable path catches everything else,
    including a caller that is not in the app's group at all — a one-off
    script, or the frontend when launched separately, both of which reported
    our own healthy services as a conflict when the group was the only test.
    """
    try:
        out = subprocess.run(
            ["ps", "-o", "pgid=,command=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except Exception:
        return False
    if not out:
        return False
    pgid, _, command = out.partition(" ")
    try:
        if int(pgid) == os.getpgrp():
            return True
    except ValueError:
        pass
    return _APP_ROOT in command


# client.log is sealed for the server (log_seal.py): this Mac can no longer
# read it back. The few lines the port page needs are copied here as they are
# logged, by ServiceEventTap — in this user's private run folder, not in App
# Support, and started over by each run of the supervisor.
EVENTS_FILE = os.path.join(runtime_paths.RUN_DIR, "service_events")
_EVENTS_MAX_BYTES = 256 * 1024
# Checked only for being there (tools/upgrade_branch.py, tools/store_check.sh),
# so the marker is kept, not the message, which names files and cameras.
EVENT_FLAGS = ("Decryption failed", "not set; cannot decrypt", "Admin refused a Device Key",
               "ปิด detect pool", "detect worker ล้มเหลว")

_RUN_START = "[CLIENT] main() started"
_RE_ATTEMPT = re.compile(r"Started run_(\w+) \(PID")
_RE_UP = re.compile(r"Starting Uvicorn server for (\w+) on port")
_RE_FRONTEND_UP = re.compile(r"Starting Python frontend server on")
_RE_FAIL = re.compile(r"Runtime error importing (\w+) main: (.+?)\s*$")
_RE_SKIPPED = re.compile(r"(\w+) skipped — (main file not yet available|file not found)")
_RE_DEAD = re.compile(r"run_(\w+) is dead \(exit code")
_RE_RESTART = re.compile(r"Stopped run_(\w+) \(PID: \S+\); the watchdog starts it again")
_OUTCOME_RES = (_RE_ATTEMPT, _RE_UP, _RE_FRONTEND_UP, _RE_FAIL, _RE_SKIPPED,
                _RE_DEAD, _RE_RESTART)


class ServiceEventTap(logging.Handler):
    """Copies the log lines _log_outcomes needs into EVENTS_FILE. On the root logger of every process."""

    def __init__(self):
        super().__init__()
        self._flagged = set()

    def emit(self, record):
        try:
            message = record.getMessage()
            first = message.split("\n", 1)[0]
            if _RUN_START in first:
                self._write(first, restart=True)
            elif any(rx.search(first) for rx in _OUTCOME_RES):
                self._write(first[:500])
            else:
                for flag in EVENT_FLAGS:
                    # Once per process: a failing worker logs the same line thousands of times.
                    if flag in message and flag not in self._flagged:
                        self._flagged.add(flag)
                        self._write(flag)
                        break
        except Exception:
            pass

    @staticmethod
    def _write(line, restart=False):
        if not restart:
            try:
                if os.path.getsize(EVENTS_FILE) > _EVENTS_MAX_BYTES:
                    return
            except OSError:
                pass
        runtime_paths.ensure_run_dir()
        flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if restart else os.O_APPEND)
        fd = os.open(EVENTS_FILE, flags, 0o600)
        try:
            os.write(fd, (line + "\n").encode("utf-8", "replace"))
        finally:
            os.close(fd)


def _log_outcomes() -> dict:
    """What the supervisor did with each service during the current run.

    A port being closed says nothing about *why*. The supervisor logs the
    answer as it starts each service, and ServiceEventTap keeps a copy of
    those lines: attempted, came up, crashed on import (with the error), or
    was never touched at all. Only the current run counts — a crash from three
    restarts ago is history, not a diagnosis.

    Lines are read in order, so `phase` is the latest thing that happened:
    starting (process launched or uvicorn coming up) or restarting (died, or
    stopped on purpose, and the watchdog will start it again). An import
    error or a missing main file stays until the service comes up, so a
    crash loop reads as Failed and not as Restarting.
    """
    outcomes: dict = {}

    def note(name, **fields):
        outcomes.setdefault(name, {}).update(fields)

    try:
        with open(EVENTS_FILE, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except OSError:
        return outcomes

    # Rewind to the last supervisor start so stale runs cannot be read as now.
    start = 0
    for i in range(len(lines) - 1, -1, -1):
        if _RUN_START in lines[i]:
            start = i
            break

    for line in lines[start:]:
        m = _RE_ATTEMPT.search(line)
        if m:
            note(m.group(1), phase="starting")
            continue
        m = _RE_UP.search(line)
        if m:
            note(m.group(1), phase="starting", error=None)
            continue
        if _RE_FRONTEND_UP.search(line):
            note("frontend_web", phase="starting", error=None)
            continue
        m = _RE_FAIL.search(line)
        if m:
            note(m.group(1), error=m.group(2).strip())
            continue
        m = _RE_DEAD.search(line) or _RE_RESTART.search(line)
        if m:
            note(m.group(1), phase="restarting")
            continue
        m = _RE_SKIPPED.search(line)
        if m:
            note(m.group(1), error=m.group(2))
    return outcomes


def diagnose(name: str, port: int, outcomes: Optional[dict] = None) -> dict:
    """Why a service is in the state it is in.

    `state` stays machine-readable; the page turns it into a label. The
    reasons are deliberately distinct because they call for different actions:
    a crashed service is a bug to fix, one still waiting is just the start-up
    queue (the watchdog starts one service every few seconds), and a port
    held by a stranger is something to close.
    """
    if outcomes is None:
        outcomes = _log_outcomes()
    seen = outcomes.get(name, {})
    status = port_status(port)

    # `status` is spread first in every branch on purpose: it carries pid and
    # process, but its own coarse "state" (online/offline/conflict) must not
    # overwrite the finer answer decided here — that bug made every diagnosis
    # read "offline", including services whose crash reason was right there.
    if status["state"] == "online":
        return {**status, "state": "running", "detail": None}
    if status["state"] == "conflict":
        return {**status, "state": "port_taken", "detail": status.get("process")}
    if seen.get("error"):
        return {**status, "state": "crashed", "detail": seen["error"]}
    phase = seen.get("phase")
    if phase in ("starting", "restarting"):
        return {**status, "state": phase, "detail": None}
    return {**status, "state": "waiting", "detail": None}


def port_status(port: int) -> dict:
    """Who holds `port` right now.

    state is one of:
      online   — one of our own processes is listening (the service works)
      conflict — something else holds it (names the program, so the shop can
                 see whether it is Zoom, a dev server, or a second copy of us)
      offline  — nobody is listening
    """
    holder = _listener(port)
    if holder is None:
        return {"state": "offline", "in_use": False, "pid": None, "process": None}
    pid, command = holder
    if _is_ours(pid):
        return {"state": "online", "in_use": True, "pid": pid, "process": command}
    return {"state": "conflict", "in_use": True, "pid": pid, "process": command}
