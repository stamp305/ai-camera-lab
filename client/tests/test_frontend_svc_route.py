"""Seam 3: the frontend route /api/svc/<service>/<path> to DLC services.

The real frontend app (with its session middleware) runs under uvicorn on
loopback; DLC services and the data service are fake HTTP servers on loopback.
The port table and the managed services folder are the inputs.
"""
import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import httpx
import pytest

import port_store

_REAL_GET_ALL_PORTS = port_store.get_all_ports  # before any test fakes the table


# ---------------------------------------------------------------------------
# Fake upstreams
# ---------------------------------------------------------------------------
class _Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive, so a client left open keeps its connection

    def log_message(self, *a):
        pass

    def handle(self):
        with self.server.lock:
            self.server.open_conns += 1
        try:
            super().handle()  # returns once the other side closes the connection
        finally:
            with self.server.lock:
                self.server.open_conns -= 1

    def _reply(self, status, body=b"", headers=()):
        self.send_response(status)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        url = urlsplit(self.path)
        seen = {"method": self.command, "raw": self.path, "path": url.path, "query": url.query,
                "body": body.decode(), "headers": {k.lower(): v for k, v in self.headers.items()}}
        self.server.seen.append(seen)
        own = f"http://127.0.0.1:{self.server.server_port}"
        if url.path == "/missing":
            self._reply(404, b'{"detail":"Not Found"}', [("Content-Type", "application/json")])
        elif url.path == "/redirect-self":
            self._reply(307, headers=[("Location", f"{own}/zones/?a=1")])
        elif url.path == "/redirect-relative":
            self._reply(302, headers=[("Location", "/zones/")])
        elif url.path == "/redirect-other-port":
            self._reply(302, headers=[("Location", "http://127.0.0.1:1/zones/")])
        elif url.path == "/redirect-away":
            self._reply(302, headers=[("Location", "http://example.invalid/zones/")])
        elif url.path == "/redirect-other-host-same-port":
            self._reply(302, headers=[("Location", f"http://example.invalid:{self.server.server_port}/zones/")])
        elif url.path == "/redirect-https-same-port":
            self._reply(302, headers=[("Location", f"https://127.0.0.1:{self.server.server_port}/zones/")])
        elif url.path == "/hop":
            # BaseHTTPRequestHandler also sends its own Server and Date.
            self._reply(200, b"{}", [("Content-Type", "application/json"), ("Connection", "X-Hop-Down"),
                                     ("X-Hop-Down", "1"), ("Keep-Alive", "timeout=5"),
                                     ("Proxy-Authenticate", "Basic"), ("X-Svc-Missing", "1"),
                                     ("X-Kept", "1")])
        elif url.path == "/slow-headers":
            time.sleep(1.0)
            self._reply(200, b"{}", [("Content-Type", "application/json")])
        elif url.path == "/slow-body":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "2")
            self.end_headers()
            time.sleep(1.0)
            self.wfile.write(b"{}")
        elif url.path.startswith("/stream/"):
            self._stream()
        elif url.path.startswith("/stream-pause/"):
            self._stream(pause=1.0)
        else:
            self._reply(200, json.dumps(seen).encode(), [
                ("Content-Type", "application/json"),
                ("Set-Cookie", "upstream=1; Path=/"),
                ("X-Upstream", self.server.name),
            ])

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _serve

    def _stream(self, pause=None):
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            for i in range(1000):
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\nframe%d\r\n" % i)
                if i == 0 and pause:
                    time.sleep(pause)  # a camera that goes quiet for a while
                elif i == 0:
                    self.server.go.wait(5)  # frame 1 only once the test has seen frame 0
                time.sleep(0.02)
        except OSError:
            self.server.stream_closed.set()


