"""Siren setup + LAN check on the branch, as the company client ships them."""
import http.client
import json
import socket
import types

import pytest

import lan_probe
from siren_setup import app as S


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "siren" / "monitor_siren.json"
    monkeypatch.setattr(S, "STORE", str(path))
    return path


@pytest.fixture
def page(store, monkeypatch):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setattr(S, "PORT", port)
    monkeypatch.setattr(S, "ALLOWED_HOSTS", {f"127.0.0.1:{port}"})
    monkeypatch.setattr(S, "_server", None)
    assert S.start()
    yield port
    S._server.shutdown()


def call(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    h = {"Host": f"127.0.0.1:{port}", "Content-Type": "application/json", **(headers or {})}
    c.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=h)
    r = c.getresponse()
    return r.status, json.loads(r.read() or b"{}")


def test_the_key_file_is_private_and_the_page_never_gets_a_key(page, store):
    S.write_config({"devices": [{"id": "abcdef123456", "key": "SECRETKEY0123456", "name": "s"}], "volume": "mute"})
    assert oct(store.stat().st_mode & 0o777) == "0o600"
    assert oct(store.parent.stat().st_mode & 0o777) == "0o700"
    status, body = call(page, "GET", "/api/state")
    assert status == 200 and "SECRETKEY" not in json.dumps(body) and "abcdef123456" not in json.dumps(body)
    assert body["volume"] == "mute"


def test_another_host_or_origin_is_refused(page):
    assert call(page, "GET", "/api/state", headers={"Host": "evil.example"})[0] == 403
    assert call(page, "GET", "/api/state", headers={"Origin": "http://evil.example"})[0] == 403


def test_a_change_needs_the_setup_header(page):
    assert call(page, "POST", "/api/settings", {"volume": "low"})[0] == 403
    status, body = call(page, "POST", "/api/settings", {"volume": "low"}, {"X-Siren-Setup": "1"})
    assert status == 200 and body["ok"] is False          # nothing paired yet


def test_qr_uses_the_code_the_admin_sent(page, monkeypatch):
    asked = []

    class Login:
        def qr_code(self, client, schema, code):
            asked.append(code)
            return {"success": True, "result": {"qrcode": "tok"}}
    monkeypatch.setattr(S, "_tuya", lambda: (Login, None))
    monkeypatch.setattr(S, "USER_CODE", "")
    status, body = call(page, "POST", "/api/qr", {"user_code": "abc123XYZ"}, {"X-Siren-Setup": "1"})
    assert status == 200 and body["ok"] and body["png"].startswith("data:image/png;base64,")
    assert asked == ["abc123XYZ"]
    status, body = call(page, "POST", "/api/qr", {"user_code": "bad code!"}, {"X-Siren-Setup": "1"})
    assert body["ok"] is False                             # a malformed code is not used


def test_a_lan_ticket_works_once_from_its_origin_and_store_only(monkeypatch):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setattr(lan_probe, "PORT", port)
    lan_probe._started.clear()
    assert lan_probe.start(lambda: "42")
    t = "t" * 40
    origin = "https://portal.example"
    assert lan_probe.add_ticket({"ticket": t, "origin": origin})["ok"]

    def post(ticket, code, org):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("POST", "/lan-check", body=json.dumps({"ticket": ticket, "store_code": code}),
                  headers={"Origin": org, "Content-Type": "application/json"})
        r = c.getresponse()
        return r.status
    assert post(t, "42", "https://other.example") == 403   # wrong origin uses it up
    assert lan_probe.add_ticket({"ticket": t, "origin": origin})["ok"]
    assert post(t, "41", origin) == 403                     # not this store
    assert lan_probe.add_ticket({"ticket": t, "origin": origin})["ok"]
    assert post(t, "42", origin) == 200
    assert post(t, "42", origin) == 403                     # single use
