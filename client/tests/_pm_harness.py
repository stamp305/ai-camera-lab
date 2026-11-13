"""Seam-4 driver: runs client_logic's process manager in a process of its own.

test_process_manager.py starts `python -c "import _pm_harness; _pm_harness.run('<scenario>')"`
with HOME, the database and PYTHONPATH (tests/_pm_site first) set up, and reads the
`RESULT:` line this prints. Services run for real: multiprocessing forced to spawn,
dummy DLC folders loaded by client_logic's own loader under real uvicorn, ports
read from the port table. The only fakes: the store hours, the clocks, the socket
to the admin, and two stand-ins for data_service and the frontend (the real ones
need the whole store database), which listen on their port rows the same way.

The clocks: WORLD is the time client_logic is handed (`now` and `clock`). It moves
only when client_logic waits (its 5 s sleeps and RESTART_EVENT waits), by exactly
what it asked for, while the real wait is scaled by SPEED; a scenario may also jump it.
"""
import base64
import json
import os
import shutil
import signal
import threading
import time
import types
import urllib.request
from datetime import datetime, timedelta, time as dt_time

SPEED = 0.1
HOURS = (dt_time(8, 0), dt_time(20, 0))
DAY = datetime(2026, 10, 26)


def T(h, m=0, s=0, day=0):
    return DAY + timedelta(days=day, hours=h, minutes=m, seconds=s)


class World:
    def __init__(self):
        self.t = 0.0
        self.jumps = []  # (when, to, hook): on reaching `when`, call hook() and move to `to`

    def clock(self):
        return self.t

    def now(self):
        return DAY + timedelta(seconds=self.t)

    def at(self, when):
        self.t = (when - DAY).total_seconds()

    def advance(self, seconds):
        self.t += seconds
        if self.jumps and self.now() >= self.jumps[0][0]:
            _, to, hook = self.jumps.pop(0)
            hook()
            self.at(to)


WORLD = World()
SOCKET_STARTS = []  # WORLD.t of every start_socket_client() call


def _serve(name):
    import uvicorn
    from fastapi import FastAPI
    import port_store
    app = FastAPI()
    app.get("/")(lambda: {"service": name})
    uvicorn.run(app, host="127.0.0.1", port=port_store.get_port(name, 0), log_level="warning")


def run_data_service():
    _serve("data_service")


def run_frontend_service():
    _serve("frontend_web")


_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def answers(port, timeout=20.0, field="service"):
    """The service name (or another `field`) a port answers with, waiting up to `timeout`;
    None if it never does."""
    end = time.monotonic() + timeout
    while True:
        try:
            with _NO_PROXY.open(f"http://127.0.0.1:{port}/", timeout=1) as r:
                return json.loads(r.read())[field]
        except Exception:
            if time.monotonic() >= end:
                return None
            time.sleep(0.1)


class _FastEvent(threading.Event):
    def wait(self, timeout=None):
        hit = super().wait(None if timeout is None else timeout * SPEED)
        if not hit and timeout is not None:
            WORLD.advance(timeout)
        return hit


def _sleep(seconds):
    time.sleep(seconds * SPEED)
    WORLD.advance(seconds)


def _setup(activated):
    import multiprocessing
    multiprocessing.set_start_method("spawn", force=True)  # as bootstrap.py does
    import client_logic as cl

    fast_time = types.SimpleNamespace(**{k: getattr(time, k) for k in dir(time) if not k.startswith("__")})
    fast_time.sleep = _sleep
    fast_time.monotonic = WORLD.clock  # what the DLC handlers stamp a file's arrival with
    cl.time = fast_time
    cl.RESTART_EVENT = _FastEvent()
    cl.get_store_hours = lambda: HOURS
    cl.start_socket_client = lambda *a, **k: SOCKET_STARTS.append(WORLD.t)  # never dial the admin
    cl._socket_refused = lambda: False
    if activated:
        cl.is_activated = lambda: True

    for real, stand_in in ((cl.run_data_service, run_data_service),
                           (cl.run_frontend_service, run_frontend_service)):
        cl.ACTIVE_SERVICE[cl.ACTIVE_SERVICE.index(real)] = stand_in
    cl.FOLDER_TO_SERVICE_FUNC["data_service"] = run_data_service
    cl._discover_managed_services()  # what ensure_database() does at start
    return cl


