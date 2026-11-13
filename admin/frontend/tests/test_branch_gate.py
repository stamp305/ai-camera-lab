"""Seam 2: frontend Branch gate (ADR 0007).

Drives the Flask app through its test client, and the Branch socket through
Flask-SocketIO's test client, sending what a browser or a Branch would send.
The data service on :8011 is replaced by canned answers at the requests layer,
so nothing leaves the process.

Run (from ai-camera-admin/frontend):  python -m pytest tests -q
"""
from __future__ import annotations

import io
import json
import os
import sys
import tarfile
from urllib.parse import urlsplit

import pytest
import requests
from flask import request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # frontend/

# Config reads the LAN-testing extras once, when it is imported, so set them first.
LAN_TEST_IPS = ["192.168.1.80", "10.0.0.5"]
os.environ["AI_CAMERA_ADMIN_EXTRA_ALLOWED_IPS"] = " 192.168.1.80 , 10.0.0.5,"

# Importing the app runs eventlet.monkey_patch(), exactly as production does.
from app_ai_camera_frontend import app
from config import Config
from extensions import socketio
from socketio import packet as sio_packet
import socket_handlers

OFFICE_IP = "171.100.254.57"
SHOP_IP = Config.BRANCH_IPS["AVI001"]
UNLISTED_IP = "203.0.113.9"  # TEST-NET-3: on neither list
FORMER_EXTRA_IP = "104.28.182.106"  # used to be whitelisted beside the office IP
CODE = "ADV-TEST-0000-0001"
OTHER_CODE = "ADV-TEST-0000-0002"
KEY = "dK3y-" + "x" * 38  # shaped like secrets.token_urlsafe(32)


class FakeBackend:
    """The data service as canned answers keyed by (METHOD, path).

    A call with no canned answer fails like an unreachable backend.
    """

    def __init__(self):
        self.answers = {}
        self.calls = []
        self.sent = []  # (METHOD, path, data, headers) of every request, for pass-through checks

    def answer(self, method: str, path: str, status: int = 200, body=None, when=None, raises=None,
               during=None) -> None:
        """`when` limits the answer to requests whose JSON body equals it, and wins
        over the answer without one. `raises` fails the request with that exception.
        `during` is called while the request is in flight, before it is answered.
        A bytes body goes out as is, anything else as JSON."""
        self.answers[(method.upper(), path, _body_key(when))] = (status, body, raises, during)

    def request(self, method: str, url: str, **kwargs) -> requests.Response:
        key = (method.upper(), urlsplit(url).path)
        self.calls.append(key)
        self.sent.append(key + (kwargs.get("data"), kwargs.get("headers") or {}))
        canned = self.answers.get(key + (_body_key(kwargs.get("json")),)) or self.answers.get(key + (None,))
        if canned is None:
            raise requests.exceptions.ConnectionError(f"no canned answer for {key}")
        status, body, raises, during = canned
        if during is not None:
            during()
        if raises is not None:
            raise raises
        resp = requests.Response()
        resp.status_code = status
        resp._content = body if isinstance(body, bytes) else json.dumps(body).encode()
        resp.headers["Content-Type"] = "application/json"
        resp.url = url
        return resp


def _body_key(body):
    return None if body is None else json.dumps(body, sort_keys=True)


@pytest.fixture
def backend(monkeypatch):
    fake = FakeBackend()
    # Every requests.get/post/... goes through Session.request, including the
    # socket handlers' direct calls.
    monkeypatch.setattr(requests.Session, "request",
                        lambda session, method, url, **kw: fake.request(method, url, **kw))
    return fake


@pytest.fixture
def client(backend):
    return app.test_client()


def from_ip(ip: str) -> dict:
    return {"environ_base": {"REMOTE_ADDR": ip}}


def dummy_data_upload(client, ip: str, headers=None):
    return client.post("/ai-camera/api/dummy-data/upload", **from_ip(ip), headers=headers,
                       data={"store_id": "S001", "file": (io.BytesIO(b"zip"), "pipeline.zip")})


def build_upload(client, ip: str, headers=None):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("VERSION")
        info.size = 2
        tar.addfile(info, io.BytesIO(b"v1"))
    buf.seek(0)
    return client.post("/ai-camera/api/build/upload", **from_ip(ip), headers=headers,
                       data={"version": "v1", "file": (buf, "build.tar.gz")})


def activate(client, ip: str):
    return client.post("/ai-camera/api/auth/activate", **from_ip(ip),
                       json={"activation_code": CODE, "serial_number": "SN1", "mac_address": "MAC1"})


@pytest.fixture
def branch_backend(backend, monkeypatch, tmp_path):
    """Backend and disk ready for a Branch's uploads and Activation."""
    backend.answer("POST", "/dummy-data/upload", 200, {"message": "stored"})
    backend.answer("POST", "/auth/activate", 200, {"message": "User activated successfully"})
    backend.answer("GET", "/users", 200, [])
    monkeypatch.setattr(Config, "MANAGED_SERVICES_VERSIONS_CACHE", str(tmp_path))
    return backend


# --- admin UI: office list only --------------------------------------------------------
@pytest.mark.parametrize("path", ["/ai-camera/landing", "/ai-camera/version-manager", "/ai-camera/api/users"])
def test_admin_ui_accepts_the_office_ip(client, branch_backend, path):
    assert client.get(path, **from_ip(OFFICE_IP)).status_code == 200


@pytest.mark.parametrize("path", ["/ai-camera/landing", "/ai-camera/version-manager", "/ai-camera/api/users"])
@pytest.mark.parametrize("ip", [SHOP_IP, UNLISTED_IP, FORMER_EXTRA_IP])
def test_admin_ui_refuses_every_other_ip(client, branch_backend, path, ip):
    assert client.get(path, **from_ip(ip)).status_code == 403


def test_support_ui_takes_the_office_list_too(branch_backend):
    # The IT-support copy of the admin UI (app_support_frontend.py, :8003).
    from app_support_frontend import app as support_app
    support = support_app.test_client()
    assert support.get("/ai-camera/landing", **from_ip(OFFICE_IP)).status_code == 200
    assert support.get("/ai-camera/landing", **from_ip(SHOP_IP)).status_code == 403