def _start_upstream(name):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    server.daemon_threads = True
    server.name, server.seen, server.lock, server.open_conns = name, [], threading.Lock(), 0
    server.go, server.stream_closed = threading.Event(), threading.Event()
    server.port = server.server_port
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def frontend():
    import uvicorn
    import frontend_web.main as fe

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(fe.app, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    assert _wait(lambda: server.started)
    yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    server.should_exit = True
    thread.join(5)


@pytest.fixture
def table(monkeypatch, tmp_path):
    """The port table (a dict) and an empty managed services folder."""
    rows = {**port_store.CORE_SERVICES, **port_store.SOCKET_PORTS}

    def no_db():
        raise AssertionError("tests must not reach a database")

    monkeypatch.setattr(port_store, "_connect", no_db)
    monkeypatch.setattr(port_store, "get_all_ports", lambda: dict(rows))
    monkeypatch.setattr(port_store, "get_port", lambda name, default: rows.get(name, default))
    monkeypatch.setattr(port_store, "MANAGED_DIR", str(tmp_path))
    return rows


@pytest.fixture
def deliver(table):
    def _deliver(name, port=None, entry="main.py"):
        folder = os.path.join(port_store.MANAGED_DIR, name)
        os.makedirs(folder, exist_ok=True)
        if entry:
            open(os.path.join(folder, entry), "w").close()
        if port is not None:
            table[name] = port
    return _deliver


@pytest.fixture
def data_service(table):
    up = _start_upstream("data_service")
    table["data_service"] = up.port
    yield up
    up.shutdown()


@pytest.fixture
def session(frontend, data_service):
    """A real login through the data-service proxy; returns the Cookie header."""
    r = httpx.post(f"{frontend}/api/proxy/users/login", json={"username": "u", "password": "p"})
    assert r.status_code == 200
    return {"Cookie": f"aic_session={r.cookies['aic_session']}"}


@pytest.fixture
def hotzone(deliver):
    up = _start_upstream("hotzone_service")
    deliver("hotzone_service", up.port)
    yield up
    up.shutdown()


# ---------------------------------------------------------------------------
# Forwarding
# ---------------------------------------------------------------------------
def test_forwards_method_path_query_and_body(frontend, session, hotzone):
    r = httpx.post(f"{frontend}/api/svc/hotzone_service/zones/cam_1/2?x=1&y=two",
                   content=b'{"zone":[1,2]}', headers={**session, "Content-Type": "application/json"})
    assert r.status_code == 200
    seen = hotzone.seen[-1]
    assert (seen["method"], seen["path"], seen["query"], seen["body"]) == \
        ("POST", "/zones/cam_1/2", "x=1&y=two", '{"zone":[1,2]}')
    assert seen["headers"]["content-type"] == "application/json"


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE", "PATCH"])
def test_forwards_every_method(frontend, session, hotzone, method):
    r = httpx.request(method, f"{frontend}/api/svc/hotzone_service/zones", headers=session)
    assert r.status_code == 200
    assert hotzone.seen[-1]["method"] == method


def test_main_so_counts_as_an_entry_point(frontend, session, deliver):
    up = _start_upstream("rate_service")
    try:
        deliver("rate_service", up.port, entry="main.so")
        r = httpx.get(f"{frontend}/api/svc/rate_service/health", headers=session)
        assert r.status_code == 200 and r.headers["x-upstream"] == "rate_service"
    finally:
        up.shutdown()


def test_stream_arrives_chunk_by_chunk_and_upstream_closes_when_browser_leaves(frontend, session, hotzone):
    with httpx.Client(timeout=5) as client:
        with client.stream("GET", f"{frontend}/api/svc/hotzone_service/stream/cam_1", headers=session) as r:
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("multipart/x-mixed-replace")
            chunks = r.iter_raw()
            buf = b""
            while b"frame0" not in buf:
                buf += next(chunks)  # times out if the route waits for the whole body
            hotzone.go.set()
            while b"frame1" not in buf:
                buf += next(chunks)
    # The browser side is closed; the route must drop its upstream connection.
    assert hotzone.stream_closed.wait(5)
    assert _wait(lambda: hotzone.open_conns == 0)


def test_closes_upstream_connection_after_a_plain_answer(frontend, session, hotzone):
    assert httpx.get(f"{frontend}/api/svc/hotzone_service/zones", headers=session).status_code == 200
    assert _wait(lambda: hotzone.open_conns == 0)


# ---------------------------------------------------------------------------
# Which names and ports are allowed
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", [
    "data_service",                 # core service, even with a folder and a row
    "activation_service",
    "frontend_web",
    "tracking_stream_socket",       # socket listener
    "client_update",
    "nope_service",                 # unknown: no folder, no row
    "nofolder_service",             # row but no folder
    "noentry_service",              # row and folder, but no main.py / main.so
    "Hotzone_service",              # does not match ^[a-z0-9_]+_service$
    "hotzone",
])
def test_not_allowed_names_are_404_with_missing_header(frontend, session, deliver, table, name):
    deliver("data_service")
    deliver("activation_service")
    table["nofolder_service"] = 8100
    deliver("noentry_service", 8101, entry=None)
    r = httpx.get(f"{frontend}/api/svc/{name}/zones", headers=session)
    assert r.status_code == 404
    assert r.headers.get("x-svc-missing") == "1"