def _alive(cl):
    """{service name: pid} of the processes the manager holds that are alive."""
    return {t.__name__: p.pid for t, p in cl.SERVICE_PROCESSES.items() if p.is_alive()}


def _proc(cl, name):
    return next((p for t, p in cl.SERVICE_PROCESSES.items() if t.__name__ == name), None)


def _ports():
    import port_store
    return port_store.get_all_ports()


def _passes(cl, n):
    for _ in range(n):
        cl._supervisor_pass(os.getcwd(), WORLD.now, WORLD.clock)


def _closed_pass(cl):
    cl._closed_hours_pass(*HOURS, WORLD.now, WORLD.clock)


def _ticks_until_alive(cl, names, limit=40, closed=False):
    """Open-hours ticks (or closed passes) until every name in `names` is alive."""
    for _ in range(limit):
        if set(names) <= set(_alive(cl)):
            return
        if closed:
            _closed_pass(cl)
        else:
            cl._open_hours_tick(os.getcwd(), WORLD.now, WORLD.clock)
    raise AssertionError(f"not all of {names} came up: {sorted(_alive(cl))}")


CORE = ("run_data_service", "run_frontend_service")


# ---------------------------------------------------------------------------
# Scenarios: each returns a dict of what it saw
# ---------------------------------------------------------------------------
def activation(cl):
    """Before activation: data_service then the frontend, nothing else. After: the DLC
    folders with an entry point, never the ones without."""
    ports = _ports()
    WORLD.at(T(21))
    out = {}
    for n in range(1, 5):
        _passes(cl, 1)
        out[f"pass{n}"] = sorted(_alive(cl))
    out["answers"] = {n: answers(ports[n], timeout=20 if n in ("data_service", "frontend_web") else 3)
                      for n in ("data_service", "frontend_web", "camera_service")}
    cl.is_activated = lambda: True
    _passes(cl, 6)
    out["activated"] = sorted(_alive(cl))
    out["ever_started"] = sorted({t.__name__ for t in cl.SERVICE_PROCESSES})
    out["camera_answers"] = answers(ports["camera_service"])
    return out


def day(cl):
    """One day from 07:00: start-up while closed, the restart 30 minutes before opening,
    opening, closing (camera_service's restart), the night."""
    ports = _ports()
    names = ("data_service", "frontend_web", "camera_service", "cus_emp_service", "heatmap_service")
    out = {}
    WORLD.at(T(7))
    _passes(cl, 12)  # the five starts take 5+5+5+30 s
    out["boot"] = _alive(cl)
    out["boot_answers"] = {n: answers(ports[n]) for n in names}
    WORLD.at(T(7, 10))
    _passes(cl, 1)  # the memory check notes who is running
    WORLD.at(T(7, 30, 30))
    _passes(cl, 1)
    out["at_0730"] = _alive(cl)
    _passes(cl, 1)
    out["after_0730"] = _alive(cl)
    out["after_0730_camera"] = answers(ports["camera_service"])
    snap = {}
    WORLD.at(T(7, 59, 50))
    WORLD.jumps = [(T(8, 0, 30), T(19, 59, 55), lambda: snap.update(open=_alive(cl), at=str(WORLD.now())))]
    _passes(cl, 3)  # 07:59:50 and 07:59:55 closed; the open pass runs 08:00:00 to closing
    out["open"] = snap["open"]
    out["open_at"] = snap["at"]
    out["closing_at"] = str(WORLD.now())
    out["at_closing"] = _alive(cl)
    # The memory check (every 60 s) does camera_service's closing restart.
    for _ in range(20):
        when = str(WORLD.now())
        _passes(cl, 1)
        if "run_camera_service" not in _alive(cl):
            out["camera_stopped"] = {"at": when, "alive": _alive(cl)}
            break
    _passes(cl, 1)
    out["night"] = _alive(cl)
    out["night_answers"] = {n: answers(ports[n]) for n in names}
    WORLD.at(T(23))
    _passes(cl, 2)
    out["late"] = _alive(cl)
    return out


