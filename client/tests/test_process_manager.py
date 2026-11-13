"""Seam 4: the Main Client's process manager.

Each test runs tests/_pm_harness.py in a fresh process: real multiprocessing
forced to spawn, HOME in a temp folder (client_logic computes the managed and
app-support paths from it at import), the secure_env stub on the path for every
spawned child (tests/_pm_site/sitecustomize.py), port rows in a throwaway
database on the local MariaDB (created and dropped here), and dummy service
folders that client_logic's own loader runs under real uvicorn. Time is the
harness's fake clock, which moves as client_logic waits (see _pm_harness.py).
"""
import getpass
import json
import os
import signal
import socket
import subprocess
import sys
import uuid

import pytest

TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS)
SOCK = "/tmp/mysql.sock"

DUMMY_MAIN = """import os

from fastapi import FastAPI

app = FastAPI()


@app.get("/")
def root():
    return {"service": %r, "key": os.environ.get("AI_CAMERA_DEVICE_KEY")}
"""
FLAKY_MAIN = """import os

if os.path.exists(os.environ["PM_FAIL_FLAG"]):
    raise RuntimeError("told to fail")
""" + DUMMY_MAIN

# folder -> its main.py (None: a folder with no entry point)
FOLDERS = {
    "camera_service": DUMMY_MAIN % "camera_service",
    "cus_emp_service": DUMMY_MAIN % "cus_emp_service",
    "heatmap_service": DUMMY_MAIN % "heatmap_service",
    "flaky_service": FLAKY_MAIN % "flaky_service",
    "broken_service": "raise RuntimeError('broken at import')\n",
    "noapp_service": "x = 1\n",
    "configs": None,
    "ghost_service": None,
}


def _db():
    pymysql = pytest.importorskip("pymysql")
    try:
        return pymysql.connect(user=getpass.getuser(), password="", unix_socket=SOCK, autocommit=True)
    except Exception as e:
        pytest.skip(f"no local MariaDB for seam 4: {e}")


def _free_ports(n):
    socks = [socket.socket() for _ in range(n)]
    for s in socks:
        s.bind(("127.0.0.1", 0))
    ports = [s.getsockname()[1] for s in socks]
    for s in socks:
        s.close()
    return ports


@pytest.fixture
def branch(tmp_path, request):
    """A temp HOME with the delivered folders a test names (default camera_service and
    heatmap_service), and port rows for them and the core services."""
    import port_store
    folders = getattr(request, "param", ("camera_service", "heatmap_service"))
    managed = tmp_path / "home" / "Library" / "Application Support" / "AICamera" / "managed_services"
    for folder in folders:
        (managed / folder).mkdir(parents=True)
        main = FOLDERS[folder]
        (managed / folder / ("main.py" if main is not None else "helpers.py")).write_text(main or "X = 1\n")
    names = ["data_service", "frontend_web", "activation_service"] + [f for f in folders if FOLDERS[f]]
    ports = dict(zip(names, _free_ports(len(names))))

    db_name = f"ai_camera_pm_test_{uuid.uuid4().hex[:12]}"
    conn = _db()
    cur = conn.cursor()
    cur.execute(f"CREATE DATABASE `{db_name}`")
    try:
        cur.execute(f"USE `{db_name}`")
        cur.execute(port_store.CREATE_TABLE_SQL)
        for name, port in ports.items():
            cur.execute("INSERT INTO service_ports (name, type, port) VALUES (%s, 'api', %s)", (name, port))
        (tmp_path / "run").mkdir()
        env = dict(os.environ,
                   HOME=str(tmp_path / "home"),
                   PYTHONPATH=os.pathsep.join([os.path.join(TESTS, "_pm_site"), TESTS, ROOT]),
                   PYTHONDONTWRITEBYTECODE="1",
                   PM_TEST_RUN_DIR=str(tmp_path / "run"),
                   PM_FAIL_FLAG=str(tmp_path / "fail"),
                   AICAMERA_KEYCHAIN_SERVICE="aicamera-test-" + uuid.uuid4().hex,
                   DB_USER=getpass.getuser(), DB_PASSWORD="", DB_HOST="localhost", DB_NAME=db_name)
        env.pop("DB_PORT", None)
        env.pop("CAMERA_SERVICE_RESTART_HOURS", None)
        env.pop("CAMERA_SERVICE_MAX_MB", None)
        yield env, ports
    finally:
        cur.execute(f"DROP DATABASE IF EXISTS `{db_name}`")
        conn.close()