# --- LAN-testing extras (AI_CAMERA_ADMIN_EXTRA_ALLOWED_IPS) join the office list -------
@pytest.mark.parametrize("ip", LAN_TEST_IPS)
def test_a_lan_extra_ip_opens_the_admin_ui_and_uploads(client, branch_backend, ip):
    assert client.get("/ai-camera/landing", **from_ip(ip)).status_code == 200
    assert dummy_data_upload(client, ip).status_code == 200


# --- Branch REST: transition list (office + shop IPs) while keyless --------------------
@pytest.mark.parametrize("ip", [SHOP_IP, OFFICE_IP])
def test_dummy_data_upload_from_a_listed_ip_gets_through(client, branch_backend, ip):
    resp = dummy_data_upload(client, ip)
    assert resp.status_code == 200
    assert resp.get_json() == {"message": "stored"}


@pytest.mark.parametrize("ip", [SHOP_IP, OFFICE_IP])
def test_build_upload_from_a_listed_ip_gets_through(client, branch_backend, ip):
    resp = build_upload(client, ip)
    assert resp.status_code == 200
    assert resp.get_json()["message"] == "Upload and extraction successful"


@pytest.mark.parametrize("upload", [dummy_data_upload, build_upload])
@pytest.mark.parametrize("ip", [UNLISTED_IP, FORMER_EXTRA_IP])
def test_upload_from_an_unlisted_ip_is_refused(client, branch_backend, upload, ip):
    assert upload(client, ip).status_code == 403


# --- Branch REST with a Device Key: the verdict decides, never the IP ------------------
def key_headers(code: str = CODE, key: str = KEY) -> dict:
    return {"X-Activation-Code": code, "Authorization": f"Bearer {key}"}


@pytest.fixture
def device_backend(branch_backend):
    """verify-device knows one Branch, CODE holding KEY; any other pair is a wrong key."""
    branch_backend.answer("POST", "/auth/verify-device", 200, {"verdict": "invalid_device_key"})
    branch_backend.answer("POST", "/auth/verify-device", 200, {"verdict": "ok"},
                          when={"activation_code": CODE, "device_key": KEY})
    return branch_backend


@pytest.mark.parametrize("upload", [dummy_data_upload, build_upload])
@pytest.mark.parametrize("ip", [UNLISTED_IP, FORMER_EXTRA_IP])
def test_upload_with_a_valid_device_key_passes_from_any_ip(client, device_backend, upload, ip):
    assert upload(client, ip, key_headers()).status_code == 200


@pytest.mark.parametrize("authorization", [f"Bearer {KEY}", f"bearer {KEY}", f"BEARER   {KEY}  ", f"Bearer\t{KEY}"])
def test_the_bearer_scheme_is_read_leniently(client, device_backend, authorization):
    headers = {"X-Activation-Code": f" {CODE} ", "Authorization": authorization}
    assert dummy_data_upload(client, UNLISTED_IP, headers).status_code == 200


@pytest.mark.parametrize("upload", [dummy_data_upload, build_upload])
@pytest.mark.parametrize("verdict", ["invalid_device_key", "no_key", "unknown_activation_code"])
def test_upload_with_a_refused_key_is_401_even_from_a_shop_ip(client, branch_backend, upload, verdict, caplog):
    # no_key: a key presented for a Branch whose key an admin cleared.
    branch_backend.answer("POST", "/auth/verify-device", 200, {"verdict": verdict})
    resp = upload(client, SHOP_IP, key_headers())
    assert resp.status_code == 401
    assert resp.get_json() == {"error": "invalid_device_key"}
    assert KEY not in caplog.text


@pytest.mark.parametrize("headers", [key_headers(key="wrong-key"), key_headers(code=OTHER_CODE)],
                         ids=["wrong key", "another Branch's code"])
def test_a_wrong_key_or_mismatched_pair_is_401(client, device_backend, headers):
    resp = dummy_data_upload(client, UNLISTED_IP, headers)
    assert resp.status_code == 401
    assert resp.get_json() == {"error": "invalid_device_key"}


@pytest.mark.parametrize("headers", [
    {"X-Activation-Code": CODE},
    {"Authorization": f"Bearer {KEY}"},
    {"X-Activation-Code": CODE, "Authorization": "Bearer"},
    {"X-Activation-Code": CODE, "Authorization": "Bearer   "},
    {"X-Activation-Code": CODE, "Authorization": f"Basic {KEY}"},
    {"X-Activation-Code": "  ", "Authorization": f"Bearer {KEY}"},
    {"Authorization": "Bearer"},
    {"Authorization": f"Basic {KEY}"},
], ids=["code only", "key only", "empty bearer", "blank bearer", "not bearer", "blank code",
        "empty bearer alone", "not bearer alone"])
def test_one_header_without_the_other_is_401_without_asking_the_backend(client, device_backend, headers):
    resp = dummy_data_upload(client, SHOP_IP, headers)
    assert resp.status_code == 401
    assert resp.get_json() == {"error": "invalid_device_key"}
    assert ("POST", "/auth/verify-device") not in device_backend.calls


# A 422 echoes the request body, Device Key included.
ECHOED_422 = {"detail": [{"type": "missing", "loc": ["body", "activation_code"], "msg": "Field required",
                          "input": {"activation_code": CODE, "device_key": KEY}}]}


@pytest.mark.parametrize("canned", [
    None,
    {"raises": requests.exceptions.Timeout("read timed out")},
    {"status": 500, "body": {"error": "Failed to verify device", "detail": "db down"}},
    {"status": 422, "body": ECHOED_422},
    {"status": 404, "body": {"verdict": "ok"}},
    {"status": 200, "body": {}},
    {"status": 200, "body": {"verdict": "maybe"}},
    {"status": 200, "body": ["ok"]},
    {"status": 200, "body": b"<html>502 Bad Gateway</html>"},
], ids=["unreachable", "timeout", "500", "422 echo", "non-200 verdict", "no verdict",
        "unknown verdict", "not an object", "not json"])
@pytest.mark.parametrize("upload", [dummy_data_upload, build_upload])
def test_upload_while_the_backend_gives_no_verdict_is_503_never_401(client, branch_backend, upload, canned, caplog):
    if canned is not None:
        branch_backend.answer("POST", "/auth/verify-device", **canned)
    resp = upload(client, SHOP_IP, key_headers())
    assert resp.status_code == 503
    assert resp.get_json() == {"error": "Upstream service unavailable"}
    assert KEY not in caplog.text


