"""Ticket 18 (B35): ensure_database creates and migrates only the Main Client's own
tables; the zone and log tables belong to ai_camera_db's schema step.

ensure_database runs for real in a fresh process (HOME in a temp folder, the
secure_env stub on the path) against a throwaway database on the local MariaDB,
holding tables in the shape an old Branch has them.
"""
import getpass
import os
import subprocess
import sys
import uuid

import pytest

TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS)
SOCK = "/tmp/mysql.sock"

# A Branch whose database was made by the first client (894e415: camera_master,
# camera_channel, attendance_polygon, charge_logs; ab24f23: floorplan) and that also
# holds the tables whose collation the old client reset (shape of b0a3095), in the
# drifted collation that reset was for.
DRIFTED = "DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci"
OLD_BRANCH = [
    """CREATE TABLE camera_master (
    id INT NOT NULL AUTO_INCREMENT, ip VARCHAR(45) NOT NULL, username VARCHAR(64), password TEXT,
    name VARCHAR(128), is_scanned TINYINT(1) NOT NULL DEFAULT 0,
    PRIMARY KEY (id), UNIQUE KEY ip (ip)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE camera_channel (
    ip VARCHAR(45) NOT NULL, channel TINYINT UNSIGNED NOT NULL, status TINYINT(1) DEFAULT 1,
    PRIMARY KEY (ip, channel),
    FOREIGN KEY (ip) REFERENCES camera_master(ip) ON DELETE CASCADE ON UPDATE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE attendance_polygon (
    id VARCHAR(40) NOT NULL, ip VARCHAR(45) NOT NULL, channel INT NOT NULL, color VARCHAR(20),
    points LONGTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL CHECK (json_valid(points)),
    PRIMARY KEY (id),
    FOREIGN KEY (ip) REFERENCES camera_master(ip) ON DELETE CASCADE ON UPDATE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE charge_logs (
    id VARCHAR(40) NOT NULL, timestamp DATETIME NOT NULL, ip VARCHAR(45) NOT NULL, channel INT NOT NULL,
    is_charged TINYINT(1) NOT NULL, person_frame VARCHAR(512),
    PRIMARY KEY (id), KEY idx_charge_ts (timestamp), KEY idx_charge_ip_channel (ip, channel)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE floorplan (
    id INT NOT NULL AUTO_INCREMENT, name VARCHAR(255) NOT NULL, image_url TEXT DEFAULT '',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    f"""CREATE TABLE demo_apple_logs (
    id VARCHAR(36) PRIMARY KEY, timestamp DATETIME, ip VARCHAR(15), channel INT, zone_name VARCHAR(255),
    track_id VARCHAR(255), person_frame VARCHAR(255)
    ) ENGINE=InnoDB {DRIFTED}""",
    f"""CREATE TABLE engagement_wait_logs (
    id VARCHAR(36) PRIMARY KEY, timestamp DATETIME, ip VARCHAR(15), channel INT, zone_name VARCHAR(255),
    customer_track_id INT, employee_track_id INT, customer_enter_time DATETIME,
    employee_enter_time DATETIME, wait_seconds FLOAT, person_frame VARCHAR(255)
    ) ENGINE=InnoDB {DRIFTED}""",
    f"""CREATE TABLE in_pass_threshold (
    ip VARCHAR(64) NOT NULL, channel INT NOT NULL, in_speed INT NOT NULL DEFAULT 20,
    out_speed INT NOT NULL DEFAULT 20, pass_speed INT NOT NULL DEFAULT 20, PRIMARY KEY (ip, channel)
    ) ENGINE=InnoDB {DRIFTED}""",
]
COLLATION_RESET_TABLES = ("demo_apple_logs", "engagement_wait_logs", "in_pass_threshold")

SCRIPT = """
import runtime_paths, sys
runtime_paths.RUN_DIR = sys.argv[1]
import client_logic
client_logic.ensure_database()
print("DONE")
"""


def _db():
    pymysql = pytest.importorskip("pymysql")
    try:
        return pymysql.connect(user=getpass.getuser(), password="", unix_socket=SOCK, autocommit=True)
    except Exception as e:
        pytest.skip(f"no local MariaDB: {e}")


@pytest.fixture
def old_branch(tmp_path):
    db_name = f"ai_camera_ddl_test_{uuid.uuid4().hex[:12]}"
    conn = _db()
    cur = conn.cursor()
    cur.execute(f"CREATE DATABASE `{db_name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci")  # as 894e415 made it
    try:
        cur.execute(f"USE `{db_name}`")
        for sql in OLD_BRANCH:
            cur.execute(sql)
        (tmp_path / "home").mkdir()
        env = dict(os.environ,
                   HOME=str(tmp_path / "home"),
                   PYTHONPATH=os.pathsep.join([os.path.join(TESTS, "_pm_site"), TESTS, ROOT]),
                   PYTHONDONTWRITEBYTECODE="1",
                   PM_TEST_RUN_DIR=str(tmp_path),
                   AICAMERA_KEYCHAIN_SERVICE="aicamera-test-" + uuid.uuid4().hex,
                   # No root login: the database and app account setup are skipped, as on a Branch
                   # where they already exist; never touch the local server's accounts.
                   DB_ROOT_USER="no_such_root_" + uuid.uuid4().hex[:8], DB_ROOT_PASSWORD="x",
                   DB_USER=getpass.getuser(), DB_PASSWORD="", DB_HOST="localhost", DB_NAME=db_name)
        env.pop("DB_PORT", None)
        r = subprocess.run([sys.executable, "-c", SCRIPT, str(tmp_path)], cwd=ROOT, env=env,
                           capture_output=True, text=True, timeout=120)
        assert r.returncode == 0 and "DONE" in r.stdout, r.stdout[-3000:] + r.stderr[-3000:]

        def rows(sql):
            cur.execute(sql, (db_name,))
            return cur.fetchall()
        yield rows
    finally:
        cur.execute(f"DROP DATABASE IF EXISTS `{db_name}`")
        conn.close()


def test_ensure_database_makes_only_setup_tables_and_leaves_zone_and_log_tables_alone(old_branch):
    tables = {t for (t,) in old_branch(
        "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s")}
    assert tables == {"camera_master", "camera_channel", "store", "account_config", "client_state",
                      "floorplan", "camera_calibration", "service_ports",
                      # the old Branch's own, untouched
                      "attendance_polygon", "charge_logs", "demo_apple_logs", "engagement_wait_logs",
                      "in_pass_threshold"}

    columns = {}
    for t, c in old_branch("SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = %s"):
        columns.setdefault(t, set()).add(c)
    indexes = {(t, i) for t, i in old_branch(
        "SELECT TABLE_NAME, INDEX_NAME FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = %s")}
    fks = {(t, n) for t, n in old_branch(
        "SELECT TABLE_NAME, CONSTRAINT_NAME FROM information_schema.TABLE_CONSTRAINTS"
        " WHERE TABLE_SCHEMA = %s AND CONSTRAINT_TYPE = 'FOREIGN KEY'")}
    collations = dict(old_branch(
        "SELECT TABLE_NAME, TABLE_COLLATION FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s"))

    # Setup migrations still run.
    assert {"description", "floorplan_group", "image_updated_at", "previous_image_url"} <= columns["floorplan"]
    assert "camera_master_id" in columns["camera_channel"]
    assert "channel_count" in columns["camera_master"]
    assert ("camera_master", "uq_ip_name") in indexes and ("camera_master", "ip") not in indexes
    assert ("camera_channel", "camera_channel_ibfk_1") not in fks

    # Log-table migrations, the charge zone index, the attendance FK drop and the
    # collation reset do not.
    assert not {"zone_name", "track_id"} & columns["charge_logs"]
    assert not {"tier", "tier2_at", "tier3_at"} & columns["demo_apple_logs"]
    assert not {"employee_leave_time", "interaction_seconds"} & columns["engagement_wait_logs"]
    assert ("charge_logs", "idx_charge_zone") not in indexes
    assert ("attendance_polygon", "attendance_polygon_ibfk_1") in fks
    assert {t: collations[t] for t in COLLATION_RESET_TABLES} == dict.fromkeys(
        COLLATION_RESET_TABLES, "utf8mb4_general_ci")