def folders(*names):
    return pytest.mark.parametrize("branch", [names], indirect=True, ids=["+".join(names)])


def run(env, scenario):
    proc = subprocess.Popen(
        [sys.executable, "-c", f"import _pm_harness; _pm_harness.run({scenario!r})"],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        start_new_session=True)
    try:
        out, _ = proc.communicate(timeout=240)
    finally:
        try:  # anything the driver left behind (it stops its services itself)
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    lines = [l for l in out.splitlines() if l.startswith("RESULT:")]
    assert proc.returncode == 0 and lines, out[-4000:]
    return json.loads(lines[-1][len("RESULT:"):])


CORE = ["run_data_service", "run_frontend_service"]


@folders("camera_service", "configs", "ghost_service")
def test_before_activation_only_data_service_then_frontend_and_never_a_folder_without_entry_point(branch):
    env, _ = branch
    r = run(env, "activation")
    assert r["pass1"] == ["run_data_service"]  # data_service first, one start per slot
    assert r["pass2"] == r["pass3"] == r["pass4"] == CORE
    assert r["answers"] == {"data_service": "data_service", "frontend_web": "frontend_web",
                            "camera_service": None}
    assert r["activated"] == ["run_camera_service"] + CORE
    assert r["ever_started"] == ["run_camera_service"] + CORE  # configs, ghost_service never
    assert r["camera_answers"] == "camera_service"


@folders("camera_service", "cus_emp_service", "heatmap_service")
def test_nothing_stops_or_starts_at_opening_or_closing_except_camera_services_two_daily_restarts(branch):
    env, _ = branch
    r = run(env, "day")
    dlc = ["run_camera_service", "run_cus_emp_service", "run_heatmap_service"]
    # Started while closed (07:00), all five.
    assert sorted(r["boot"]) == sorted(CORE + dlc)
    assert all(k == v for k, v in r["boot_answers"].items())
    others = {k: v for k, v in r["boot"].items() if k != "run_camera_service"}

    def without_camera(alive):
        return {k: v for k, v in alive.items() if k != "run_camera_service"}

    # 30 minutes before opening: camera_service stopped, then started again; nobody else.
    assert "run_camera_service" not in r["at_0730"] and r["at_0730"] == others
    assert r["after_0730"]["run_camera_service"] != r["boot"]["run_camera_service"]
    assert without_camera(r["after_0730"]) == others
    assert r["after_0730_camera"] == "camera_service"
    # Opening started nothing and stopped nothing.
    assert r["open_at"].endswith("08:00:30")
    assert r["open"] == r["after_0730"]
    # Closing stops nothing; within the minute of the memory check camera_service gets its
    # daily restart, and is back the next pass.
    assert r["closing_at"].endswith("20:00:05")
    assert r["at_closing"] == r["after_0730"]
    assert "2026-10-26 20:00:05" <= r["camera_stopped"]["at"] <= "2026-10-26 20:01:05"
    assert r["camera_stopped"]["alive"] == others
    assert r["night"]["run_camera_service"] not in (r["boot"]["run_camera_service"],
                                                    r["after_0730"]["run_camera_service"])
    assert without_camera(r["night"]) == others
    assert all(k == v for k, v in r["night_answers"].items())
    # No more restarts later that night.
    assert r["late"] == r["night"]