def dead_service(cl):
    """A dead service comes back while open and while closed; a DLC restart round
    restarts the service it touched."""
    ports = _ports()
    out = {}
    WORLD.at(T(12))
    _ticks_until_alive(cl, CORE + ("run_heatmap_service",))
    out["started"] = sorted(_alive(cl))
    for when in ("open", "closed"):
        WORLD.t += 601
        old = _proc(cl, "run_heatmap_service")
        out[f"{when}_up"] = answers(ports["heatmap_service"])
        os.kill(old.pid, signal.SIGKILL)
        old.join(10)
        if when == "open":
            cl._open_hours_tick(os.getcwd(), WORLD.now, WORLD.clock)
        else:
            WORLD.at(T(21))
            cl._closed_hours_pass(*HOURS, WORLD.now, WORLD.clock)
        new = _proc(cl, "run_heatmap_service")
        out[when] = {"old_exitcode": old.exitcode, "same_pid": new.pid == old.pid,
                     "new_alive": new.is_alive(), "answers": answers(ports["heatmap_service"])}
    # A DLC delivered while open: the restart round stops heatmap, the next tick starts it.
    WORLD.at(T(13, day=1))
    before = _alive(cl)
    cl._UPDATED_SERVICE_FUNCS.add(cl.FOLDER_TO_SERVICE_FUNC["heatmap_service"])
    cl.RESTART_EVENT.set()
    _passes(cl, 1)
    out["dlc_round_alive"] = sorted(_alive(cl))
    cl._open_hours_tick(os.getcwd(), WORLD.now, WORLD.clock)
    after = _alive(cl)
    out["dlc"] = {"heatmap_restarted": after.get("run_heatmap_service") not in (None, before["run_heatmap_service"]),
                  "others_same": {k: v for k, v in after.items() if k != "run_heatmap_service"}
                  == {k: v for k, v in before.items() if k != "run_heatmap_service"},
                  "answers": answers(ports["heatmap_service"]),
                  "event_cleared": not cl.RESTART_EVENT.is_set()}
    out["alive"] = sorted(_alive(cl))
    return out


def together(cl):
    """Three services that die together come back one at a time."""
    WORLD.at(T(12))
    dlc = ("run_camera_service", "run_cus_emp_service", "run_heatmap_service")
    _ticks_until_alive(cl, CORE + dlc)
    WORLD.t += 601  # they have all run a good while (as before a daily exit)
    old = {n: _proc(cl, n) for n in dlc}
    for p in old.values():
        os.kill(p.pid, signal.SIGKILL)
    for p in old.values():
        p.join(10)
    killed_at = WORLD.t
    back = {}
    for _ in range(20):
        cl._open_hours_tick(os.getcwd(), WORLD.now, WORLD.clock)  # waits 5 s, then the watchdog
        for n in dlc:
            p = _proc(cl, n)
            if n not in back and p is not None and p.pid != old[n].pid:
                back[n] = WORLD.t - killed_at
        if len(back) == len(dlc):
            break
    ports = _ports()
    return {"back_after": back, "exitcodes": [p.exitcode for p in old.values()],
            "answers": {n: answers(ports[n]) for n in ("camera_service", "cus_emp_service", "heatmap_service")}}


def backoff(cl):
    """A folder that fails at import waits 5, 10, 20 ... 300 s; a run of over 10 minutes
    takes the next free slot and starts the wait over."""
    global SPEED
    SPEED = 0.01  # this scenario joins every child itself before moving on
    ports = _ports()
    flag = os.environ["PM_FAIL_FLAG"]
    open(flag, "w").close()
    WORLD.at(T(21))
    name = "run_flaky_service"
    starts, seen_dead, codes, healthy = [], [], [], None
    dead = False
    for _ in range(1000):
        if len(starts) == 12:
            break
        t = WORLD.t
        old = _proc(cl, name)
        cl._closed_hours_pass(*HOURS, WORLD.now, WORLD.clock)  # the watchdog runs at t
        if dead:
            seen_dead.append(t)
            dead = False
        p = _proc(cl, name)
        if p is None or p is old:
            continue
        starts.append(t)
        if len(starts) == 10:  # the flag went before this start: it runs
            healthy = answers(ports["flaky_service"])
            WORLD.t += 601
            open(flag, "w").close()
            os.kill(p.pid, signal.SIGKILL)
        p.join(30)
        codes.append(p.exitcode)
        dead = True
        if len(starts) == 9:
            os.remove(flag)
    return {"waits": [s - d for s, d in zip(starts[1:], seen_dead)],
            "exitcodes": codes, "healthy_answers": healthy}