# --- Activation: open to every IP, throttled by serial and by IP (ticket 04) ----------
@pytest.fixture(autouse=True)
def fresh_throttle(monkeypatch):
    import gate
    gate._activation_failures.clear()
    now = [1000.0]
    monkeypatch.setattr(gate, "_clock", lambda: now[0])
    return now


def activate_as(client, ip: str, serial: str = "SN1"):
    return client.post("/ai-camera/api/auth/activate", **from_ip(ip),
                       json={"activation_code": CODE, "serial_number": serial, "mac_address": "MAC1"})


@pytest.mark.parametrize("ip", [SHOP_IP, UNLISTED_IP, FORMER_EXTRA_IP])
def test_activation_gets_through_from_any_ip(client, branch_backend, ip):
    resp = activate(client, ip)
    assert resp.status_code == 200
    assert resp.get_json() == {"message": "User activated successfully"}


@pytest.mark.parametrize("status,body", [
    (401, {"error": "Invalid activation code", "detail": None}),
    (404, {"error": "User not found", "detail": None}),
    (409, {"error": "device_key_exists", "detail": "held"}),
])
def test_activation_answers_pass_through_unchanged(client, backend, status, body):
    backend.answer("POST", "/auth/activate", status, body)
    resp = activate(client, UNLISTED_IP)
    assert resp.status_code == status
    assert resp.get_json() == body


def test_the_device_key_reaches_the_client_and_is_never_logged(client, backend, caplog):
    backend.answer("POST", "/auth/activate", 200, {"message": "User activated successfully", "device_key": KEY})
    resp = activate(client, UNLISTED_IP)
    assert resp.get_json()["device_key"] == KEY
    assert KEY not in caplog.text


@pytest.mark.parametrize("status", [401, 404, 409])
def test_six_failures_for_one_serial_are_throttled(client, backend, status):
    backend.answer("POST", "/auth/activate", status, {"error": "x"})
    for i in range(5):  # a different IP each time: only the serial ties them
        assert activate_as(client, f"198.51.100.{i}").status_code == status
    calls = len(backend.calls)
    resp = activate_as(client, "198.51.100.99")
    assert resp.status_code == 429
    assert resp.get_json() == {"error": "too_many_attempts"}
    assert len(backend.calls) == calls  # refused without calling the backend


def test_six_failures_from_one_ip_are_throttled(client, backend):
    backend.answer("POST", "/auth/activate", 401, {"error": "x"})
    for i in range(5):  # a different serial each time: only the IP ties them
        assert activate_as(client, UNLISTED_IP, serial=f"SN{i}").status_code == 401
    assert activate_as(client, UNLISTED_IP, serial="SN-new").status_code == 429
    assert activate_as(client, "198.51.100.7", serial="SN-new").status_code == 401  # other IP, fresh serial


def test_a_refusal_without_a_serial_still_counts_against_the_ip(client, backend):
    backend.answer("POST", "/auth/activate", 404, {"error": "User not found"})
    for _ in range(5):
        client.post("/ai-camera/api/auth/activate", **from_ip(UNLISTED_IP), data=b"not json",
                    content_type="application/json")
    assert activate_as(client, UNLISTED_IP).status_code == 429


@pytest.mark.parametrize("status", [400, 422, 500, 502, 503])
def test_outages_and_malformed_requests_do_not_count(client, backend, status):
    # A 5xx is an outage, not a guess: it must not lock a Branch out after recovery.
    backend.answer("POST", "/auth/activate", status, {"error": "x"})
    for _ in range(10):
        assert activate_as(client, UNLISTED_IP).status_code == status


def test_an_unreachable_backend_does_not_count(client, backend):
    # No canned answer: the proxy's own 503 "Activation service unavailable".
    for _ in range(10):
        assert activate_as(client, UNLISTED_IP).status_code == 503


def test_successes_do_not_count(client, branch_backend):
    for _ in range(10):
        assert activate(client, UNLISTED_IP).status_code == 200


def test_the_throttle_lifts_after_an_hour(client, backend, fresh_throttle):
    backend.answer("POST", "/auth/activate", 401, {"error": "x"})
    for _ in range(5):
        activate_as(client, UNLISTED_IP)
    assert activate_as(client, UNLISTED_IP).status_code == 429
    fresh_throttle[0] += 3601
    assert activate_as(client, UNLISTED_IP).status_code == 401


def test_expired_failures_are_swept(client, backend, fresh_throttle, monkeypatch):
    import gate
    monkeypatch.setattr(gate, "_SWEEP_ABOVE", 3)
    backend.answer("POST", "/auth/activate", 401, {"error": "x"})
    for i in range(4):
        activate_as(client, f"198.51.100.{i}", serial=f"SN{i}")
    fresh_throttle[0] += 3601
    activate_as(client, UNLISTED_IP, serial="SN-late")
    assert set(gate._activation_failures) == {"ip:" + UNLISTED_IP, "serial:SN-late"}


# --- client IP: first X-Forwarded-For entry, else the remote address (ADR 0007) --------
def test_client_ip_is_the_first_forwarded_for_entry(client, branch_backend):
    via_proxy = {"environ_base": {"REMOTE_ADDR": "127.0.0.1"}}
    office = client.get("/ai-camera/landing", headers={"X-Forwarded-For": f"{OFFICE_IP}, {SHOP_IP}"}, **via_proxy)
    shop = client.get("/ai-camera/landing", headers={"X-Forwarded-For": f"{SHOP_IP}, {OFFICE_IP}"}, **via_proxy)
    assert office.status_code == 200
    assert shop.status_code == 403


def test_client_ip_falls_back_to_the_remote_address(client, branch_backend):
    assert client.get("/ai-camera/landing", **from_ip(OFFICE_IP)).status_code == 200
    assert client.get("/ai-camera/landing", **from_ip(SHOP_IP)).status_code == 403


# --- Branch socket: the connect `auth` decides (Flask-SocketIO's test client) ----------
IDENTIFIED = [{"name": "identified", "args": [{"status": "ok", "client_id": CODE, "assigned_version": "v1"}],
               "namespace": "/"}]