def test_folder_without_port_row_is_404(frontend, session, deliver):
    deliver("hotzone_service")
    r = httpx.get(f"{frontend}/api/svc/hotzone_service/zones", headers=session)
    assert r.status_code == 404 and r.headers.get("x-svc-missing") == "1"


def test_service_own_404_passes_without_missing_header(frontend, session, hotzone):
    r = httpx.get(f"{frontend}/api/svc/hotzone_service/missing", headers=session)
    assert r.status_code == 404
    assert "x-svc-missing" not in r.headers
    assert r.json() == {"detail": "Not Found"}


def test_dead_upstream_is_502(frontend, session, deliver):
    deliver("hotzone_service", _free_port())
    r = httpx.get(f"{frontend}/api/svc/hotzone_service/zones", headers=session)
    assert r.status_code == 502
    assert "x-svc-missing" not in r.headers


@pytest.mark.parametrize("name", ["hot-zone_service", "Hotzone_service", "hotzone", "hotzone_service_x"])
def test_names_off_the_pattern_are_404_even_when_delivered(frontend, session, deliver, name):
    up = _start_upstream(name)
    try:
        deliver(name, up.port)
        r = httpx.get(f"{frontend}/api/svc/{name}/zones", headers=session)
        assert r.status_code == 404 and r.headers.get("x-svc-missing") == "1"
        assert up.seen == []
    finally:
        up.shutdown()


@pytest.mark.parametrize("kind", ["core", "socket", "database", "remote_status", "other_name"])
def test_refused_port_is_never_contacted_even_with_a_listener(frontend, session, deliver, table, monkeypatch, kind):
    import frontend_web.main as fe
    squatter = _start_upstream("squatter")
    try:
        if kind == "core":  # the constant only: data_service's row stays on its own port
            monkeypatch.setitem(port_store.CORE_SERVICES, "data_service", squatter.port)
        elif kind == "socket":
            monkeypatch.setitem(port_store.SOCKET_PORTS, "tracking_stream_socket", squatter.port)
        elif kind == "database":
            monkeypatch.setenv("DB_PORT", str(squatter.port))
        elif kind == "remote_status":
            monkeypatch.setattr(fe, "_REMOTE_STATUS_PORT", squatter.port)
        else:
            deliver("rate_service", squatter.port)
        deliver("hotzone_service", squatter.port)
        r = httpx.get(f"{frontend}/api/svc/hotzone_service/zones", headers=session)
        assert r.status_code == 502 and "x-svc-missing" not in r.headers
        assert squatter.seen == []
    finally:
        squatter.shutdown()