def memory(cl):
    """camera_service over its memory limit is restarted; under it, left alone."""
    ports = _ports()
    WORLD.at(T(12))
    _ticks_until_alive(cl, CORE + ("run_camera_service",))
    out = {"first": _alive(cl)["run_camera_service"], "first_answers": answers(ports["camera_service"])}
    WORLD.t += 61
    cl._open_hours_tick(os.getcwd(), WORLD.now, WORLD.clock)  # a check under 12 GB
    out["under_limit"] = _alive(cl).get("run_camera_service")
    cl._MEMORY_LIMITS_MB["camera_service"] = 1  # any live python process is over 1 MB
    WORLD.t += 61
    cl._open_hours_tick(os.getcwd(), WORLD.now, WORLD.clock)
    out["over_limit"] = _alive(cl).get("run_camera_service")
    cl._MEMORY_LIMITS_MB["camera_service"] = 12288
    cl._open_hours_tick(os.getcwd(), WORLD.now, WORLD.clock)
    out["restarted"] = _alive(cl).get("run_camera_service")
    out["restarted_answers"] = answers(ports["camera_service"])
    return out


def loader_exit(cl):
    """The loader's exit code for each way a folder cannot serve."""
    import multiprocessing
    out = {}
    for folder in ("ghost_service", "broken_service", "noapp_service"):
        p = multiprocessing.Process(target=cl.ManagedServiceRunner(folder))
        p.start()
        p.join(60)
        out[folder] = p.exitcode
    return out


# --- A DLC at any hour (ticket 09) ----------------------------------------------------
DLC = ("run_camera_service", "run_heatmap_service")


def _deliver(cl, folder, files, complete=True):
    """What a Save of the Branch's Services sends for one folder: begin, its files, complete."""
    cl.on_service_folder_begin({"folder": folder})
    for rel, text in files.items():
        cl.on_add_file({"filename": f"{folder}/{rel}", "content": base64.b64encode(text.encode()).decode()})
    if complete:
        cl.on_dlc_update_complete(None)


def _flags(cl):
    return {"restart_event": cl.RESTART_EVENT.is_set(),
            "pending_services": sorted(t.__name__ for t in cl._UPDATED_SERVICE_FUNCS),
            "pending_folders": sorted(cl._UPDATED_FOLDERS),
            "manual_disconnect": cl._MANUAL_DISCONNECT.is_set(),
            "socket_dialled": len(SOCKET_STARTS)}


def _restarted(before, after):
    """Names whose process changed between two _alive() snapshots (all must be alive)."""
    assert set(after) == set(before), (before, after)
    return sorted(n for n in before if after[n] != before[n])


def _boot_closed(cl, names=CORE + DLC):
    WORLD.at(T(21))
    _ticks_until_alive(cl, names, closed=True)
    WORLD.t += 601  # a good while after start-up


def delivered(cl):
    """A delivered folder restarts its service in the next pass, closed or open; after the
    round the lists and flags are clear and the socket the DLC dropped is dialled again."""
    ports = _ports()
    _boot_closed(cl)
    out = {}
    for when in ("closed", "open"):
        if when == "open":
            WORLD.at(T(12, day=1))
        before = _alive(cl)
        cl._MANUAL_DISCONNECT.set()  # as _safe_socket_disconnect does on a connected socket
        SOCKET_STARTS.clear()
        _deliver(cl, "heatmap_service", {"VERSION": when + "\n"})
        if when == "closed":
            _closed_pass(cl)  # the round, then the watchdog starts it
        else:
            cl._open_hours_tick(os.getcwd(), WORLD.now, WORLD.clock)  # the round
            flags = _flags(cl)
            cl._open_hours_tick(os.getcwd(), WORLD.now, WORLD.clock)  # the watchdog
        out[when] = {"restarted": _restarted(before, _alive(cl)),
                     "flags": flags if when == "open" else _flags(cl),
                     "answers": answers(ports["heatmap_service"])}
    return out


def shared_folder(cl):
    """A delivered folder with no entry point restarts every DLC service, not data_service
    or the frontend."""
    ports = _ports()
    _boot_closed(cl)
    before = _alive(cl)
    SOCKET_STARTS.clear()
    _deliver(cl, "configs", {"helpers.py": "X = 2\n"})
    _closed_pass(cl)
    flags = _flags(cl)
    _ticks_until_alive(cl, CORE + DLC, closed=True)
    return {"restarted": _restarted(before, _alive(cl)), "flags": flags,
            "answers": {n: answers(ports[n]) for n in ("camera_service", "heatmap_service")}}