DEFAULT_REFUSAL = {"message": "Connection rejected by server"}  # Socket.IO's own, no contract message


@pytest.fixture
def socket_backend(backend):
    """What identify calls besides verify-device, which each test cans itself."""
    backend.answer("GET", "/branches", 200, [{"activation_code": CODE, "assigned_version": "v1"}])
    backend.answer("POST", f"/branches/{CODE}/identify", 200, {})
    backend.answer("PATCH", "/users/online", 200, {})
    return backend


@pytest.fixture
def branch_socket(socket_backend):
    """Connects a Branch socket with the given `auth`; every one is closed afterwards."""
    opened = []

    def connect(auth=None):
        sio = socketio.test_client(app, auth=auth)
        opened.append(sio)
        return sio

    yield connect
    for sio in opened:
        if sio.is_connected():
            sio.disconnect()


@pytest.fixture
def refusals(monkeypatch):
    """The data of every connect_error the server sends, i.e. what the client's
    connect_error handler receives: {"message": <reason>}."""
    sent = []
    make = socketio.server.packet_class

    def packet_class(*args, **kwargs):
        pkt = make(*args, **kwargs)
        if pkt.packet_type == sio_packet.CONNECT_ERROR:
            sent.append(pkt.data)
        return pkt

    monkeypatch.setattr(socketio.server, "packet_class", packet_class)
    return sent


def identify_message(activate_id: str = CODE) -> dict:
    return {"command": "identify", "info": {"activateID": activate_id, "version": "v1"}}


def identify(sio, activate_id: str = CODE):
    sio.emit("identify", identify_message(activate_id))


HANG_UP = sio_packet.Packet(sio_packet.DISCONNECT)


def sent_meanwhile(*packets):
    """For FakeBackend.answer(during=...): the Branch sends `packets` while the
    handler that made the request still waits for its answer. Production runs
    Socket.IO handlers concurrently; the test client runs them one at a time, so
    the packets go in from inside that wait, the way the test client's emit does."""
    def send():
        eio_sid = socketio.server.manager.eio_sid_from_sid(request.sid, "/")
        for pkt in packets:
            socketio.server._handle_eio_message(eio_sid, pkt.encode())
    return send


def test_socket_with_a_valid_key_is_accepted_and_identifies(branch_socket, device_backend, refusals, caplog):
    sio = branch_socket({"activation_code": CODE, "device_key": KEY})
    assert sio.is_connected() and refusals == []
    identify(sio)
    assert sio.get_received() == IDENTIFIED
    assert KEY not in caplog.text


@pytest.mark.parametrize("auth", [
    {"activation_code": CODE, "device_key": "wrong-key"},
    {"activation_code": OTHER_CODE, "device_key": KEY},
    {"device_key": KEY},
], ids=["wrong key", "another Branch's code", "key without a code"])
def test_socket_with_a_wrong_key_or_mismatched_pair_is_refused(branch_socket, device_backend, refusals, auth, caplog):
    assert not branch_socket(auth).is_connected()
    assert refusals == [{"message": "invalid_device_key"}]
    assert KEY not in caplog.text


@pytest.mark.parametrize("verdict", ["invalid_device_key", "no_key", "unknown_activation_code"])
def test_socket_with_a_key_and_any_verdict_but_ok_is_refused_invalid_device_key(
        branch_socket, socket_backend, refusals, verdict):
    # no_key: a key presented for a Branch whose key an admin cleared.
    socket_backend.answer("POST", "/auth/verify-device", 200, {"verdict": verdict})
    assert not branch_socket({"activation_code": CODE, "device_key": KEY}).is_connected()
    assert refusals == [{"message": "invalid_device_key"}]


@pytest.mark.parametrize("auth", [{"activation_code": CODE}, {"activation_code": CODE, "device_key": ""}],
                         ids=["no key", "empty key"])
@pytest.mark.parametrize("verdict, refusal", [
    ("no_key", None),
    ("invalid_device_key", "invalid_device_key"),  # the Branch holds a key and must present it
    ("unknown_activation_code", "unknown_activation_code"),
])
def test_socket_with_a_code_and_no_key(branch_socket, socket_backend, refusals, auth, verdict, refusal):
    socket_backend.answer("POST", "/auth/verify-device", 200, {"verdict": verdict})
    sio = branch_socket(auth)
    if refusal is None:
        assert sio.is_connected() and refusals == []
        identify(sio)
        assert sio.get_received() == IDENTIFIED
    else:
        assert not sio.is_connected()
        assert refusals == [{"message": refusal}]


@pytest.mark.parametrize("auth", [None, {}, {"activation_code": None}], ids=["no auth", "empty auth", "no code"])
def test_socket_without_auth_is_accepted_provisionally_and_identify_checks_the_code(
        branch_socket, socket_backend, refusals, auth):
    # The old client: no auth at all. Its code is judged at identify instead.
    socket_backend.answer("POST", "/auth/verify-device", 200, {"verdict": "no_key"})
    sio = branch_socket(auth)
    assert sio.is_connected() and refusals == []
    assert ("POST", "/auth/verify-device") not in socket_backend.calls
    identify(sio)
    assert sio.get_received() == IDENTIFIED
    assert sio.is_connected()


@pytest.mark.parametrize("verdict", ["invalid_device_key", "unknown_activation_code", "ok"])
def test_a_provisional_socket_is_disconnected_at_identify_unless_its_code_has_no_key(
        branch_socket, socket_backend, verdict):
    # invalid_device_key: the Branch holds a key.
    socket_backend.answer("POST", "/auth/verify-device", 200, {"verdict": verdict})
    sio = branch_socket()
    identify(sio)
    assert not sio.is_connected()
    assert ("POST", f"/branches/{CODE}/identify") not in socket_backend.calls  # never reported online


NO_VERDICT = [
    None,  # unreachable
    {"raises": requests.exceptions.Timeout("read timed out")},
    {"status": 500, "body": {"error": "Failed to verify device", "detail": "pool exhausted"}},
    {"status": 200, "body": {"verdict": "maybe"}},
]
NO_VERDICT_IDS = ["unreachable", "timeout", "500", "unknown verdict"]