@folders("heatmap_service")
def test_dead_service_is_restarted_while_open_and_while_closed_and_a_dlc_round_restarts_it(branch):
    env, _ = branch
    r = run(env, "dead_service")
    assert r["started"] == CORE + ["run_heatmap_service"]
    for when in ("open", "closed"):
        assert r[f"{when}_up"] == "heatmap_service"
        assert r[when] == {"old_exitcode": -signal.SIGKILL, "same_pid": False,
                           "new_alive": True, "answers": "heatmap_service"}
    assert "run_heatmap_service" not in r["dlc_round_alive"]
    assert r["dlc"] == {"heatmap_restarted": True, "others_same": True,
                        "answers": "heatmap_service", "event_cleared": True}
    assert r["alive"] == CORE + ["run_heatmap_service"]


@folders("camera_service", "cus_emp_service", "heatmap_service")
def test_services_that_die_together_come_back_one_at_a_time_5s_apart_and_30s_after_cus_emp(branch):
    env, _ = branch
    r = run(env, "together")
    assert r["exitcodes"] == [-signal.SIGKILL] * 3
    back = r["back_after"]
    # First at the next tick (they had run over 10 minutes), then one slot each.
    assert back["run_camera_service"] == 5
    assert back["run_cus_emp_service"] - back["run_camera_service"] == 5
    assert back["run_heatmap_service"] - back["run_cus_emp_service"] == 30
    assert all(k == v for k, v in r["answers"].items())


@folders("flaky_service")
def test_failing_folder_backs_off_5_to_300s_and_a_long_run_resets_it(branch):
    env, _ = branch
    r = run(env, "backoff")
    # 9 failed starts, then a good one (flag removed), killed after 601 s: back at once,
    # fails again (flag back), and waits 5 s again.
    assert r["waits"] == [5, 10, 20, 40, 80, 160, 300, 300, 300, 0, 5]
    assert r["exitcodes"] == [1] * 9 + [-signal.SIGKILL, 1, 1]
    assert r["healthy_answers"] == "flaky_service"


@folders("camera_service")
def test_camera_service_is_restarted_over_its_memory_limit(branch):
    env, _ = branch
    r = run(env, "memory")
    assert r["first_answers"] == "camera_service"
    assert r["under_limit"] == r["first"]
    assert r["over_limit"] is None  # stopped by the check
    assert r["restarted"] not in (None, r["first"])
    assert r["restarted_answers"] == "camera_service"


@folders("ghost_service", "broken_service", "noapp_service")
def test_loader_exits_non_zero_for_each_way_a_folder_cannot_serve(branch):
    env, _ = branch
    r = run(env, "loader_exit")
    assert r == {"ghost_service": 1, "broken_service": 1, "noapp_service": 1}


@folders("camera_service")
def test_restart_required_and_port_change_still_restart(branch):
    env, _ = branch
    r = run(env, "restart_flags")
    expect = {"before_activation/PORT_CHANGE": "--port-change-restart",
              "before_activation/requested": "--restart-only",
              # closed hours never told the two apart, and still does not
              "closed/PORT_CHANGE": "--restart-only",
              "closed/requested": "--restart-only",
              "open/PORT_CHANGE": "--port-change-restart",
              "open/requested": "--restart-only"}
    for case, mode in expect.items():
        assert r[case] == {"popen": [["updater.py", mode]], "exit": 0, "flag_left": False, "alive": []}, case


# --- A DLC takes effect at any hour (ticket 09) -----------------------------------------
DLC = ["run_camera_service", "run_heatmap_service"]
ROUND_DONE = {"restart_event": False, "pending_services": [], "pending_folders": [],
              "manual_disconnect": False, "socket_dialled": 1}


@folders("camera_service", "heatmap_service")
def test_a_delivered_folder_restarts_its_service_at_once_closed_or_open_and_the_branch_reconnects(branch):
    env, _ = branch
    r = run(env, "delivered")
    for when in ("closed", "open"):
        assert r[when]["restarted"] == ["run_heatmap_service"], when
        assert r[when]["answers"] == "heatmap_service", when
        # The lists and both flags are clear and the socket the DLC dropped is dialled again.
        assert r[when]["flags"] == ROUND_DONE, when