@pytest.mark.parametrize("bad_port", [0, -1, 65536, 70000])
def test_port_row_outside_range_is_refused(frontend, session, deliver, bad_port):
    deliver("hotzone_service", bad_port)
    r = httpx.get(f"{frontend}/api/svc/hotzone_service/zones", headers=session)
    assert r.status_code == 502 and "x-svc-missing" not in r.headers


def test_unreadable_port_table_is_503_not_missing(frontend, session, deliver, monkeypatch):
    deliver("hotzone_service")
    monkeypatch.setattr(port_store, "get_all_ports", _REAL_GET_ALL_PORTS)  # its database read fails here
    r = httpx.get(f"{frontend}/api/svc/hotzone_service/zones", headers=session)
    assert r.status_code == 503 and "x-svc-missing" not in r.headers


def test_port_lookup_does_not_block_other_requests(frontend, session, hotzone, table, monkeypatch):
    reading = threading.Event()

    def slow_table():
        reading.set()
        time.sleep(1.0)  # a database that answers slowly
        return dict(table)

    monkeypatch.setattr(port_store, "get_all_ports", slow_table)
    caller = threading.Thread(target=httpx.get, args=(f"{frontend}/api/svc/hotzone_service/zones",),
                              kwargs={"headers": session, "timeout": 5})
    caller.start()
    assert reading.wait(2)
    t0 = time.monotonic()
    assert httpx.get(f"{frontend}/frontend-health").status_code == 200
    assert time.monotonic() - t0 < 0.5
    caller.join(5)


def test_port_change_is_used_on_the_next_request(frontend, session, hotzone, table):
    assert httpx.get(f"{frontend}/api/svc/hotzone_service/a", headers=session).headers["x-upstream"] == "hotzone_service"
    moved = _start_upstream("hotzone_moved")
    try:
        table["hotzone_service"] = moved.port
        r = httpx.get(f"{frontend}/api/svc/hotzone_service/a", headers=session)
        assert r.status_code == 200 and r.headers["x-upstream"] == "hotzone_moved"
    finally:
        moved.shutdown()


# ---------------------------------------------------------------------------
# Headers and redirects
# ---------------------------------------------------------------------------
def test_cookie_authorization_host_not_forwarded_and_set_cookie_dropped(frontend, session, hotzone):
    r = httpx.get(f"{frontend}/api/svc/hotzone_service/zones", headers={
        "Cookie": session["Cookie"] + "; other=x",
        "Authorization": "Bearer secret",
        "Connection": "keep-alive",
        "X-Page": "zone",
    })
    assert r.status_code == 200
    up = hotzone.seen[-1]["headers"]
    assert "cookie" not in up and "authorization" not in up
    assert up["host"] == f"127.0.0.1:{hotzone.port}"
    assert up["x-page"] == "zone"
    assert "set-cookie" not in r.headers
    assert r.headers["x-upstream"] == "hotzone_service"


@pytest.mark.parametrize("path, location", [
    ("redirect-self", "/api/svc/hotzone_service/zones/?a=1"),
    ("redirect-relative", "/api/svc/hotzone_service/zones/"),
])
def test_loopback_redirect_rewritten_not_followed(frontend, session, hotzone, path, location):
    r = httpx.get(f"{frontend}/api/svc/hotzone_service/{path}", headers=session)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == location
    assert [s["path"] for s in hotzone.seen] == [f"/{path}"]


@pytest.mark.parametrize("path", ["redirect-other-port", "redirect-other-host-same-port",
                                  "redirect-https-same-port", "redirect-away"])
def test_redirect_elsewhere_is_502(frontend, session, hotzone, path):
    r = httpx.get(f"{frontend}/api/svc/hotzone_service/{path}", headers=session)
    assert r.status_code == 502
    assert "location" not in r.headers


