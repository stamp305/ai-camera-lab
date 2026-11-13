"""Device Key contract gaps (ticket 21): the key is saved before the accepted MAC,
and a pause of key requests reaches the menu bar as a "contact admin" line."""
import ast
import json
import os
import sys
import types

import pytest
from cryptography.fernet import Fernet

import _fake_mariadb
import device_identity

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CODE = "ADV-1111-2222-3333"
KEY = "k" * 43


class _Response:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.content = json.dumps(body).encode()
        self.text = self.content.decode()

    def json(self):
        return self._body


@pytest.fixture
def identity(tmp_path, monkeypatch):
    """device_identity on a temp store and a fake Keychain; returns the Keychain dict."""
    keychain = {}
    fake = types.ModuleType("keyring")
    fake.get_password = lambda s, a: keychain.get((s, a))
    fake.set_password = lambda s, a, v: keychain.__setitem__((s, a), v)
    fake.delete_password = lambda s, a: keychain.pop((s, a))
    errors = types.ModuleType("keyring.errors")
    errors.PasswordDeleteError = KeyError
    monkeypatch.setitem(sys.modules, "keyring", fake)
    monkeypatch.setitem(sys.modules, "keyring.errors", errors)
    monkeypatch.setenv("AICAMERA_APP_SUPPORT_DIR", str(tmp_path))
    cipher = Fernet(Fernet.generate_key())
    monkeypatch.setattr(device_identity, "_cipher", lambda: cipher)
    monkeypatch.setattr(device_identity, "hardware_serial", lambda: "SERIAL")
    monkeypatch.setattr(device_identity, "hardware_mac", lambda: "aa:aa:aa:aa:aa:aa")
    return keychain


