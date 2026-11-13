"""The fake `mysql.connector` every test in this service runs against — no live host.

It records each statement with its parameters, serves canned SELECT rows keyed by the
**exact** table name (so `shoping_traffic_hotzone_mono` never picks up
`shoping_traffic_hotzone`'s rows), hands an id back for INSERTs, and counts commits and
rollbacks. A test drives it through `self.db`:

    self.db.rows["emp_demo"] = [{"demo_id": 5}]   # what a SELECT on that table returns
    self.db.fail_on = "INSERT"                    # make that statement raise
    self.db.calls / .writes() / .commits / .rollbacks / .connects   # what happened

Run (from ai-camera-admin/backend/ai_camera_service/):
    ../.venv/bin/python -m unittest -v
"""
import os
import re
import sys
import types
import unittest
from typing import Optional
from unittest import mock

from starlette.testclient import TestClient

ACS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # backend/ai_camera_service/
sys.path.insert(0, ACS_DIR)

import app as appmod

# The table a statement acts on: FROM x / INTO x / UPDATE x, first match wins. Backticks
# are stripped, so a schema-qualified `10000001`.ar_invoice keys as 10000001.ar_invoice.
TABLE_RE = re.compile(r"\b(?:from|into|update)\s+(`?[\w.`]+)", re.I)


def written_columns(sql, params):
    """{column: value} for one INSERT or UPDATE, read back out of the statement itself.

    An UPDATE's trailing `WHERE id = %s` lands in the dict as "id", which is how a test
    checks the row an update targeted."""
    if sql.upper().startswith("INSERT"):
        names = sql[sql.index("(") + 1:sql.index(")")].split(", ")
    else:
        names = re.findall(r"(\w+) = %s", sql)
    return dict(zip(names, params))


class FakeDBError(Exception):
    """Stands in for mysql.connector.Error — the service never catches it, so it
    propagates and the endpoint answers 500."""


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self._rows = []
        self.lastrowid = None

    def execute(self, sql, params=None):
        sql = " ".join(sql.split())
        self.db.calls.append((sql, params))
        if self.db.fail_on and self.db.fail_on in sql:
            raise FakeDBError(f"fake database failure on: {sql}")
        if sql.upper().startswith("INSERT"):
            self.db.next_id += 1
            self.lastrowid = self.db.next_id
            self._rows = []
        else:
            table = TABLE_RE.search(sql)
            name = table.group(1).replace("`", "") if table else None
            self._rows = list(self.db.rows.get(name, []))

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass


class FakeConn:
    def __init__(self, db):
        self.db = db

    def cursor(self, dictionary=False):
        return FakeCursor(self.db)

    def commit(self):
        self.db.commits += 1

    def rollback(self):
        self.db.rollbacks += 1

    def close(self):
        pass


class FakeDB:
    def __init__(self):
        self.calls = []          # every (sql, params) executed, whitespace-collapsed
        self.connects = []       # connect kwargs per connection (one per statement, or
                                 # one per request on the hotzone feed's single transaction)
        self.commits = 0
        self.rollbacks = 0
        self.rows = {}           # exact table name → the rows a SELECT on it returns
        self.next_id = 100       # id handed to the next INSERT, so a test can pin the value
        self.fail_on: Optional[str] = None   # statements containing this substring raise FakeDBError

    def connect(self, **kwargs):
        self.connects.append(kwargs)
        return FakeConn(self)

    def writes(self):
        return [c for c in self.calls if not c[0].lstrip().upper().startswith("SELECT")]

    def writes_to(self, table):
        """The write statements aimed at one table — the hotzone feed touches four."""
        return [(sql, params) for sql, params in self.writes()
                if (m := TABLE_RE.search(sql)) and m.group(1).replace("`", "") == table]


class FakeDBTestCase(unittest.TestCase):
    """Installs the fake mysql.connector module for the duration of each test.

    Shadow mode is switched OFF here (TRAFFIC_FEEDS_SHADOW=0): every feed test below asserts
    the real writes. tests/test_traffic_shadow.py switches it back on.
    """

    def setUp(self):
        env = mock.patch.dict("os.environ", {"TRAFFIC_FEEDS_SHADOW": "0"})
        env.start()
        self.addCleanup(env.stop)
        self.db = FakeDB()
        self._saved = {k: sys.modules.get(k) for k in ("mysql", "mysql.connector")}
        connector = types.ModuleType("mysql.connector")
        connector.connect = self.db.connect
        root = types.ModuleType("mysql")
        root.connector = connector
        sys.modules["mysql"], sys.modules["mysql.connector"] = root, connector
        self.client = TestClient(appmod.app)

    def tearDown(self):
        for name, mod in self._saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod

    def reset_db(self):
        """A clean fake, for a test that drives several requests through one.

        Not `setUp()`: re-running that would snapshot the already-installed fake as the
        module to restore, so `tearDown` would leave it in sys.modules for every test after.
        """
        self.db = FakeDB()
        sys.modules["mysql.connector"].connect = self.db.connect

    def assert_zero_writes(self):
        self.assertEqual(self.db.writes(), [])
        self.assertEqual(self.db.commits, 0)
