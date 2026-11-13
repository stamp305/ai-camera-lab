from data_service.db.session import get_db
from data_service.schemas.store_schemas import BranchModel
from datetime import time, timedelta, datetime

def _normalize_region(region: str) -> str:
    if not region:
        return region
    normalized = region.strip()
    if normalized.startswith("ภาค"):
        normalized = normalized[len("ภาค"):]
    return normalized.replace("ฯ", "")

def create_store_data(branch_data: BranchModel) -> None:
    try:
        conn = get_db()
        cursor = conn.cursor()

        # A save that does not name the Security setting (the first-run Create Store page)
        # keeps the one already set; the row is rewritten whole below.
        staff_check = branch_data.staff_check_minutes
        if "staff_check_minutes" not in branch_data.model_fields_set:
            cursor.execute("SELECT staff_check_minutes FROM store LIMIT 1")
            row = cursor.fetchone()
            staff_check = row[0] if row else None
        cursor.execute("DELETE FROM store")
        cursor.execute(
            "INSERT INTO store (id, name, region, province, open_time, close_time, staff_check_minutes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (branch_data.id, branch_data.name, _normalize_region(branch_data.region), branch_data.province,
             str(branch_data.open_time), str(branch_data.close_time), staff_check)
        )
        conn.commit()
        print(f"✅ Store data created successfully: {branch_data}")
    except Exception as e:
        print(f"❌ DB error in create_store_data: {e}")
        raise
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

def read_store_data() -> BranchModel | None:
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("SELECT * FROM store")
        row = cursor.fetchone()
        if not row:
            print("⚠️ No store data found.")
            return None

        # Convert timedelta to time objects
        open_time = row['open_time']
        close_time = row['close_time']
        
        if isinstance(open_time, timedelta):
            open_time = (datetime.min + open_time).time()
        if isinstance(close_time, timedelta):
            close_time = (datetime.min + close_time).time()

        return BranchModel(
            id=row['id'],
            name=row['name'],
            region=row['region'],
            province=row['province'],
            open_time=open_time,
            close_time=close_time,
            staff_check_minutes=row.get('staff_check_minutes'),
        )
    except Exception as e:
        print(f"❌ DB error in read_store_data: {e}")
        raise
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()