def stalled_transfer(cl):
    """Files without the complete event, while closed: applied 120 s (fake clock) after the
    last file."""
    _boot_closed(cl)
    before = _alive(cl)
    t0 = WORLD.t
    _deliver(cl, "heatmap_service", {"VERSION": "2\n"}, complete=False)
    for _ in range(40):
        t = WORLD.t
        _closed_pass(cl)
        if _alive(cl).get("run_heatmap_service") != before["run_heatmap_service"]:
            return {"applied_after": t - t0, "restarted": _restarted(before, _alive(cl)), "flags": _flags(cl)}
    return {"applied_after": None}


def device_key(cl):
    """A Device Key change restarts every running DLC service, which then holds the new key."""
    key = {"v": "key-one"}
    cl.device_identity.load = lambda: {"activateID": "CODE-1", "activated": True}
    cl.device_identity.device_key = lambda: key["v"]
    cl.load_config()  # at start, as main() does
    ports = _ports()
    _boot_closed(cl)
    out = {"keys_before": {n: answers(ports[n], field="key") for n in ("camera_service", "heatmap_service")}}
    before = _alive(cl)
    key["v"] = "key-two"
    cl.load_config()  # what the Device Key refresh does with a new key
    for _ in range(20):
        _closed_pass(cl)
        now = _alive(cl)
        if all(now.get(n) not in (None, before[n]) for n in DLC):
            break
    out["restarted"] = _restarted(before, _alive(cl))
    out["keys_after"] = {n: answers(ports[n], field="key") for n in ("camera_service", "heatmap_service")}
    out["flags"] = _flags(cl)
    return out


def delete_folder(cl):
    """Removing a service's folder stops it at once and for good; a folder gone from disk
    without the event is never started again either (the watchdog's own guard)."""
    import port_store
    _boot_closed(cl)
    heat, cam = _proc(cl, "run_heatmap_service"), _proc(cl, "run_camera_service")
    cl.on_delete_folder({"folder": "heatmap_service"})
    _closed_pass(cl)  # the next restart round stops it
    out = {"heatmap_alive": heat.is_alive(), "heatmap_exitcode": heat.exitcode,
           "heatmap_runner": [t.__name__ for t in cl.ACTIVE_SERVICE if t.__name__ == "run_heatmap_service"],
           "heatmap_port": "heatmap_service" in port_store.get_all_ports()}
    shutil.rmtree(os.path.join(cl.MANAGED_PATH, "camera_service"))
    os.kill(cam.pid, signal.SIGKILL)
    cam.join(10)
    pids = {"run_heatmap_service": set(), "run_camera_service": set()}
    for _ in range(70):  # 350 s, past the longest back-off
        _closed_pass(cl)
        for n in pids:
            p = _proc(cl, n)
            if p is not None:
                pids[n].add(p.pid)
    out["heatmap_pids_after"] = sorted(pids["run_heatmap_service"])
    out["camera_started_again"] = pids["run_camera_service"] != {cam.pid}
    out["camera_exitcode"] = cam.exitcode
    out["alive"] = sorted(_alive(cl))
    return out


def dlc_clears_backoff(cl):
    """A crash-looping folder (waiting 80 s for its next start) that receives a fix starts
    at once."""
    global SPEED
    SPEED = 0.01  # this scenario joins every child itself
    ports = _ports()
    flag = os.environ["PM_FAIL_FLAG"]
    open(flag, "w").close()
    WORLD.at(T(21))
    name = "run_flaky_service"
    starts = 0
    while starts < 5:  # deaths 1..4 waited 5, 10, 20, 40 s
        old = _proc(cl, name)
        _closed_pass(cl)
        p = _proc(cl, name)
        if p is not None and p is not old:
            starts += 1
            p.join(30)
    _closed_pass(cl)  # sees the fifth death: its next start waits 80 s
    dead = _proc(cl, name)
    os.remove(flag)
    t0 = WORLD.t
    _deliver(cl, "flaky_service", {"VERSION": "fixed\n"})
    for _ in range(40):
        t = WORLD.t
        _closed_pass(cl)
        if _proc(cl, name) is not dead:
            break
    out = {"started_after": t - t0, "dead_exitcode": dead.exitcode,
           "answers": answers(ports["flaky_service"])}
    # It dies young again: the wait starts over at 5 s, not at the 160 s it had reached.
    p = _proc(cl, name)
    os.kill(p.pid, signal.SIGKILL)
    p.join(10)
    killed = WORLD.t
    for _ in range(60):
        t = WORLD.t
        _closed_pass(cl)
        if _proc(cl, name) is not p:
            break
    out["next_wait"] = t - killed
    return out