@pytest.mark.parametrize("canned", NO_VERDICT, ids=NO_VERDICT_IDS)
@pytest.mark.parametrize("auth", [None, {"activation_code": CODE}], ids=["provisional", "keyless"])
def test_a_keyless_identify_is_let_through_while_the_backend_gives_no_verdict(
        branch_socket, socket_backend, auth, canned, caplog):
    # The client never reconnects after the server disconnects it, so refusing
    # here left a Branch offline until someone restarted it (prod, 2026-09-25).
    # Connect needs no_key once, then identify runs into the outage.
    socket_backend.answer("POST", "/auth/verify-device", 200, {"verdict": "no_key"})
    sio = branch_socket(auth)
    socket_backend.answers.pop(("POST", "/auth/verify-device", None))
    if canned is not None:
        socket_backend.answer("POST", "/auth/verify-device", **canned)
    identify(sio)
    assert sio.get_received() == IDENTIFIED
    assert f"allowed keyless identify as {CODE}" in caplog.text


@pytest.mark.parametrize("keyed, switch, verdict, refusal", [
    (False, False, None, None),   # keyless, backend down, switch off: as before the Device Key
    (False, True, None, "no device key while REQUIRE_DEVICE_KEY is on"),  # keyless, switch on: never
    (True, False, None, None),    # keyed: proved at connect, identify never asks the backend
    (True, True, None, None),
    (False, False, "invalid_device_key", "keyless, verify-device said invalid_device_key"),
    (False, False, "unknown_activation_code", "keyless, verify-device said unknown_activation_code"),
    (False, False, "no_key", None),
])
def test_socket_identify_refusal(monkeypatch, keyed, switch, verdict, refusal):
    from gate import socket_identify_refusal
    asked = []
    monkeypatch.setattr("gate.verify_device", lambda code: asked.append(code) or verdict)
    monkeypatch.setattr(Config, "REQUIRE_DEVICE_KEY", switch)
    with app.app_context():
        assert socket_identify_refusal(CODE, keyed, CODE) == refusal
    assert asked == ([] if keyed or switch else [CODE])


def test_socket_identify_refusal_names_the_code_the_connection_proved():
    from gate import socket_identify_refusal
    with app.app_context():
        assert socket_identify_refusal(CODE, True, "ADV-OTHER") == f"connected as {CODE}"


@pytest.mark.parametrize("auth", [{"activation_code": CODE}, None], ids=["keyless", "provisional"])
def test_a_keyless_socket_is_disconnected_at_identify_once_its_branch_holds_a_key(branch_socket, socket_backend, auth):
    # Otherwise a stranger let in before the Branch activated could re-identify
    # and take over the keyed Branch's connection and its Distributions.
    keyless = {"activation_code": CODE, "device_key": None}
    socket_backend.answer("POST", "/auth/verify-device", 200, {"verdict": "no_key"}, when=keyless)
    socket_backend.answer("POST", f"/branches/{CODE}/force-update", 200, {})
    stranger = branch_socket(auth)
    identify(stranger)
    assert stranger.get_received() == IDENTIFIED
    # The Branch activates and receives KEY.
    socket_backend.answer("POST", "/auth/verify-device", 200, {"verdict": "invalid_device_key"}, when=keyless)
    socket_backend.answer("POST", "/auth/verify-device", 200, {"verdict": "ok"},
                          when={"activation_code": CODE, "device_key": KEY})
    branch = branch_socket({"activation_code": CODE, "device_key": KEY})
    identify(branch)
    identify(stranger)
    assert not stranger.is_connected()
    branch.emit("request_update", {})
    assert ("POST", f"/branches/{CODE}/force-update") in socket_backend.calls  # still the Branch's connection


@pytest.mark.parametrize("auth", [{"activation_code": CODE, "device_key": KEY}, {"activation_code": CODE}],
                         ids=["with key", "keyless"])
def test_identify_as_another_branch_after_connect_is_disconnected(branch_socket, socket_backend, auth):
    # Would otherwise take over OTHER_CODE's connection and receive its Distributions.
    socket_backend.answer("POST", "/auth/verify-device", 200, {"verdict": "ok" if "device_key" in auth else "no_key"})
    sio = branch_socket(auth)
    assert sio.is_connected()
    identify(sio, OTHER_CODE)
    assert not sio.is_connected()


@pytest.mark.parametrize("canned", [
    None,
    {"raises": requests.exceptions.Timeout("read timed out")},
    {"status": 500, "body": {"error": "Failed to verify device", "detail": "db down"}},
    {"status": 422, "body": ECHOED_422},
    {"status": 404, "body": {"verdict": "ok"}},
    {"status": 200, "body": {}},
    {"status": 200, "body": {"verdict": "maybe"}},
    {"status": 200, "body": ["ok"]},
    {"status": 200, "body": b"<html>502 Bad Gateway</html>"},
], ids=["unreachable", "timeout", "500", "422 echo", "non-200 verdict", "no verdict",
        "unknown verdict", "not an object", "not json"])
@pytest.mark.parametrize("auth", [{"activation_code": CODE, "device_key": KEY}, {"activation_code": CODE}],
                         ids=["with key", "keyless"])
def test_socket_while_the_backend_gives_no_verdict_gets_the_default_refusal(
        branch_socket, socket_backend, refusals, auth, canned, caplog):
    # Never a contract message: those send the client to re-activate or to stop.
    if canned is not None:
        socket_backend.answer("POST", "/auth/verify-device", **canned)
    assert not branch_socket(auth).is_connected()
    assert refusals == [DEFAULT_REFUSAL]
    assert KEY not in caplog.text


@pytest.mark.parametrize("auth", [{"activation_code": CODE, "device_key": KEY}, None], ids=["with key", "provisional"])
def test_manifest_and_request_update_act_only_once_identified(branch_socket, device_backend, auth):
    device_backend.answer("POST", "/auth/verify-device", 200, {"verdict": "no_key"},
                          when={"activation_code": CODE, "device_key": None})
    device_backend.answer("POST", f"/branches/{CODE}/force-update", 200, {})
    sio = branch_socket(auth)
    sio.emit("manifest", {"a.py": "hash"})
    sio.emit("request_update", {})
    assert sio.get_received() == []
    assert ("POST", f"/branches/{CODE}/force-update") not in device_backend.calls
    identify(sio)
    sio.emit("request_update", {})
    assert ("POST", f"/branches/{CODE}/force-update") in device_backend.calls


