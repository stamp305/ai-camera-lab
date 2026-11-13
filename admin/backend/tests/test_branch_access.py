"""Seam 1: backend Branch Access (ADR 0007).

Drives POST /auth/activate, POST /auth/verify-device, GET /users and
GET /branches through FastAPI's TestClient on the production app, the way the
frontend calls the backend. The users table is an in-memory SQLite handed to
the crud modules in place of the MySQL pool, so their own SQL runs and nothing
opens a MySQL connection. The branches table is an in-memory SQLite engine.

Run (from ai-camera-admin/backend):  python -m pytest tests -q
"""
from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import sys

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # backend/

from app_ai_camera_backend import fastapi_app  # no DB connection or thread at import
from data_service.core.database import branch_db, session
from data_service.core.database.crud import auth_crud, user_crud
from data_service.core.services import auth_service
from data_service.core.utils import user_util


class FakeUsers:
    """The users table in an in-memory SQLite, standing in for a pooled MySQL connection.

    Implements only what the crud modules call: cursor(dictionary=...),
    commit() and close().
    """

    def __init__(self):
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.db.execute(
            "CREATE TABLE users (activation_code TEXT, serial_number TEXT, mac_address TEXT,"
            " group_id INTEGER, package_id INTEGER, version TEXT, branch_name TEXT,"
            " is_online INTEGER DEFAULT 0, is_activated INTEGER DEFAULT 0, start_date TEXT,"
            " last_update TEXT, device_key_hash CHAR(64))"
        )

    def add(self, name: str, **cols) -> None:
        cols = {"activation_code": code(name), "serial_number": f"SN-{name}",
                "mac_address": f"MAC-{name}", "group_id": 1, "branch_name": name, **cols}
        self.db.execute(f"INSERT INTO users ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                        list(cols.values()))

    def set(self, name: str, **cols) -> None:
        """What an admin runs by hand, e.g. UPDATE users SET device_key_hash = NULL."""
        self.db.execute(f"UPDATE users SET {', '.join(c + ' = ?' for c in cols)} WHERE serial_number = ?",
                        [*cols.values(), f"SN-{name}"])

    def row(self, name: str) -> dict:
        cursor = self.cursor(dictionary=True)
        cursor.execute("SELECT * FROM users WHERE serial_number = %s", (f"SN-{name}",))
        return cursor.fetchone()

    def cursor(self, dictionary=False):
        return FakeCursor(self.db.cursor(), dictionary)

    def commit(self):
        self.db.commit()

    def close(self):
        pass


class FakeCursor:
    def __init__(self, cursor, dictionary):
        self.cursor = cursor
        self.dictionary = dictionary

    def execute(self, sql, params=()):
        # MySQL dialect to SQLite: placeholders and NOW() are all the crud SQL needs.
        self.cursor.execute(sql.replace("%s", "?").replace("NOW()", "CURRENT_TIMESTAMP"), params)

    @property
    def rowcount(self):
        return self.cursor.rowcount

    def fetchone(self):
        return self._row(self.cursor.fetchone())

    def fetchall(self):
        return [self._row(row) for row in self.cursor.fetchall()]

    def _row(self, row):
        if row is None or not self.dictionary:
            return row
        return dict(zip([d[0] for d in self.cursor.description], row))

    def close(self):
        self.cursor.close()


def code(name: str) -> str:
    return f"ADV-{name.upper():0>4}-0000-0001"


@pytest.fixture
def db(monkeypatch):
    fake = FakeUsers()
    for crud in (auth_crud, user_crud):
        monkeypatch.setattr(crud, "get_db", lambda: fake)
    monkeypatch.setattr(user_util, "read_all_groups", lambda: [{"id": 1, "name": "shops"}])
    monkeypatch.setattr(user_util, "read_all_packages", lambda: [])
    # GET /branches mirrors users into the branches table first.
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    monkeypatch.setattr(branch_db, "_engine", engine)
    monkeypatch.setattr(branch_db, "_session_factory", None)
    branch_db.init_branches_db()
    return fake


@pytest.fixture
def client():
    return TestClient(fastapi_app)


def activate(client, name, serial=None, activation_code=None, mac=None):
    return client.post("/auth/activate", json={
        "activation_code": activation_code or code(name),
        "serial_number": serial or f"SN-{name}",
        "mac_address": mac or f"MAC-{name}",
    })


def verify(client, activation_code, device_key=None):
    body = {"activation_code": activation_code}
    if device_key is not None:
        body["device_key"] = device_key
    r = client.post("/auth/verify-device", json=body)
    assert r.status_code == 200, r.text
    return r.json()["verdict"]


def listed(client, name):
    r = client.get("/users")
    assert r.status_code == 200, r.text
    return next(u for g in r.json() for u in g["users"] if u["activation_code"] == code(name))


# --- Activation ------------------------------------------------------------------

def test_first_activation_issues_a_device_key(client, db):
    db.add("a")

    r = activate(client, "a")

    assert r.status_code == 200
    key = r.json()["device_key"]
    assert r.json()["message"] == "User activated successfully"
    assert len(key) >= 43  # secrets.token_urlsafe(32)
    assert verify(client, code("a"), key) == "ok"
    assert listed(client, "a")["is_activated"] == 1
    assert listed(client, "a")["start_date"] != "N/A"
    # The one storage assertion: only the SHA-256 digest is kept, never the key.
    assert db.row("a")["device_key_hash"] == hashlib.sha256(key.encode()).hexdigest()


@pytest.mark.parametrize("kwargs, status", [
    ({"serial": "SN-nobody"}, 404),
    ({"activation_code": "ADV-XXXX-XXXX-XXXX"}, 401),
    ({"mac": "MAC-other"}, 401),
])
def test_existing_activation_refusals_are_unchanged(client, db, kwargs, status):
    db.add("a")

    r = activate(client, "a", **kwargs)

    assert r.status_code == status
    assert set(r.json()) == {"error", "detail"}
    assert verify(client, code("a")) == "no_key"


def test_activated_branch_holding_a_key_is_refused_and_left_untouched(client, db):
    db.add("a")
    key = activate(client, "a").json()["device_key"]
    started = listed(client, "a")["start_date"]

    r = activate(client, "a")

    assert r.status_code == 409
    assert r.json()["error"] == "device_key_exists"
    assert "device_key" not in r.json()
    assert verify(client, code("a"), key) == "ok"
    assert listed(client, "a")["start_date"] == started


def test_old_activated_branch_without_key_gets_one_and_keeps_start_date(client, db):
    db.add("a", is_activated=1, start_date="2025-03-01 09:00:00")

    r = activate(client, "a")

    assert r.status_code == 200
    assert verify(client, code("a"), r.json()["device_key"]) == "ok"
    assert listed(client, "a")["start_date"] == "2025-03-01 09:00:00"


def test_activation_succeeds_again_after_admin_clears_the_key(client, db):
    db.add("a")
    old_key = activate(client, "a").json()["device_key"]
    db.set("a", device_key_hash=None)

    r = activate(client, "a")

    assert r.status_code == 200
    new_key = r.json()["device_key"]
    assert new_key != old_key
    assert verify(client, code("a"), new_key) == "ok"
    assert verify(client, code("a"), old_key) == "invalid_device_key"


def test_admin_resetting_is_activated_reopens_activation(client, db):
    db.add("a")
    old_key = activate(client, "a").json()["device_key"]
    db.set("a", is_activated=0)  # key left on record

    r = activate(client, "a")

    assert r.status_code == 200
    assert verify(client, code("a"), r.json()["device_key"]) == "ok"
    assert verify(client, code("a"), old_key) == "invalid_device_key"


@pytest.mark.parametrize("cols", [
    {"is_activated": 1, "device_key_hash": ""},          # key cleared with '' instead of NULL
    {"is_activated": None, "device_key_hash": "0" * 64},  # not activated, stale key on record
])
def test_activation_is_refused_only_when_activated_with_a_key_on_record(client, db, cols):
    db.add("a", **cols)

    r = activate(client, "a")

    assert r.status_code == 200, r.text
    assert verify(client, code("a"), r.json()["device_key"]) == "ok"


def test_activation_that_loses_a_race_is_refused_and_keeps_the_winners_key(client, db, monkeypatch):
    db.add("a")
    # Both requests read the keyless row; the other one writes first.
    stale = db.row("a")
    winner = activate(client, "a").json()["device_key"]
    monkeypatch.setattr(auth_service, "read_data_by_serial_number", lambda serial: dict(stale))

    r = activate(client, "a")

    assert r.status_code == 409
    assert r.json()["error"] == "device_key_exists"
    assert verify(client, code("a"), winner) == "ok"


# --- Device verification ---------------------------------------------------------

@pytest.mark.parametrize("branch, key, verdict", [
    ("a", "a", "ok"),
    ("a", "wrong", "invalid_device_key"),
    ("b", "a", "invalid_device_key"),        # Branch A's key with Branch B's code
    ("c", "a", "invalid_device_key"),        # key presented, none on record
    ("a", None, "invalid_device_key"),       # key on record, none presented
    ("a", "", "invalid_device_key"),         # an empty key counts as not presented
    ("c", None, "no_key"),
    ("c", "", "no_key"),
    ("zzzz", "a", "unknown_activation_code"),
    ("zzzz", None, "unknown_activation_code"),
])
def test_verify_device_verdicts(client, db, branch, key, verdict):
    db.add("a")
    db.add("b")
    db.add("c", is_activated=1)
    keys = {"a": activate(client, "a").json()["device_key"],
            "b": activate(client, "b").json()["device_key"],
            "wrong": "not-the-key-" + "x" * 32}

    assert verify(client, code(branch), keys.get(key, key)) == verdict


def test_verify_device_answers_a_verdict_for_an_unencodable_key(client, db):
    db.add("a")
    activate(client, "a")

    # A lone surrogate is valid JSON but cannot be UTF-8 encoded.
    r = client.post("/auth/verify-device", headers={"Content-Type": "application/json"},
                    content=('{"activation_code": "%s", "device_key": "\\ud800"}' % code("a")).encode())

    assert r.status_code == 200, r.text
    assert r.json() == {"verdict": "invalid_device_key"}


# --- Nothing leaks ---------------------------------------------------------------

def test_no_response_carries_the_key_hash(client, db):
    db.add("a")
    db.add("b", is_activated=1)
    first = activate(client, "a")                                # 200
    key = first.json()["device_key"]

    responses = [
        first,
        activate(client, "a"),                                   # 409
        activate(client, "b"),                                   # 200
        client.post("/auth/verify-device", json={"activation_code": code("a"), "device_key": key}),
        client.get("/users"),
        client.get("/branches"),
    ]
    digests = [db.row("a")["device_key_hash"], db.row("b")["device_key_hash"]]

    assert all(digests)
    assert [r.status_code for r in responses] == [200, 409, 200, 200, 200, 200]
    for listing in responses[-2:]:  # both listings really carry the two keyed rows
        assert code("a") in listing.text and code("b") in listing.text
    for r in responses:
        assert "device_key_hash" not in r.text
        for digest in digests:
            assert digest not in r.text


def test_device_key_never_logged(client, db, caplog):
    caplog.set_level(logging.DEBUG)
    db.add("a")

    key = activate(client, "a").json()["device_key"]
    activate(client, "a")                                        # 409
    verify(client, code("a"), key)
    verify(client, code("a"), "presented-but-wrong-key")

    assert "User activated successfully: SN-a" in caplog.text   # logs are captured at all
    assert key not in caplog.text
    assert "presented-but-wrong-key" not in caplog.text


# --- pool: a reconnect storm waits for a connection instead of failing --------------
class BusyPool:
    """Exhausted for the first `busy` asks, then hands out a connection."""

    def __init__(self, busy: int):
        self.busy = busy

    def get_connection(self):
        if self.busy:
            self.busy -= 1
            raise session.mysql.connector.errors.PoolError("Failed getting connection; pool exhausted")
        return "conn"


def test_get_db_waits_for_a_free_connection(monkeypatch):
    monkeypatch.setattr(session, "get_connection_pool", lambda: BusyPool(busy=3))
    assert session.get_db() == "conn"


def test_get_db_gives_up_once_the_wait_runs_out(monkeypatch):
    monkeypatch.setattr(session, "get_connection_pool", lambda: BusyPool(busy=10**6))
    monkeypatch.setattr(session, "POOL_WAIT_SECONDS", 0.2)
    with pytest.raises(session.mysql.connector.errors.PoolError):
        session.get_db()
