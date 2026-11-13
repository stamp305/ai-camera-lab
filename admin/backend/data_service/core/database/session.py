import time

import mysql.connector
from mysql.connector import pooling
from data_service.core.configs.database_config import (
    DB_USER,
    DB_PASSWORD,
    DB_HOST,
    DB_PORT,
    DB_NAME
)

# Global connection pool
_db_pool = None

def get_connection_pool():
    global _db_pool
    if _db_pool is None:
        try:
            _db_pool = mysql.connector.pooling.MySQLConnectionPool(
                pool_name="ai_camera_pool",
                pool_size=10,
                pool_reset_session=True,
                host=DB_HOST,
                port=DB_PORT,
                database=DB_NAME,
                user=DB_USER,
                password=DB_PASSWORD
            )
        except mysql.connector.Error as err:
            print(f"Error creating connection pool: {err}")
            raise err
    return _db_pool

# How long get_db waits for a free connection. A burst bigger than the pool
# (every Branch reconnecting at once) queues here instead of failing with
# "pool exhausted".
POOL_WAIT_SECONDS = 5.0


def get_db():
    pool = get_connection_pool()
    deadline = time.monotonic() + POOL_WAIT_SECONDS
    while True:
        try:
            return pool.get_connection()
        except mysql.connector.errors.PoolError as err:
            if time.monotonic() >= deadline:
                print(f"Error getting connection from pool: {err}")
                raise
            time.sleep(0.05)
        except mysql.connector.Error as err:
            print(f"Error getting connection from pool: {err}")
            raise