@pytest.fixture
def activate(identity, monkeypatch):
    """validate_user_credentials with the admin answering 200 + a key, and every
    write in order: ("key", ...) for the Keychain, ("mac", ...) for the MAC."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(sys.modules, "mariadb", _fake_mariadb.install({}))
        import data_service.utils.validate_user_credentials as v
    writes = []
    real_save = device_identity.save_device_key
    monkeypatch.setattr(device_identity, "save_device_key", lambda k: (writes.append(("key", k)), real_save(k)))

    def failing_mac_write(mac):
        writes.append(("mac", mac))
        raise OSError("disk full")
    monkeypatch.setattr(device_identity, "remember_registered_mac", failing_mac_write)
    monkeypatch.setattr(v, "_activation_macs", lambda: ["bb:bb:bb:bb:bb:bb"])
    monkeypatch.setattr(v, "_post_activate_once",
                        lambda code, mac: (_Response(200, {"message": "ok", "device_key": KEY}), None))
    monkeypatch.setattr(v, "update_activated_status", lambda **kw: None)
    monkeypatch.setattr(v.requests, "post", lambda *a, **kw: None)  # the local success callback
    return v, writes


def test_activation_saves_the_key_first_and_a_failed_mac_write_keeps_it(activate, identity):
    v, writes = activate
    result, error = v.validate_activation_code(CODE)
    assert result == {"message": "Account activated successfully"} and error is None
    assert writes == [("key", KEY), ("mac", "bb:bb:bb:bb:bb:bb")]
    assert device_identity.device_key() == KEY
    assert device_identity.is_activated() and device_identity.activation_code() == CODE


def test_key_refresh_saves_the_key_first_and_a_failed_mac_write_keeps_it(activate, identity):
    v, writes = activate
    device_identity.update(activateID=CODE, activated=True)
    assert v.refresh_device_key() == "ok"
    assert writes == [("key", KEY), ("mac", "bb:bb:bb:bb:bb:bb")]
    assert device_identity.device_key() == KEY


def _status_writer(halt):
    """client_logic._write_app_status_forever itself, without importing client_logic;
    returns the status file it writes on its first round."""
    with open(os.path.join(ROOT, "client_logic.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_write_app_status_forever")

    class _Done(Exception):
        pass

    def stop(_seconds):
        raise _Done
    import re
    import time
    ns = {
        "os": os, "re": re, "time": types.SimpleNamespace(time=time.time, sleep=stop),
        "is_activated": lambda: True, "_sio": None, "_installed_version": lambda: "1.0",
        "device_identity": types.SimpleNamespace(key_requests_halted=lambda: halt),
        "port_store": types.SimpleNamespace(get_config=lambda: {"API_PORTS": {}}),
        "_listening_ports": set,
    }
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "client_logic.py", "exec"), ns)
    return ns["_write_app_status_forever"], _Done


@pytest.mark.parametrize("halt,shown", [
    (None, None),
    ("device_key_exists", "device_key_exists"),
    ("unknown_activation_code", "unknown_activation_code"),
    (f"bad code {CODE}", "refused"),  # server text that is not a plain error code
    (KEY, "refused"),
])
def test_status_file_carries_the_pause_reason_and_never_a_secret(halt, shown, tmp_path, monkeypatch):
    import runtime_paths
    status_file = tmp_path / "app_status"
    monkeypatch.setattr(runtime_paths, "APP_STATUS_FILE", str(status_file))
    monkeypatch.setattr(runtime_paths, "ensure_run_dir", lambda: None)
    writer, done = _status_writer(halt)
    with pytest.raises(done):
        writer()
    text = status_file.read_text()
    assert json.loads(text).get("key_halt") == shown
    assert CODE not in text and KEY not in text


@pytest.fixture
def menubar(monkeypatch):
    """menubar_app with pystray and PIL stubbed (not installed for tests)."""
    pystray = types.ModuleType("pystray")
    pystray.Menu = type("Menu", (), {"SEPARATOR": None, "__init__": lambda self, *items: setattr(self, "items", items)})
    pystray.MenuItem = lambda text, action, **kw: types.SimpleNamespace(text=text, **kw)
    pil = types.ModuleType("PIL")
    pil.Image = pil.ImageDraw = None
    for name, mod in (("pystray", pystray), ("PIL", pil)):
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.delitem(sys.modules, "menubar_app", raising=False)
    import menubar_app
    return menubar_app


def _status(**extra):
    import time
    return {"time": time.time(), "activated": True, "server": False, "version": "1.0",
            "services_total": 2, "services_down": [], **extra}


def test_menu_shows_contact_admin_only_while_paused(menubar):
    colour, lines = menubar.summarize(_status())
    assert not any("admin" in line for line in lines)
    colour, lines = menubar.summarize(_status(key_halt="device_key_exists"))
    assert lines[-1] == "Contact admin: key requests paused (device_key_exists)"
    assert colour == menubar._RED


def test_menu_has_room_for_every_status_line(menubar):
    colour, menubar._State.lines = menubar.summarize(_status(activated=False, key_halt="rejected"))
    assert len(menubar._State.lines) == 4
    shown = [i for i in menubar._build_menu().items if getattr(i, "visible", None) and i.visible(i)]
    assert [i.text(i) for i in shown] == menubar._State.lines


MACS = ["m1", "m2", "m3"]


def _admin(v, monkeypatch, answers):
    """The admin answers each MAC offered in turn; returns (MACs tried, MACs remembered)."""
    tried, remembered = [], []
    replies = iter(answers)
    monkeypatch.setattr(v, "_activation_macs", lambda: list(MACS))
    monkeypatch.setattr(v, "_post_activate_once", lambda code, mac: (tried.append(mac), (next(replies), None))[1])
    monkeypatch.setattr(device_identity, "remember_registered_mac", remembered.append)
    return tried, remembered


def _mismatch():
    return _Response(401, {"error": "mac_mismatch"})


@pytest.mark.parametrize("via", ["activate", "refresh"])
def test_mismatched_macs_are_skipped_and_only_the_accepted_one_is_remembered(via, activate, monkeypatch):
    v, _ = activate
    tried, remembered = _admin(v, monkeypatch, [_mismatch(), _mismatch(),
                                                _Response(200, {"message": "ok", "device_key": KEY})])
    if via == "activate":
        assert v.validate_activation_code(CODE)[1] is None
    else:
        device_identity.update(activateID=CODE, activated=True)
        assert v.refresh_device_key() == "ok"
    assert tried == MACS and remembered == ["m3"]
    assert device_identity.device_key() == KEY


def test_key_refresh_200_without_a_key_still_remembers_the_mac_and_retries(activate, monkeypatch):
    v, _ = activate
    tried, remembered = _admin(v, monkeypatch, [_mismatch(), _Response(200, {"message": "ok"})])
    device_identity.update(activateID=CODE, activated=True)
    assert v.refresh_device_key() == "retry"
    assert tried == ["m1", "m2"] and remembered == ["m2"]
    assert device_identity.device_key() is None
