from typing import List

from data_service.core.schemas.service_schemas import (
    ServiceCreateRequest,
    ServiceResponse
)
from data_service.core.database.session import get_db
from data_service.core.database.crud.package_crud import read_package_id_by_name

def create_service_in_db(service_data: ServiceCreateRequest):
    package_id = read_package_id_by_name(service_data.package.capitalize())
    if package_id == -1:
        raise ValueError(f"Package '{service_data.package}' not found")
    
    try:
        conn = get_db()
        cursor = conn.cursor()
        # แต่ละแพ็คเกจกำหนดสมาชิกของตัวเองชัด ๆ ไม่สืบทอดจากแพ็คเกจอื่นอัตโนมัติ
        # โฟลเดอร์เดียวจึงอยู่ได้หลายแพ็คเกจ (unique อยู่ที่คู่ folder+package)
        # เช่น camera_service ลงทะเบียนได้ทั้งใน Basic และ Pro
        #
        # ส่งโฟลเดอร์เดิม + แพ็คเกจเดิมเข้ามาอีกครั้ง = ตั้งใจแก้ชื่อ/คำอธิบาย
        # ไม่ใช่ความผิดพลาด (ไม่มี endpoint แก้ไขบริการ มีแค่เพิ่มกับลบ)
        insert_query = """
            INSERT INTO services (name, package_id, description, dlc_folder)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                name = VALUES(name),
                description = VALUES(description)
        """
        cursor.execute(insert_query, (
            service_data.name,
            package_id,
            service_data.description,
            service_data.dlc_folder
        ))
        # rowcount: 1 = เพิ่มใหม่, 2 = ไปอัปเดตของเดิม (พฤติกรรมของ MySQL)
        created = cursor.rowcount == 1
        conn.commit()
        return created
    except Exception as e:
        if conn:
            conn.rollback()
        raise e
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

def read_all_services_from_db():
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True)
        query = """
            SELECT s.id, s.name, p.name AS package, s.description, s.dlc_folder
            FROM services s
            JOIN packages p ON s.package_id = p.id
        """
        cursor.execute(query)
        services = cursor.fetchall()
        return [ServiceResponse(**service) for service in services]
    except Exception as e:
        print(f"Error reading all services: {e}")
        return []
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

def delete_service_from_db(service_id: int):
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM services WHERE id = %s", (service_id,))
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        raise e
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()



# =============================================================
# Service Utilities
# =============================================================
def read_service_folder_by_ids(service_ids: List[int]) -> List[str]:
    if not service_ids:
        return []
    
    format_strings = ','.join(['%s'] * len(service_ids))
    try:
        conn = get_db()
        cursor = conn.cursor()
        query = f"SELECT dlc_folder FROM services WHERE id IN ({format_strings})"
        cursor.execute(query, tuple(service_ids))
        results = cursor.fetchall()
        return [row[0] for row in results]
    except Exception as e:
        print(f"Error reading service folders by IDs {service_ids}: {e}")
        return []
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

def read_services_by_package_from_db(package_name: str):
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True)
        query = """
            SELECT s.id, s.name, p.name AS package, s.description, s.dlc_folder
            FROM services s
            JOIN packages p ON s.package_id = p.id
            WHERE p.name = %s
        """
        cursor.execute(query, (package_name.capitalize(),))
        services = cursor.fetchall()
        return [ServiceResponse(**service) for service in services]
    except Exception as e:
        print(f"Error reading services by package '{package_name}': {e}")
        return []
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()