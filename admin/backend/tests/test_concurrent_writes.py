"""Every Branch reconnects in the same second after a frontend restart.

Each identify makes the frontend PATCH /users/online with the whole online list
and POST /branches/<code>/identify. On .85 (30 Oct 2026) that burst gave MariaDB
deadlocks (1213) on users and "database is locked" on the branches SQLite file,
and held the backend's threads long enough that verify-device timed out.

Seam 1 needs a real MariaDB/MySQL: the deadlock is InnoDB's, a fake cannot show
it. Point AI_CAMERA_TEST_MYSQL_* at a server where the user may create
databases named aicam_test_*; without one the test is skipped.

Run (from ai-camera-admin/backend):  python -m pytest tests -q
"""
from __future__ import annotations

import os
import random
import sys
import threading

import mysql.connector
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # backend/

from data_service.core.database import branch_db
from data_service.core.database.crud import user_crud
from data_service.core.models.branch import Branch
from data_service.core.services import branch_service, user_service

BRANCHES = 33
ROUNDS = 5


def code(i: int) -> str:
    return f"ADV-T{i:03d}-0000-0001"


def burst(fn, args_per_thread):
    """Run fn once per args tuple, all released at the same moment; return what they raised."""
    gate = threading.Barrier(len(args_per_thread))
    errors = []

    def run(args):
        gate.wait()
        try:
            fn(*args)
        except Exception as e:  # collected, asserted by the caller
            errors.append(e)

    threads = [threading.Thread(target=run, args=(args,)) for args in args_per_thread]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


# --- Seam 1: PATCH /users/online -------------------------------------------------

MYSQL = {
    "host": os.getenv("AI_CAMERA_TEST_MYSQL_HOST", "127.0.0.1"),
    "port": int(os.getenv("AI_CAMERA_TEST_MYSQL_PORT", "3306")),
    "user": os.getenv("AI_CAMERA_TEST_MYSQL_USER", "aicam_test"),
    "password": os.getenv("AI_CAMERA_TEST_MYSQL_PASSWORD", "aicam_test"),
}


@pytest.fixture
def users_db(monkeypatch):
    name = f"aicam_test_{os.getpid()}"
    try:
        admin = mysql.connector.connect(**MYSQL)
    except mysql.connector.Error as e:
        pytest.skip(f"no test MySQL at {MYSQL['host']}:{MYSQL['port']}: {e}")
    cur = admin.cursor()
    cur.execute(f"DROP DATABASE IF EXISTS {name}")
    cur.execute(f"CREATE DATABASE {name}")
    # The columns and indexes users has on .85 that the online update touches.
    cur.execute(
        f"CREATE TABLE {name}.users (id INT AUTO_INCREMENT PRIMARY KEY,"
        " activation_code VARCHAR(64) NOT NULL UNIQUE, is_online TINYINT(1) DEFAULT 0) ENGINE=InnoDB"
    )
    cur.executemany(f"INSERT INTO {name}.users (activation_code) VALUES (%s)", [(code(i),) for i in range(51)])
    admin.commit()
    monkeypatch.setattr(user_crud, "get_db", lambda: mysql.connector.connect(database=name, **MYSQL))
    yield lambda: mysql.connector.connect(database=name, **MYSQL)
    cur.execute(f"DROP DATABASE IF EXISTS {name}")
    admin.close()


def online_codes(connect) -> set:
    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT activation_code FROM users WHERE is_online = 1")
    rows = {r[0] for r in cur.fetchall()}
    conn.close()
    return rows


def test_every_branch_reporting_online_at_once_never_deadlocks(users_db):
    online = [code(i) for i in range(BRANCHES)]
    rng = random.Random(0)
    for _ in range(ROUNDS):
        # Each identify sends the list in its own order, as update_clients grows.
        # The old two-step update deadlocked on exactly these mixed orders.
        errors = burst(user_service.update_online_status,
                       [(rng.sample(online, len(online)),) for _ in range(BRANCHES)])
        assert errors == []
        assert online_codes(users_db) == set(online)


def test_online_list_sets_exactly_those_branches_online(users_db):
    user_service.update_online_status([code(i) for i in range(10)])
    user_service.update_online_status([code(3), code(40)])
    assert online_codes(users_db) == {code(3), code(40)}
    user_service.update_online_status([])
    assert online_codes(users_db) == set()


# --- Seam 2: POST /branches/<code>/identify --------------------------------------

@pytest.fixture
def branches_file(monkeypatch, tmp_path):
    monkeypatch.setattr(branch_db, "BRANCH_DB_URL", f"sqlite:///{tmp_path / 'branches.db'}")
    monkeypatch.setattr(branch_db, "_engine", None)
    monkeypatch.setattr(branch_db, "_session_factory", None)
    branch_db.init_branches_db()
    with branch_db.get_session_factory()() as session:
        session.add_all([Branch(activation_code=code(i), status="offline") for i in range(BRANCHES)])
        session.commit()
    yield
    branch_db.get_engine().dispose()


def test_every_branch_identifying_at_once_is_recorded(branches_file):
    for round_ in range(ROUNDS):
        version = f"V.{round_}"
        errors = burst(branch_service.report_identify, [(code(i), version) for i in range(BRANCHES)])
        assert errors == []
        with branch_db.get_session_factory()() as session:
            rows = session.query(Branch).all()
            assert {(b.status, b.last_version) for b in rows} == {("active", version)}
