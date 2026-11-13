"""A branch that is marked connected must really be reachable (UNS089, 30 Oct).

The link dropped during a DLC transfer; python-socketio reconnected by itself, and
while it was still running connect() the connect loop saw "not connected" with
Engine.IO up and disconnected it. The client ended up marked connected with no
connection under it: every emit dropped without an error, the admin showed the
branch offline, and only a restart of the app brought it back.

The client code is client_logic's own, taken out of the file without importing it;
the server is a real Socket.IO server on loopback, and the client the real
python-socketio the app ships.
"""
import ast
import asyncio
import logging
import os
import re
import socket
import threading
import time
import types

import pytest
import requests
import socketio
import uvicorn

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(ROOT, "client_logic.py"), encoding="utf-8") as _f:
    SOURCE = _f.read()
_TREE = ast.parse(SOURCE)


def load(names, **ns):
    """client_logic's top-level functions, classes and constants named, run in `ns`."""
    body = [node for node in _TREE.body
            if (isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names)
            or (isinstance(node, ast.Assign) and any(getattr(t, "id", None) in names for t in node.targets))]
    ns.setdefault("logger", logging.getLogger("client_logic_test"))
    for mod in (os, threading, time, socketio, requests):
        ns.setdefault(mod.__name__, mod)
    exec(compile(ast.Module(body=body, type_ignores=[]), "client_logic.py", "exec"), ns)
    return ns


def wait_until(check, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def server():
    """A Socket.IO server on loopback that answers identify; yields its URL."""
    sio = socketio.AsyncServer(async_mode="asgi")

    @sio.event
    async def identify(sid, data):
        return {"status": "ok"}

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    config = uvicorn.Config(socketio.ASGIApp(sio), host="127.0.0.1", port=port, log_level="error")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    assert wait_until(lambda: srv.started)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(5)


def connect_loop(**ns):
    return load({"_link_up", "_reconnecting", "OWN_RECONNECT_WAIT_SECONDS", "_wait_for_own_reconnect",
                 "_start_single_client"},
                _socket_refused=lambda: False, _socket_auth=lambda: {}, _reset_http_session=lambda c: None, **ns)


def test_the_connect_loop_leaves_python_socketios_own_reconnect_alone(server):
    ns = connect_loop()
    client = socketio.Client(reconnection=True, reconnection_delay=0.2, reconnection_delay_max=0.2,
                             randomization_factor=0)
    client.server_info = {"url": server, "path": "socket.io"}
    first, in_handler = threading.Event(), threading.Event()

    def on_connect():
        # client_logic's connect() takes a moment (config, identify, threads); the
        # loop struck in that moment.
        if first.is_set():
            in_handler.set()
            time.sleep(1.5)
        first.set()
    client.on("connect", on_connect)
    client.connect(server, wait_timeout=5)

    client.eio.ws.sock.shutdown(socket.SHUT_RDWR)   # the link drops
    assert in_handler.wait(10)
    loop = threading.Thread(target=ns["_start_single_client"], args=(client, 5), daemon=True)
    loop.start()
    loop.join(20)

    assert ns["_link_up"](client)
    assert client.call("identify", {}, timeout=5) == {"status": "ok"}
    client.disconnect()


def multi_client(server_url):
    handlers = set(re.findall(r"sio\.on\('[a-z_]+', ([A-Za-z_]+)\)", SOURCE))
    ns = {name: (lambda *a, **k: None) for name in handlers}
    ns.update(_secrets=types.SimpleNamespace(SOCKET_SERVERS=[{"url": server_url, "path": "socket.io"}]),
              _ENV_SOCKET_HOST_OVERRIDE=None, _ENV_SOCKET_PATH_OVERRIDE=None, _ENV_SOCKET_PORT_OVERRIDE=None)
    ns = load({"_link_up", "_reconnecting", "MultiSocketClient"}, **ns)
    multi = ns["MultiSocketClient"]()
    multi.setup_clients()
    return multi


def mark_dead(client):
    # What the race left behind: python-socketio's flag up, Engine.IO without a connection.
    client.connected = True
    assert client.eio.state == "disconnected"


def test_a_client_marked_connected_without_a_connection_does_not_count_as_connected():
    multi = multi_client("http://127.0.0.1:9")
    mark_dead(multi.clients[0])
    assert multi.connected is False   # what the menu bar and every reconnect check read


def test_a_dead_client_is_replaced_on_the_second_look_only():
    multi = multi_client("http://127.0.0.1:9")
    dead = multi.clients[0]
    mark_dead(dead)
    assert multi.replace_dead_clients() is False   # a disconnect passes through this state
    assert multi.clients[0] is dead
    assert multi.replace_dead_clients() is True
    fresh = multi.clients[0]
    assert fresh is not dead and fresh.connected is False and dead.reconnection is False
    assert fresh.server_info == dead.server_info


def test_a_client_that_recovers_between_two_looks_is_kept():
    multi = multi_client("http://127.0.0.1:9")
    client = multi.clients[0]
    mark_dead(client)
    multi.replace_dead_clients()
    client.connected = False
    multi.replace_dead_clients()
    mark_dead(client)
    assert multi.replace_dead_clients() is False
    assert multi.clients[0] is client


def test_a_replaced_client_connects(server):
    multi = multi_client(server)
    mark_dead(multi.clients[0])
    multi.replace_dead_clients()
    multi.replace_dead_clients()
    ns = connect_loop()
    ns["_start_single_client"](multi.clients[0], 5)
    assert multi.connected
    assert multi.clients[0].call("identify", {}, timeout=5) == {"status": "ok"}
    multi.disconnect()


class _Silent:
    """A connection marked up that never answers anything."""
    def __init__(self):
        self.clients = [object()]
        self.connected = True
        self.replaced = []

    def emit(self, *args, **kwargs):
        pass

    def replace_dead_clients(self):
        return False

    def replace_client(self, i):
        self.replaced.append(i)


def refresher(sio, started):
    return load({"IDENTIFY_REFRESH_SECONDS", "SOCKET_CHECK_SECONDS", "_refresh_identify", "_identify_refresher"}, _sio=sio, _last_ident_payload={"command": "identify"},
                IDENTIFY_REFRESH_ACK_SECONDS=0.05, IDENTIFY_REFRESH_MISSES=2,
                on_identified=lambda reply: None, start_socket_client=started.set)


def test_two_unanswered_repeats_drop_the_connection_and_dial_again():
    sio, started = _Silent(), threading.Event()
    ns = refresher(sio, started)
    threading.Thread(target=ns["_identify_refresher"], kwargs={"interval": 0.01, "check_every": 0.01},
                     daemon=True).start()
    assert started.wait(5)
    assert sio.replaced == [0]


def test_answered_repeats_leave_the_connection_alone():
    class _Answering(_Silent):
        def emit(self, event, data, callback=None):
            callback({"status": "ok", "known": True})
    sio, started = _Answering(), threading.Event()
    ns = refresher(sio, started)
    threading.Thread(target=ns["_identify_refresher"], kwargs={"interval": 0.01, "check_every": 0.01},
                     daemon=True).start()
    assert not started.wait(0.5)
    assert sio.replaced == []
