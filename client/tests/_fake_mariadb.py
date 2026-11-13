"""A stand-in for the `mariadb` driver (a C extension from the bundled python_env).

Every query answers with no rows unless a test registers rows for it: the first
entry of ROWS whose text is found in the SQL wins.
"""
import types

ROWS = []  # [(sql fragment, [row, ...])]
EXECUTED = []  # every SQL text run, in order


class _Cursor:
    def __init__(self):
        self._rows = []

    def execute(self, sql, params=()):
        EXECUTED.append(sql)
        self._rows = next((list(rows) for frag, rows in ROWS if frag in sql), [])

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass


class _Conn:
    def cursor(self, *a, **kw):
        return _Cursor()

    def commit(self):
        pass

    def close(self):
        pass


def install(modules):
    """Put the fake under the name `mariadb` in the given sys.modules-like mapping."""
    mod = types.ModuleType("mariadb")
    mod.connect = lambda **kw: _Conn()
    mod.Error = Exception
    modules["mariadb"] = mod
    return mod
