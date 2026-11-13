import argparse
import os
import mysql.connector
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

# Config database connection
API_DATA_CONFIG = {
    "host": "10.10.10.209",
    "user": os.getenv("NEW_DB_USER"),
    "password": os.getenv("NEW_DB_PASS"),
    "database": "api_data",
}


def check_database():
    try:
        connection = mysql.connector.connect(**API_DATA_CONFIG)
        print(f"Connected: {API_DATA_CONFIG['host']} / {API_DATA_CONFIG['database']}")
        return connection

    except mysql.connector.Error as e:
        print(f"Database Error: {e}")
        return None


def get_all_branches(connection):
    cursor = connection.cursor()

    try:
        cursor.execute(
            "SELECT DISTINCT branch_cuscode FROM shoping_traffic_hotzone ORDER BY branch_cuscode"
        )
        return [row[0] for row in cursor.fetchall()]

    except mysql.connector.Error as e:
        print(f"Database Error: {e}")
        return []

    finally:
        cursor.close()


def get_latest_records(connection, branch_cuscode, zone_type=None):
    cursor = None

    try:
        cursor = connection.cursor()

        zone_filter = "AND h.zone_type = %s" if zone_type else ""

        query = f"""
            SELECT
                h.id,
                h.branch_cuscode,
                h.region_id,
                h.region_name,
                h.province_id,
                h.province_name,
                h.timestamps,
                h.day_of_week,
                h.hour,
                h.count_in,
                h.count_apple,
                h.count_nb,
                h.count_learnhub,
                h.count_active,
                h.count_not_active,
                h.engagement_wait,
                h.insert_date_time,
                h.nb_to_apple,
                h.apple_to_nb,
                h.zone_type,
                h.mono_id,

                e.demo_id,
                e.hotzone_id,
                e.name_zone1_mono,
                e.count_zone1_mono,
                e.name_zone2_mono,
                e.count_zone2_mono,
                e.name_zone3_mono,
                e.count_zone3_mono,
                e.name_zone4_mono,
                e.count_zone4_mono,
                e.name_zone5_mono,
                e.count_zone5_mono,
                e.name_zone_ce,
                e.count_zone_ce

            FROM shoping_traffic_hotzone h

            LEFT JOIN emp_demo e
                ON h.id = e.hotzone_id

            WHERE h.branch_cuscode = %s
            {zone_filter}

            ORDER BY h.timestamps DESC

            LIMIT 1
        """

        params = (branch_cuscode, zone_type) if zone_type else (branch_cuscode,)
        cursor.execute(query, params)

        records = cursor.fetchall()

        # ชื่อ columns
        columns = [desc[0] for desc in cursor.description]

        print("\n" + "=" * 80)
        header = f"LATEST RECORDS | branch_cuscode = {branch_cuscode}"
        if zone_type:
            header += f" | zone_type = {zone_type}"
        print(header)
        print("=" * 80)

        width = max(len(c) for c in columns)

        for i, record in enumerate(records, start=1):
            print(f"\n--- Record {i} ---")

            for col, value in zip(columns, record):
                if isinstance(value, datetime):
                    value = value.strftime("%Y-%m-%d %H:%M:%S")

                print(f"  {col:<{width}} : {value}")

        print("\n" + "-" * 80)
        print(f"Total records: {len(records)}")

    except mysql.connector.Error as e:
        print(f"Database Error: {e}")

    finally:
        if cursor:
            cursor.close()


def desc_table(connection, table):
    cursor = connection.cursor()

    try:
        cursor.execute(f"DESCRIBE `{table}`")
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]

        widths = [max(len(str(c)), max((len(str(r[i])) for r in rows), default=0)) for i, c in enumerate(columns)]

        print(f"\nDESCRIBE {table}")
        print("  " + " | ".join(f"{c:<{w}}" for c, w in zip(columns, widths)))
        print("  " + "-+-".join("-" * w for w in widths))

        for row in rows:
            print("  " + " | ".join(f"{str(v):<{w}}" for v, w in zip(row, widths)))

    except mysql.connector.Error as e:
        print(f"Database Error: {e}")

    finally:
        cursor.close()


def show_tables(connection):
    cursor = connection.cursor()

    try:
        cursor.execute("SHOW TABLES")
        tables = cursor.fetchall()
        print("\nTables in api_data database:")
        for table in tables:
            print(f"  - {table[0]}")

    except mysql.connector.Error as e:
        print(f"Database Error: {e}")

    finally:
        cursor.close()


if __name__ == "__main__":

    parser = argparse.ArgumentParser(description="Check api_data database")
    parser.add_argument("-b", "--branch", default="51000001", help="branch_cuscode (default: %(default)s)")
    parser.add_argument("-a", "--all", action="store_true", help="latest records ของทุก branch (distinct branch_cuscode)")
    parser.add_argument("-z", "--zone", choices=["MONO", "IT"], help="filter zone_type")
    parser.add_argument("-t", "--tables", action="store_true", help="show tables ทั้งหมดใน database")
    parser.add_argument("-d", "--desc", metavar="TABLE", help="describe table (โครงสร้าง columns)")
    args = parser.parse_args()

    connection = check_database()

    if connection:
        try:
            if args.tables:
                show_tables(connection)
            elif args.desc:
                desc_table(connection, args.desc)
            elif args.all:
                branches = get_all_branches(connection)
                print(f"\nFound {len(branches)} branches")

                for branch in branches:
                    get_latest_records(connection, branch, args.zone)
            else:
                get_latest_records(
                    connection,
                    args.branch,
                    args.zone
                )

        finally:
            if connection.is_connected():
                connection.close()