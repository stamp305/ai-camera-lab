from passlib.hash import bcrypt

from data_service.db.session import get_db


USERNAME = "admin"
PASSWORD = "admin"

def init_account_config():
    conn = get_db()
    cursor = conn.cursor()

    try:
        cursor.execute("SELECT COUNT(*) FROM account_config")
        if cursor.fetchone()[0] == 0:
            hashed_password = bcrypt.hash(PASSWORD)
            cursor.execute("INSERT INTO account_config (username, password) VALUES (?, ?)", (USERNAME, hashed_password))
            conn.commit()
    except Exception as e:
        print(f"Error initializing account config: {e}")
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()