def fair_slots(cl):
    """Among due services the one due longest gets the slot: heatmap (dead first) goes
    before flaky (crash-looping, earlier in start order) once cus_emp's 30 s are over."""
    open(os.environ["PM_FAIL_FLAG"], "w").close()
    WORLD.at(T(21))
    names = ("run_cus_emp_service", "run_flaky_service", "run_heatmap_service")
    pids = dict.fromkeys(names)
    flaky_starts, third_at, killed_at, order = 0, None, None, []
    for i in range(80):
        t = WORLD.t
        _closed_pass(cl)
        for n in names:
            p = _proc(cl, n)
            if p is not None and p.pid != pids[n]:
                pids[n] = p.pid
                if killed_at is not None:
                    order.append([n, t - killed_at])
                if n == "run_flaky_service":
                    flaky_starts += 1
                    third_at = i if flaky_starts == 3 else third_at
                    p.join(30)
        if third_at is not None and i == third_at + 1:  # the pass that saw flaky's third death
            for n in ("run_cus_emp_service", "run_heatmap_service"):
                p = _proc(cl, n)
                os.kill(p.pid, signal.SIGKILL)
                p.join(10)
            killed_at = WORLD.t
        if len(order) >= 3:
            break
    return {"order": order}


FOREIGN_WRITES = []  # (container, method) written from a thread other than the supervisor's


def _owned(base, methods, label):
    """`base` that notes every write made off the main (supervisor) thread."""
    def wrap(name):
        orig = getattr(base, name)

        def method(self, *a, **k):
            if threading.current_thread() is not threading.main_thread():
                FOREIGN_WRITES.append([label, name])
            return orig(self, *a, **k)
        return method
    return type("Owned" + base.__name__, (base,), {m: wrap(m) for m in methods})


_DICT_WRITES = ("__setitem__", "__delitem__", "pop", "popitem", "clear", "update", "setdefault")
_LIST_WRITES = ("__setitem__", "__delitem__", "append", "remove", "insert", "pop", "clear", "extend")


def socket_threads(cl):
    """Deliveries and a removal on another thread while supervisor passes run: no error,
    nothing but the supervisor changes the process table, the removed service stays gone."""
    global SPEED
    SPEED = 0.01
    for name in ("SERVICE_PROCESSES", "FOLDER_TO_SERVICE_FUNC", "_BACKOFF", "_DUE_AT"):
        setattr(cl, name, _owned(dict, _DICT_WRITES, name)(getattr(cl, name)))
    cl.ACTIVE_SERVICE = _owned(list, _LIST_WRITES, "ACTIVE_SERVICE")(cl.ACTIVE_SERVICE)
    _boot_closed(cl)
    heat = _proc(cl, "run_heatmap_service")
    state = {"deleted": False, "error": None}

    def socket_side():
        try:
            for i in range(20):
                _deliver(cl, "camera_service", {"VERSION": f"{i}\n"})
                if i == 8:
                    cl.on_delete_folder({"folder": "heatmap_service"})
                    state["deleted"] = True
                time.sleep(0.02)
        except Exception as e:  # pragma: no cover - reported to the test
            state["error"] = repr(e)

    th = threading.Thread(target=socket_side)
    th.start()
    heat_pids = set()
    while th.is_alive():
        _closed_pass(cl)
        p = _proc(cl, "run_heatmap_service")
        if state["deleted"] and p is not None:
            heat_pids.add(p.pid)
    th.join()
    for _ in range(70):  # 350 s
        _closed_pass(cl)
        p = _proc(cl, "run_heatmap_service")
        if p is not None:
            heat_pids.add(p.pid)
    return {"error": state["error"], "foreign_writes": FOREIGN_WRITES,
            "heatmap_other_pids": sorted(heat_pids - {heat.pid}), "heatmap_alive": heat.is_alive(),
            "alive": sorted(_alive(cl))}


