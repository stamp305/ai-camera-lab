"""Tickets 03 and 18: what no page or service uses, and what moved to the DLC,
has left the Main Client.

The data service is imported for real; only the database driver (a C extension
from the bundled python_env) is faked, so no MariaDB is needed.
"""
import os
import socket
import subprocess
import sys
import textwrap
import threading
import time
import types
import uuid

import httpx
import pytest

import _fake_mariadb

TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS)


def serve(app, lifespan):
    """Run the app under real uvicorn on loopback; returns (base url, stop)."""
    import uvicorn
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan=lifespan))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    end = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < end:
        time.sleep(0.02)
    assert server.started, "the data service did not start"

    def stop():
        server.should_exit = True
        thread.join(5)
    return f"http://127.0.0.1:{sock.getsockname()[1]}", stop


REMOVED_FEEDS = ("in-pass-config", "hotzone-config", "rate-config", "charge-cus-config", "attendance-config")


def test_data_service_imports_and_starts_with_no_dlc(tmp_path):
    # A fresh process with an empty HOME: no managed services folder, no
    # dlc_local, and any import of a DLC module fails loudly.
    script = textwrap.dedent(f"""
        import importlib.abc, sys, types
        sys.path[:0] = [{ROOT!r}, {TESTS!r}]
        import httpx
        from test_data_service_dead_code import serve

        class NoDlc(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path, target=None):
                top = name.split(".")[0]
                if top in ("dlc_local", "ai_camera_db", "configs", "events") or (
                        top.endswith("_service") and top != "data_service"):
                    raise ImportError("data service reached for DLC module " + name)
        sys.meta_path.insert(0, NoDlc())

        env = types.ModuleType("data_service.configs.secure_env")
        env.load_secure_env = lambda: None
        sys.modules["data_service.configs.secure_env"] = env
        import _fake_mariadb
        _fake_mariadb.install(sys.modules)
        _fake_mariadb.ROWS.append(("COUNT(*) FROM account_config", [(1,)]))

        from passlib.hash import bcrypt  # no bcrypt backend here: a password "hash" is the password
        bcrypt.verify = classmethod(lambda cls, secret, stored: secret == stored)
        _fake_mariadb.ROWS[:0] = [
            ("SELECT password FROM account_config WHERE username", [("pw",)]),
            ("credentials_changed FROM account_config", [(0,)]),
            ("SELECT * FROM store", [{{"id": "10001", "name": "Shop", "region": "BKK", "province": "BKK",
                                       "open_time": "08:00:00", "close_time": "20:00:00"}}]),
        ]
        import runtime_paths
        runtime_paths.RUN_DIR = {str(tmp_path)!r}

        from data_service.main import app
        from data_service.configs.client_api_config import TRIGGER_APIS
        TRIGGER_APIS["update_camera"] = "http://127.0.0.1:1/update_cameras"  # nothing listens: never a real camera_service
        import os, time
        from data_service.utils import log_picture_cleanup
        log_picture_cleanup.LOGS_DIR = {str(tmp_path / "logs")!r}  # never the worktree's own folder
        old = os.path.join(log_picture_cleanup.LOGS_DIR, "in_pass", "old.jpg")
        os.makedirs(os.path.dirname(old))
        open(old, "wb").close()
        os.utime(old, (0, 0))
        url, stop = serve(app, "on")  # the lifespan runs too
        assert httpx.get(url + "/health").status_code == 200
        assert httpx.get(url + "/data/active-cameras-config").json() == {{}}
        for feed in {REMOVED_FEEDS!r}:
            assert httpx.get(url + "/data/" + feed).status_code == 404, feed
        assert not os.path.exists(old), "the lifespan did not run the log-picture clean-up"
        r = httpx.post(url + "/users/login", json={{"username": "admin", "password": "pw"}})
        assert r.status_code == 200 and r.json()["is_activated"] is False, r.text
        r = httpx.get(url + "/store/")
        assert r.status_code == 200 and r.json()["name"] == "Shop", r.text
        r = httpx.patch(url + "/cameras/channels/selection",
                        json=[{{"ip": "10.0.0.5", "channel": 1, "network_name": "NVR"}}])
        assert r.status_code == 200, r.text
        stop()

        import frontend_web.main as fe
        url, stop = serve(fe.app, "on")
        assert httpx.get(url + "/login").status_code == 200
        stop()

        import socket, threading
        import client_logic
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            client_logic.ACTIVATION_API_PORT = s.getsockname()[1]
        threading.Thread(target=client_logic.run_activation_server, daemon=True).start()
        end = time.monotonic() + 10
        while True:
            try:
                r = httpx.post(f"http://127.0.0.1:{{client_logic.ACTIVATION_API_PORT}}/trigger_activate",
                               json={{"activateID": "ADV-1"}})
                break
            except httpx.ConnectError:
                assert time.monotonic() < end, "the activation service did not start"
                time.sleep(0.05)
        assert r.status_code == 200 and r.json()["status"] is False, r.text  # not activated on this machine
        print("STARTED")
    """)
    env = dict(os.environ, HOME=str(tmp_path), PYTHONDONTWRITEBYTECODE="1",
               AICAMERA_KEYCHAIN_SERVICE="aicamera-test-" + uuid.uuid4().hex)
    env.pop("DLC_LOCAL_PATH", None)
    r = subprocess.run([sys.executable, "-c", script], cwd=tmp_path, env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and "STARTED" in r.stdout, r.stdout + r.stderr


@pytest.fixture(scope="module")
def data_client():
    with pytest.MonkeyPatch.context() as mp:
        # Only while the app is imported and used: the frontend tests need the real driver to be missing.
        mp.setitem(sys.modules, "mariadb", _fake_mariadb.install({}))
        mp.setattr(_fake_mariadb, "ROWS", [("COUNT(*) FROM account_config", [(1,)])])
        import data_service.db.session as session
        mp.setattr(session, "mariadb", sys.modules["mariadb"])
        from data_service.main import app
        url, stop = serve(app, "off")  # the picture prefetch is not under test here
        with httpx.Client(base_url=url) as client:
            yield client
        stop()


@pytest.mark.parametrize("method,path", [
    ("GET", "/cameras/zoom/in-pass/10.0.0.5/1"),
    ("POST", "/cameras/zoom/in-pass/10.0.0.5/1"),
    ("GET", "/cus-emp/has-dlc"),
    ("GET", "/cus-emp/uniforms"),
    ("POST", "/cus-emp/uniforms"),
    ("GET", "/cus-emp/uniforms/1"),
    ("PUT", "/cus-emp/uniforms/1"),
    ("DELETE", "/cus-emp/uniforms/1"),
    ("PATCH", "/cus-emp/uniforms/1/status"),
    ("POST", "/cus-emp/uniforms/1/image"),
    ("DELETE", "/cus-emp/uniforms/image/1"),
    ("GET", "/data/cus-emp-config"),
    ("GET", "/logs/analytics/visit-duration"),
    ("GET", "/logs/analytics/zone-dwell"),
    ("GET", "/logs/analytics/zone-engagement"),
    ("GET", "/monitor"),  # ticket 17: Live Display reads each service itself
    # ticket 18: Zone Settings and thresholds live in each DLC service
    *[(m, f"/{svc}/{p}") for svc in ("in-pass", "hotzone", "rate", "attendance")
      for m, p in (("GET", "has-dlc"), ("GET", "polygons"), ("POST", "polygons"), ("DELETE", "polygons/z1"))],
    ("GET", "/in-pass/thresholds/10.0.0.5/1"),
    ("PUT", "/in-pass/thresholds/10.0.0.5/1"),
    ("GET", "/cameras/processed_image/10.0.0.5/1"),
    # ticket 20: each DLC service builds its own config; only the camera list stays
    *[("GET", f"/data/{feed}") for feed in REMOVED_FEEDS],
    # ticket 18: log rows are written by ai_camera_db
    *[("POST", f"/logs/{p}") for p in ("in-pass", "age-gender", "hotzone", "attendance", "rate",
                                        "charge-cus", "demo", "demo-apple", "engagement-wait")],
    ("PATCH", "/logs/demo-apple/tier"),
    ("PATCH", "/logs/hotzone/duration"),
    ("PATCH", "/logs/engagement-wait/end"),
])
def test_removed_endpoints_answer_404(data_client, method, path):
    assert data_client.request(method, path).status_code == 404


def test_camera_image_has_no_zoom_option(data_client):
    # zoom=true used to skip the kept picture and crop a live one; now it is ignored.
    r = data_client.get("/cameras/image/10.0.0.5/1", params={"cached": "true", "zoom": "true", "service": "rate"})
    assert r.status_code == 404 and r.json()["detail"] == "Image not ready yet"


def test_camera_list_for_camera_service_has_no_crop_keys(data_client, monkeypatch):
    monkeypatch.setattr(_fake_mariadb, "ROWS", [
        ("FROM camera_channel cc", [("10.0.0.5", 1, "admin", "pw"), ("10.0.0.5", 2, "admin", "pw"),
                                    ("10.0.0.6", 3, "a@b", "p:w/1")]),
        # A crop saved before zoom went: it must not be read any more.
        ("camera_zoom", [{"x1": 0.1, "y1": 0.1, "x2": 0.5, "y2": 0.5}]),
    ])
    r = data_client.get("/data/active-cameras-config")
    assert r.status_code == 200
    rtsp = "rtsp://admin:pw@10.0.0.5:554/cam/realmonitor?channel={}&subtype=0"
    assert r.json() == {
        "cam_10_0_0_5_1": {"path_rtsp": rtsp.format(1), "box_detect": None},
        "cam_10_0_0_5_2": {"path_rtsp": rtsp.format(2), "box_detect": None},
        "cam_10_0_0_6_3": {"path_rtsp": "rtsp://a%40b:p%3Aw%2F1@10.0.0.6:554/cam/realmonitor?channel=3&subtype=0",
                           "box_detect": None},
    }


def test_feed_builders_and_threshold_store_are_gone():
    # ticket 20: the zone config builders, the in_pass threshold CRUD (with its
    # lazy CREATE TABLE) and its schema left with the feeds; the RTSP port stays.
    import importlib
    from data_service.configs import tracking_config
    from data_service.utils import data_api_utils
    assert {k for k in vars(tracking_config) if k.isupper()} == {"DEFAULT_RTSP_PORT"}
    assert not any(n.startswith("get_") and n.endswith("_config") for n in vars(data_api_utils))
    for gone in ("data_service.db.crud.in_pass_threshold_crud", "data_service.schemas.in_pass_schemas"):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(gone)


def test_frontend_has_no_uniform_page():
    import frontend_web.main as fe
    assert "/employee" not in {getattr(r, "path", None) for r in fe.app.routes}
    with open(os.path.join(ROOT, "frontend_web", "templates", "layout_navbar.html"), encoding="utf-8") as f:
        assert "/employee" not in f.read()


def test_log_picture_cleanup_deletes_only_pictures_past_retention(tmp_path, monkeypatch):
    from data_service.utils import log_picture_cleanup as cleanup
    monkeypatch.setattr(cleanup, "LOGS_DIR", str(tmp_path))
    folder = tmp_path / "in_pass"
    folder.mkdir()
    old, new = folder / "old.jpg", folder / "new.jpg"
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    past = time.time() - cleanup.MAX_AGE_SECONDS - 60
    os.utime(old, (past, past))
    assert cleanup.MAX_AGE_SECONDS == 7 * 24 * 60 * 60
    assert cleanup.cleanup_old_log_images() == 1
    assert not old.exists() and new.exists()


def test_log_picture_cleanup_folder_is_the_data_service_static_logs():
    from data_service.utils import log_picture_cleanup as cleanup
    assert cleanup.LOGS_DIR == os.path.join(ROOT, "data_service", "static", "logs")


CAMERA_ROWS = [("FROM camera_channel cc", [("10.0.0.5", 1, "admin", "pw")])]
CAMERA_LIST = {"cam_10_0_0_5_1": {"path_rtsp": "rtsp://admin:pw@10.0.0.5:554/cam/realmonitor?channel=1&subtype=0",
                                  "box_detect": None}}


@pytest.fixture
def posts(monkeypatch):
    """Every requests.post the data service makes: [(url, json)]."""
    import requests
    seen = []

    def post(url, json=None, **kw):
        seen.append((url, json))
        return types.SimpleNamespace(status_code=200, text="{}")
    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(_fake_mariadb, "EXECUTED", [])
    return seen


def _pushes(posts):
    end = time.monotonic() + 5  # the push runs as a background task, after the answer
    while not posts and time.monotonic() < end:
        time.sleep(0.02)
    time.sleep(0.2)  # a fan-out would follow the camera_service push
    return posts


@pytest.mark.parametrize("change", ["selection", "edit network", "delete network"])
def test_camera_list_goes_to_camera_service_only(data_client, posts, monkeypatch, change):
    from data_service.configs.client_api_config import TRIGGER_APIS
    if change == "selection":
        monkeypatch.setattr(_fake_mariadb, "ROWS", CAMERA_ROWS)
        r = data_client.patch("/cameras/channels/selection",
                              json=[{"ip": "10.0.0.5", "channel": 1, "network_name": "NVR"}])
    elif change == "edit network":
        monkeypatch.setattr(_fake_mariadb, "ROWS", CAMERA_ROWS + [
            ("password, channel_count FROM camera_master WHERE id", [
                {"id": 1, "ip": "10.0.0.5", "name": "NVR", "username": "admin", "password": "", "channel_count": 16}]),
            ("SELECT ip FROM camera_master WHERE id", [("10.0.0.5",)]),
            ("AND name = ? AND id <>", [(0,)]),
            ("SELECT channel_count FROM camera_master WHERE id", [(16,)]),
            ("COUNT(*) FROM camera_channel WHERE ip", [(16,)]),
        ])
        r = data_client.put("/cameras/configs/1", json={"ip": "10.0.0.5", "name": "NVR 2"})
    else:
        monkeypatch.setattr(_fake_mariadb, "ROWS", CAMERA_ROWS + [
            ("SELECT ip FROM camera_master WHERE id", [("10.0.0.5",)]),
            ("COUNT(*) FROM camera_master WHERE ip", [(0,)]),
        ])
        r = data_client.delete("/cameras/configs/1")
    assert r.status_code == 200, r.text
    assert _pushes(posts) == [(TRIGGER_APIS["update_camera"], CAMERA_LIST)]
    assert TRIGGER_APIS["update_camera"].endswith("/update_cameras")


def test_removing_the_last_network_on_an_ip_keeps_its_zone_polygons(data_client, posts, monkeypatch):
    # B12: reconnecting the NVR brings its zones back.
    monkeypatch.setattr(_fake_mariadb, "ROWS", [
        ("SELECT ip FROM camera_master WHERE id", [("10.0.0.5",)]),
        ("COUNT(*) FROM camera_master WHERE ip", [(0,)]),
    ])
    assert data_client.delete("/cameras/configs/1").status_code == 200
    _pushes(posts)
    sql = " ".join(_fake_mariadb.EXECUTED)
    assert "DELETE FROM camera_channel WHERE ip" in sql  # the NVR really went
    assert "_polygon" not in sql and "in_pass_threshold" not in sql


def test_dlc_url_table_has_only_activation_and_camera_service():
    from data_service.configs.client_api_config import TRIGGER_APIS
    assert set(TRIGGER_APIS) == {"activate_success", "update_camera"}