@folders("camera_service", "heatmap_service", "configs")
def test_a_shared_folder_restarts_every_dlc_service_and_never_data_service_or_frontend(branch):
    env, _ = branch
    r = run(env, "shared_folder")
    assert r["restarted"] == DLC
    assert r["answers"] == {"camera_service": "camera_service", "heatmap_service": "heatmap_service"}
    assert r["flags"] == ROUND_DONE


@folders("camera_service", "heatmap_service")
def test_a_stalled_transfer_is_applied_while_closed_120s_after_its_last_file(branch):
    env, _ = branch
    r = run(env, "stalled_transfer")
    assert r["applied_after"] == 120
    assert r["restarted"] == ["run_heatmap_service"]
    assert r["flags"] == ROUND_DONE


@folders("camera_service", "heatmap_service")
def test_a_device_key_change_restarts_every_dlc_service_with_the_new_key(branch):
    env, _ = branch
    r = run(env, "device_key")
    assert r["keys_before"] == {"camera_service": "key-one", "heatmap_service": "key-one"}
    assert r["restarted"] == DLC  # data_service and the frontend keep running
    assert r["keys_after"] == {"camera_service": "key-two", "heatmap_service": "key-two"}
    assert r["flags"] == ROUND_DONE


@folders("camera_service", "heatmap_service")
def test_a_removed_folder_stops_its_service_at_once_and_it_is_never_started_again(branch):
    env, _ = branch
    r = run(env, "delete_folder")
    assert r["heatmap_alive"] is False and r["heatmap_exitcode"] is not None
    assert r["heatmap_runner"] == [] and r["heatmap_port"] is False
    assert r["heatmap_pids_after"] == []
    # Gone from disk without the event and killed: the watchdog's guard leaves it dead.
    assert r["camera_exitcode"] == -signal.SIGKILL
    assert r["camera_started_again"] is False
    assert r["alive"] == CORE


@folders("flaky_service")
def test_a_dlc_round_clears_the_back_off_of_the_service_it_restarts(branch):
    env, _ = branch
    r = run(env, "dlc_clears_backoff")
    assert r["dead_exitcode"] == 1
    assert r["started_after"] == 0  # not the 80 s its back-off had reached
    assert r["answers"] == "flaky_service"
    assert r["next_wait"] == 5  # its back-off started over, not 160 s


@folders("cus_emp_service", "flaky_service", "heatmap_service")
def test_the_free_slot_goes_to_the_service_due_longest_not_to_a_crash_looping_one(branch):
    env, _ = branch
    r = run(env, "fair_slots")
    # Killed 5 s after flaky's third death (its next start due 15 s later): cus_emp and
    # heatmap are due 5 s after; cus_emp takes 30 s; heatmap, due first, goes before flaky.
    assert r["order"] == [["run_cus_emp_service", 5], ["run_heatmap_service", 35], ["run_flaky_service", 40]]


@folders("camera_service", "heatmap_service")
def test_socket_threads_never_change_the_process_table_and_a_removed_service_stays_gone(branch):
    env, _ = branch
    r = run(env, "socket_threads")
    assert r["error"] is None
    assert r["foreign_writes"] == []
    assert r["heatmap_other_pids"] == [] and r["heatmap_alive"] is False
    assert r["alive"] == ["run_camera_service"] + CORE


@folders("camera_service", "heatmap_service")
def test_a_restart_round_takes_its_work_under_the_dlc_lock(branch):
    env, _ = branch
    r = run(env, "round_takes_under_lock")
    assert r["blocked"] is True
    assert r["restarted"] == DLC  # heatmap delivered, camera added while the round waited
    assert r["flags"] == ROUND_DONE


@folders("camera_service")
def test_a_round_during_a_transfer_leaves_it_pending_and_the_new_service_starts(branch):
    env, _ = branch
    r = run(env, "transfer_survives_round")
    assert r["key_round_restarted"] == ["run_camera_service"]
    assert r["pending_after_key_round"] == ["zone_service"]
    assert r["started_after"] == 120
    assert r["answers"] == "zone_service"