def round_takes_under_lock(cl):
    """The round takes its work under the DLC lock: held by a socket thread, the round
    waits, and what that thread adds meanwhile is done in the same round."""
    _boot_closed(cl)
    before = _alive(cl)
    _deliver(cl, "heatmap_service", {"VERSION": "2\n"})
    with cl._UPDATED_FOLDERS_LOCK:
        th = threading.Thread(target=cl._restart_round, args=(WORLD.clock,))
        th.start()
        time.sleep(0.5)
        blocked = th.is_alive()
        cl._UPDATED_SERVICE_FUNCS.add(cl.FOLDER_TO_SERVICE_FUNC["camera_service"])
    th.join(30)
    _ticks_until_alive(cl, CORE + DLC, closed=True)
    return {"blocked": blocked, "restarted": _restarted(before, _alive(cl)), "flags": _flags(cl)}


ZONE_MAIN = 'from fastapi import FastAPI\n\napp = FastAPI()\napp.get("/")(lambda: {"service": "zone_service"})\n'


def transfer_survives_round(cl):
    """A Device Key round while a new service's files are arriving (no complete event yet)
    leaves that transfer pending: the stall fallback applies it 120 s after its last file."""
    key = {"v": "key-one"}
    cl.device_identity.load = lambda: {"activateID": "CODE-1", "activated": True}
    cl.device_identity.device_key = lambda: key["v"]
    cl.load_config()
    _boot_closed(cl, CORE + ("run_camera_service",))
    before = _alive(cl)
    t0 = WORLD.t
    _deliver(cl, "zone_service", {"main.py": ZONE_MAIN}, complete=False)
    key["v"] = "key-two"
    cl.load_config()
    _closed_pass(cl)  # the Device Key round
    out = {"key_round_restarted": _restarted(before, _alive(cl)) if set(_alive(cl)) == set(before) else None,
           "pending_after_key_round": sorted(cl._UPDATED_FOLDERS)}
    for _ in range(40):
        t = WORLD.t
        _closed_pass(cl)
        if "run_zone_service" in _alive(cl):
            out["started_after"] = t - t0
            break
    out["answers"] = answers(_ports().get("zone_service", 0)) if "started_after" in out else None
    return out


class _Exited(BaseException):
    pass


def restart_flags(cl):
    """.restart_required in each pass: updater.py is started with the mode the marker asks
    for, every service is stopped, the client exits 0."""
    import port_manager
    calls = []
    cl.subprocess = types.SimpleNamespace(Popen=lambda args, **kw: calls.append(args[1:]))
    real_exit = os._exit

    def fake_exit(code):
        raise _Exited(code)

    WORLD.at(T(21))
    passes = {"before_activation": lambda: cl._pre_activation_pass(os.getcwd(), WORLD.clock),
              "closed": lambda: cl._closed_hours_pass(*HOURS, WORLD.now, WORLD.clock),
              "open": lambda: cl._open_hours_tick(os.getcwd(), WORLD.now, WORLD.clock)}
    out = {}
    os._exit = fake_exit
    try:
        for name, fn in passes.items():
            for marker in ("PORT_CHANGE", "requested"):
                path = port_manager.request_restart(None, marker=marker)
                calls.clear()
                code = None
                try:
                    fn()
                except _Exited as e:
                    code = e.args[0]
                out[f"{name}/{marker}"] = {"popen": list(calls), "exit": code,
                                           "flag_left": os.path.exists(path), "alive": sorted(_alive(cl))}
    finally:
        os._exit = real_exit
    return out


SCENARIOS = {"activation": (activation, False),
             "day": (day, True),
             "dead_service": (dead_service, True),
             "together": (together, True),
             "backoff": (backoff, True),
             "memory": (memory, True),
             "loader_exit": (loader_exit, True),
             "restart_flags": (restart_flags, True),
             "delivered": (delivered, True),
             "shared_folder": (shared_folder, True),
             "stalled_transfer": (stalled_transfer, True),
             "device_key": (device_key, True),
             "delete_folder": (delete_folder, True),
             "dlc_clears_backoff": (dlc_clears_backoff, True),
             "fair_slots": (fair_slots, True),
             "socket_threads": (socket_threads, True),
             "round_takes_under_lock": (round_takes_under_lock, True),
             "transfer_survives_round": (transfer_survives_round, True)}


def run(name):
    fn, activated = SCENARIOS[name]
    cl = _setup(activated)
    try:
        result = fn(cl)
    finally:
        cl.stop_all_services()
    print("RESULT:" + json.dumps(result), flush=True)