def test_a_provisional_socket_stays_the_branch_it_first_identified_as(branch_socket, socket_backend):
    # Otherwise one anonymous socket could hold several keyless Branches' connections.
    socket_backend.answer("POST", "/auth/verify-device", 200, {"verdict": "no_key"})
    sio = branch_socket()
    identify(sio)
    assert sio.get_received() == IDENTIFIED
    identify(sio, OTHER_CODE)
    assert not sio.is_connected()


@pytest.mark.parametrize("key, verdict", [(KEY, "ok"), ("wrong-key", "invalid_device_key")],
                         ids=["accepted", "refused"])
def test_identify_sent_while_connect_is_being_judged_is_ignored(branch_socket, socket_backend, refusals, key, verdict):
    # Judged as provisional, it would register OTHER_CODE on a socket that proved
    # CODE, or on one about to be refused.
    socket_backend.answer("POST", "/auth/verify-device", 200, {"verdict": "no_key"},
                          when={"activation_code": OTHER_CODE, "device_key": None})
    socket_backend.answer("POST", "/auth/verify-device", 200, {"verdict": verdict},
                          when={"activation_code": CODE, "device_key": key},
                          during=sent_meanwhile(sio_packet.Packet(sio_packet.EVENT,
                                                                  ["identify", identify_message(OTHER_CODE)])))
    sio = branch_socket({"activation_code": CODE, "device_key": key})
    assert ("POST", f"/branches/{OTHER_CODE}/identify") not in socket_backend.calls  # never reported online
    if verdict == "ok":
        assert sio.get_received() == []
        identify(sio)
        assert sio.get_received() == IDENTIFIED
    else:
        assert not sio.is_connected()
        assert refusals == [{"message": "invalid_device_key"}]


@pytest.mark.parametrize("auth, waits_on", [
    ({"activation_code": CODE, "device_key": KEY}, {"activation_code": CODE, "device_key": KEY}),
    (None, {"activation_code": CODE, "device_key": None}),
], ids=["at connect", "at provisional identify"])
def test_a_socket_that_hangs_up_while_verify_device_runs_leaves_nothing_behind(
        branch_socket, socket_backend, auth, waits_on):
    socket_backend.answer("POST", "/auth/verify-device", 200, {"verdict": "ok" if auth else "no_key"},
                          when=waits_on, during=sent_meanwhile(HANG_UP))
    remembered = len(socket_handlers._connect_codes)  # a leak has no outside view
    sio = branch_socket(auth)
    if auth is None:
        identify(sio)
    assert ("POST", f"/branches/{CODE}/identify") not in socket_backend.calls  # never reported online
    assert len(socket_handlers._connect_codes) == remembered


def requests_update(sio, backend) -> bool:
    """Whether the server takes `sio` for the Branch: only then does request_update act."""
    backend.calls.clear()
    sio.emit("request_update", {})
    return ("POST", f"/branches/{CODE}/force-update") in backend.calls


def test_a_branch_stays_online_when_its_newer_connection_hangs_up_first(branch_socket, device_backend):
    # UNS037, 28 Oct: two connections overlapped while it reconnected, the one that
    # identified last hung up, and the one left open was no longer the Branch's.
    device_backend.answer("POST", f"/branches/{CODE}/force-update", 200, {})
    auth = {"activation_code": CODE, "device_key": KEY}
    older, newer = branch_socket(auth), branch_socket(auth)
    identify(older)
    identify(newer)
    newer.disconnect()
    assert CODE in socket_handlers.update_clients
    assert requests_update(older, device_backend)
    older.disconnect()
    assert CODE not in socket_handlers.update_clients


def test_a_repeated_identify_is_answered_without_reporting_the_branch_again(branch_socket, device_backend):
    sio = branch_socket({"activation_code": CODE, "device_key": KEY})
    identify(sio)
    assert sio.get_received() == IDENTIFIED
    device_backend.calls.clear()
    ack = sio.emit("identify", identify_message(), callback=True)
    assert ack == {"status": "ok", "client_id": CODE, "assigned_version": "v1", "known": True}
    assert sio.get_received() == []
    assert device_backend.calls == []  # no identify report, verify-device or online PATCH


def test_a_repeat_on_a_connection_the_server_lost_registers_it_again(branch_socket, device_backend):
    sio = branch_socket({"activation_code": CODE, "device_key": KEY})
    identify(sio)
    sio.get_received()
    with socket_handlers.update_clients_lock:
        del socket_handlers.update_clients[CODE]
    ack = sio.emit("identify", identify_message(), callback=True)
    assert ack == {"status": "ok", "client_id": CODE, "assigned_version": "v1"}
    assert CODE in socket_handlers.update_clients
    assert ("PATCH", "/users/online") in device_backend.calls


# --- traffic feeds: Device Key only, passed through untouched (ADR 0009) ----------------
FEEDS = ["rate", "in-pass", "hotzone", "zone-hour"]
# Bytes, not a dict: the frontend must forward what the Branch sent, not a re-encoding of it.
FEED_BODY = b'{"branch": "51000017", "zone_type": "IT",  "note": "\u0e44\u0e17\u0e22"}'


def post_feed(client, feed: str, ip: str = UNLISTED_IP, headers=None):
    return client.post(f"/ai-camera/api/traffic/{feed}", **from_ip(ip), headers=headers,
                       data=FEED_BODY, content_type="application/json")


def backend_feed_path(feed: str) -> str:
    return f"/ai-camera-service/traffic/{feed}"


@pytest.mark.parametrize("feed", FEEDS)
def test_a_feed_with_a_valid_key_reaches_the_backend_byte_for_byte(client, device_backend, feed):
    device_backend.answer("POST", backend_feed_path(feed), 200, {"id": 7, "action": "inserted"})
    resp = post_feed(client, feed, headers=key_headers())
    assert (resp.status_code, resp.get_json()) == (200, {"id": 7, "action": "inserted"})
    method, path, data, headers = device_backend.sent[-1]
    assert (method, path, data) == ("POST", backend_feed_path(feed), FEED_BODY)
    assert headers["Content-Type"] == "application/json"