def test_hop_by_hop_and_connection_named_headers_dropped_both_ways(frontend, session, hotzone):
    r = httpx.get(f"{frontend}/api/svc/hotzone_service/hop", headers={
        **session, "Connection": "keep-alive, X-Hop-Up", "X-Hop-Up": "1", "Keep-Alive": "timeout=5",
        "TE": "trailers", "Proxy-Authorization": "Basic eA==", "X-Page": "zone"})
    assert r.status_code == 200
    up = hotzone.seen[-1]["headers"]
    for name in ("x-hop-up", "keep-alive", "te", "proxy-authorization"):
        assert name not in up
    assert up["x-page"] == "zone"
    for name in ("x-hop-down", "keep-alive", "proxy-authenticate", "x-svc-missing"):
        assert name not in r.headers
    assert r.headers["x-kept"] == "1"
    assert r.headers.get_list("server") == ["uvicorn"]
    assert len(r.headers.get_list("date")) == 1


@pytest.mark.parametrize("sent", [
    "/a%3Fb/c%23d/e%2Ff/g%25h",
    "/zones/x?q=a%26b&r=%2F%3F",
    "/zones/",
])
def test_path_and_query_forwarded_exactly_as_sent(frontend, session, hotzone, sent):
    r = httpx.get(f"{frontend}/api/svc/hotzone_service{sent}", headers=session)
    assert r.status_code == 200
    assert hotzone.seen[-1]["raw"] == sent


@pytest.mark.parametrize("bad, status", [
    ("a%00b", 400), ("a%1Bb", 400), ("a%7Fb", 400), ("a%09b", 400),
    ("a%0Ab", 404),  # Starlette's {path:path} does not match a newline: no route at all, so no X-Svc-Missing
])
def test_control_characters_in_path_never_reach_the_service(frontend, session, hotzone, bad, status):
    r = httpx.get(f"{frontend}/api/svc/hotzone_service/{bad}", headers=session)
    assert r.status_code == status
    assert "x-svc-missing" not in r.headers
    assert hotzone.seen == []


# ---------------------------------------------------------------------------
# Timeouts
# ---------------------------------------------------------------------------
@pytest.fixture
def short_timeout(monkeypatch):
    import frontend_web.main as fe
    monkeypatch.setattr(fe, "_SVC_TIMEOUT", 0.3)


def test_live_view_may_pause_longer_than_the_timeout(frontend, session, hotzone, short_timeout):
    with httpx.Client(timeout=5) as client:
        with client.stream("GET", f"{frontend}/api/svc/hotzone_service/stream-pause/cam_1", headers=session) as r:
            assert r.status_code == 200
            buf, chunks = b"", r.iter_raw()
            while b"frame1" not in buf:  # frame 1 comes 1 s after frame 0
                buf += next(chunks)


def test_slow_answer_headers_are_502(frontend, session, hotzone, short_timeout):
    r = httpx.get(f"{frontend}/api/svc/hotzone_service/slow-headers", headers=session, timeout=5)
    assert r.status_code == 502


def test_plain_body_reads_stay_bounded(frontend, session, hotzone, short_timeout):
    with pytest.raises(httpx.HTTPError):  # the answer is cut off, not left hanging
        httpx.get(f"{frontend}/api/svc/hotzone_service/slow-body", headers=session, timeout=5)


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_no_session_is_401_session_expired(frontend, hotzone, method):
    r = httpx.request(method, f"{frontend}/api/svc/hotzone_service/zones")
    assert r.status_code == 401
    assert r.headers.get("x-session-expired") == "1"
    assert hotzone.seen == []


# ---------------------------------------------------------------------------
# The existing data-service proxy
# ---------------------------------------------------------------------------
def test_data_service_proxy_still_works_and_closes_its_client(frontend, session, data_service):
    r = httpx.get(f"{frontend}/api/proxy/cameras?branch=1", headers=session)
    assert r.status_code == 200
    assert (data_service.seen[-1]["path"], data_service.seen[-1]["query"]) == ("/cameras", "branch=1")
    assert _wait(lambda: data_service.open_conns == 0)