def test_a_branch_cannot_send_the_dry_run_header_through(client, device_backend):
    # Only an operator on the box may dry run: it reads any Branch's bills, whatever the body names.
    device_backend.answer("POST", backend_feed_path("in-pass"), 200, {"action": "shadow"})
    post_feed(client, "in-pass", headers={**key_headers(), "X-Traffic-Dry-Run": "1"})
    assert "X-Traffic-Dry-Run" not in device_backend.sent[-1][3]


@pytest.mark.parametrize("status, body", [
    (400, {"detail": [{"type": "missing", "loc": ["body", "walk_in"], "msg": "Field required"}]}),
    (500, {"detail": "Internal Server Error"}),
], ids=["400 malformed body", "500 nothing written"])
def test_a_feed_failure_comes_back_as_the_backend_gave_it(client, device_backend, status, body):
    device_backend.answer("POST", backend_feed_path("in-pass"), status, body)
    resp = post_feed(client, "in-pass", headers=key_headers())
    assert (resp.status_code, resp.get_json()) == (status, body)


@pytest.mark.parametrize("raises", [None, requests.exceptions.Timeout("read timed out")],
                         ids=["unreachable", "timeout"])
def test_a_feed_the_backend_cannot_take_is_503_never_200(client, device_backend, raises):
    if raises is not None:
        device_backend.answer("POST", backend_feed_path("rate"), raises=raises)
    resp = post_feed(client, "rate", headers=key_headers())
    assert (resp.status_code, resp.get_json()) == (503, {"error": "Upstream service unavailable"})


@pytest.mark.parametrize("ip", [UNLISTED_IP, SHOP_IP, OFFICE_IP, LAN_TEST_IPS[0]])
@pytest.mark.parametrize("headers", [
    None,
    key_headers(key="wrong-key"),
    key_headers(code=OTHER_CODE),
    {"X-Activation-Code": CODE},
    {"Authorization": f"Bearer {KEY}"},
], ids=["no key", "wrong key", "another Branch's code", "code only", "key only"])
def test_a_feed_without_a_valid_key_is_401_from_every_ip_and_never_forwarded(
        client, device_backend, ip, headers, caplog):
    resp = post_feed(client, "hotzone", ip=ip, headers=headers)
    assert (resp.status_code, resp.get_json()) == (401, {"error": "invalid_device_key"})
    assert ("POST", backend_feed_path("hotzone")) not in device_backend.calls
    assert KEY not in caplog.text


@pytest.mark.parametrize("headers", [None, key_headers(key="wrong-key")], ids=["no key", "wrong key"])
def test_zone_hour_without_a_valid_key_is_401_and_never_forwarded(client, device_backend, headers):
    resp = post_feed(client, "zone-hour", headers=headers)
    assert (resp.status_code, resp.get_json()) == (401, {"error": "invalid_device_key"})
    assert ("POST", backend_feed_path("zone-hour")) not in device_backend.calls


def test_zone_service_is_no_longer_a_feed(client, device_backend):
    # Replaced by zone-hour (ADR 0010). From the office IP an unknown URL is admin, so the
    # gate lets it by and Flask's own 404 shows the route itself is gone.
    resp = post_feed(client, "zone-service", ip=OFFICE_IP, headers=key_headers())
    assert resp.status_code == 404
    assert ("POST", backend_feed_path("zone-service")) not in device_backend.calls


def test_a_feed_key_whose_verdict_is_no_key_is_401(client, branch_backend):
    # no_key: a key presented for a Branch whose key an admin cleared.
    branch_backend.answer("POST", "/auth/verify-device", 200, {"verdict": "no_key"})
    assert post_feed(client, "rate", headers=key_headers()).status_code == 401


# --- Camera Placement: Device Key only, passed through untouched ------------------------
PLACEMENT_BODY = b'{"jpeg": "/9j/4AAQSkZJRg==", "camera": "192.168.20.240_ch01"}'
PLACEMENT_BACKEND = "/camera-placement-service/judge"


def post_placement(client, ip: str = UNLISTED_IP, headers=None):
    return client.post("/ai-camera/api/camera-placement", **from_ip(ip), headers=headers,
                       data=PLACEMENT_BODY, content_type="application/json")


def test_camera_placement_with_a_valid_key_reaches_the_backend_as_the_branch_it_proved(client, device_backend):
    device_backend.answer("POST", PLACEMENT_BACKEND, 200, {"answers": {"label": {"choice": "inside"}}})
    resp = post_placement(client, headers=key_headers())
    assert (resp.status_code, resp.get_json()) == (200, {"answers": {"label": {"choice": "inside"}}})
    method, path, data, headers = device_backend.sent[-1]
    assert (method, path, data) == ("POST", PLACEMENT_BACKEND, PLACEMENT_BODY)
    assert headers["X-Branch-Code"] == CODE


@pytest.mark.parametrize("status, body", [(400, {"error": "jpeg is not base64"}), (503, {"error": "jev_unavailable"})])
def test_camera_placement_failures_come_back_as_the_backend_gave_them(client, device_backend, status, body):
    device_backend.answer("POST", PLACEMENT_BACKEND, status, body)
    resp = post_placement(client, headers=key_headers())
    assert (resp.status_code, resp.get_json()) == (status, body)


@pytest.mark.parametrize("headers", [None, key_headers(key="wrong-key"), {"X-Activation-Code": CODE}],
                         ids=["no key", "wrong key", "code only"])
def test_camera_placement_without_a_valid_key_is_401_and_never_forwarded(client, device_backend, headers):
    resp = post_placement(client, ip=OFFICE_IP, headers=headers)
    assert (resp.status_code, resp.get_json()) == (401, {"error": "invalid_device_key"})
    assert ("POST", PLACEMENT_BACKEND) not in device_backend.calls


def test_camera_placement_the_backend_cannot_take_is_503(client, device_backend):
    device_backend.answer("POST", PLACEMENT_BACKEND, raises=requests.exceptions.Timeout("read timed out"))
    resp = post_placement(client, headers=key_headers())
    assert (resp.status_code, resp.get_json()) == (503, {"error": "Upstream service unavailable"})


# --- camera alert feeds: Device Key only; the Branch is the one the key proved -----------
ALERT_FEEDS = ["health", "event"]
ALERT_BODY = b'{"store_open": true, "cameras": {"cam_192_168_20_240_3": {"status": "black"}}}'


def post_alert(client, feed: str, ip: str = UNLISTED_IP, headers=None):
    return client.post(f"/ai-camera/api/camera-alert/{feed}", **from_ip(ip), headers=headers,
                       data=ALERT_BODY, content_type="application/json")


@pytest.mark.parametrize("feed", ALERT_FEEDS)
def test_an_alert_feed_with_a_valid_key_reaches_the_backend_as_that_branch(client, device_backend, feed):
    device_backend.answer("POST", f"/camera-alert-service/{feed}", 200, {"ok": True})
    # A caller naming another Branch in X-Branch-Code is ignored: the key decides.
    resp = post_alert(client, feed, headers={**key_headers(), "X-Branch-Code": OTHER_CODE})
    assert (resp.status_code, resp.get_json()) == (200, {"ok": True})
    method, path, data, headers = device_backend.sent[-1]
    assert (method, path, data) == ("POST", f"/camera-alert-service/{feed}", ALERT_BODY)
    assert headers["X-Branch-Code"] == CODE


@pytest.mark.parametrize("headers", [None, key_headers(key="wrong-key"), key_headers(code=OTHER_CODE)],
                         ids=["no key", "wrong key", "another Branch's code"])
def test_an_alert_feed_without_a_valid_key_is_401_and_never_forwarded(client, device_backend, headers):
    resp = post_alert(client, "health", ip=OFFICE_IP, headers=headers)
    assert (resp.status_code, resp.get_json()) == (401, {"error": "invalid_device_key"})
    assert ("POST", "/camera-alert-service/health") not in device_backend.calls


def test_an_alert_feed_the_backend_cannot_take_is_503(client, device_backend):
    assert post_alert(client, "event", headers=key_headers()).status_code == 503


# --- Transition switch: AI_CAMERA_ADMIN_REQUIRE_DEVICE_KEY (ticket 07) -----------------
@pytest.fixture
def switch_on(monkeypatch):
    monkeypatch.setattr(Config, "REQUIRE_DEVICE_KEY", True)


@pytest.mark.parametrize("upload", [dummy_data_upload, build_upload], ids=["dummy-data", "build"])
@pytest.mark.parametrize("ip", [SHOP_IP, OFFICE_IP, UNLISTED_IP])
def test_switch_on_refuses_keyless_branch_rest(client, branch_backend, switch_on, upload, ip):
    resp = upload(client, ip)
    assert resp.status_code == 401
    assert resp.get_json() == {"error": "invalid_device_key"}


@pytest.mark.parametrize("upload", [dummy_data_upload, build_upload], ids=["dummy-data", "build"])
def test_switch_on_still_passes_a_valid_key(client, branch_backend, switch_on, upload):
    branch_backend.answer("POST", "/auth/verify-device", 200, {"verdict": "ok"})
    headers = {"X-Activation-Code": CODE, "Authorization": f"Bearer {KEY}"}
    assert upload(client, UNLISTED_IP, headers).status_code == 200


@pytest.mark.parametrize("auth", [None, {"activation_code": CODE}, {"activation_code": CODE, "device_key": ""}],
                         ids=["no auth", "code only", "empty key"])
def test_switch_on_refuses_a_keyless_socket(branch_socket, socket_backend, refusals, switch_on, auth):
    socket_backend.answer("POST", "/auth/verify-device", 200, {"verdict": "no_key"})
    assert not branch_socket(auth).is_connected()
    assert refusals == [{"message": "invalid_device_key"}]


def test_switch_on_still_accepts_a_keyed_socket(branch_socket, socket_backend, refusals, switch_on):
    socket_backend.answer("POST", "/auth/verify-device", 200, {"verdict": "ok"})
    sio = branch_socket({"activation_code": CODE, "device_key": KEY})
    assert sio.is_connected() and refusals == []
    identify(sio)
    assert sio.get_received() == IDENTIFIED


def test_a_keyless_upload_passed_by_ip_is_logged_for_the_readiness_check(client, branch_backend, caplog):
    assert dummy_data_upload(client, SHOP_IP).status_code == 200
    assert f"Keyless branch_rest request passed by IP: {SHOP_IP}" in caplog.text


def test_switch_on_leaves_activation_alone(client, branch_backend, switch_on):
    assert activate(client, UNLISTED_IP).status_code == 200


def test_switch_is_read_from_the_environment():
    import importlib
    import config as config_module
    try:
        for value, expected in [("1", True), ("true", True), ("ON", True), ("", False), ("0", False), ("no", False)]:
            os.environ["AI_CAMERA_ADMIN_REQUIRE_DEVICE_KEY"] = value
            assert importlib.reload(config_module).Config.REQUIRE_DEVICE_KEY is expected
    finally:
        os.environ.pop("AI_CAMERA_ADMIN_REQUIRE_DEVICE_KEY", None)
        importlib.reload(config_module)


def test_switch_is_read_from_frontend_env_file_unless_the_process_env_sets_it():
    import importlib
    import config as config_module
    env_file = os.path.join(os.path.dirname(config_module.__file__), ".env")
    if os.path.exists(env_file):
        pytest.skip("frontend/.env exists here; not touching it")
    outer = os.environ.pop("AI_CAMERA_ADMIN_REQUIRE_DEVICE_KEY", None)
    try:
        with open(env_file, "w") as f:
            f.write("# AI Camera Admin frontend\nOTHER=1\nAI_CAMERA_ADMIN_REQUIRE_DEVICE_KEY = 1\t# on 30 Oct\n")
        assert importlib.reload(config_module).Config.REQUIRE_DEVICE_KEY is True
        os.environ["AI_CAMERA_ADMIN_REQUIRE_DEVICE_KEY"] = ""  # set but empty: the file still counts
        assert importlib.reload(config_module).Config.REQUIRE_DEVICE_KEY is True
        os.environ["AI_CAMERA_ADMIN_REQUIRE_DEVICE_KEY"] = "0"  # set: the process env wins
        assert importlib.reload(config_module).Config.REQUIRE_DEVICE_KEY is False
    finally:
        os.environ.pop("AI_CAMERA_ADMIN_REQUIRE_DEVICE_KEY", None)
        if outer is not None:
            os.environ["AI_CAMERA_ADMIN_REQUIRE_DEVICE_KEY"] = outer
        os.remove(env_file)
        importlib.reload(config_module)
