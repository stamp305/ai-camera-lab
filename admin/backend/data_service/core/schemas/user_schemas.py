import re
from typing import List, Optional
from pydantic import BaseModel, field_validator

# MAC: หกไบต์เลขฐานสิบหก คั่นด้วย : หรือ - (00:1B:44:11:3A:B7 หรือ 00-1B-...)
_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}$")
# Serial: ฟอร์แมตซีเรียลของเครื่อง Mac — ตัวอักษรกับตัวเลขล้วน ยาว 10 หรือ 12
# ตัวเท่านั้น ไม่มีขีด ไม่มีเว้นวรรค (เครื่องรุ่นเก่าเป็น 12 รุ่นใหม่เป็น 10)
# บังคับเป๊ะแบบนี้เพราะสาขาทั้งหมดเป็นเครื่อง Mac ถ้าอนาคตมีอุปกรณ์อื่น
# ค่อยผ่อนกติกาตรงนี้ที่เดียว
_SERIAL_RE = re.compile(r"^[A-Za-z0-9]{10}$|^[A-Za-z0-9]{12}$")


class UserCreateRequest(BaseModel):
    """
    Schema for creating a new user.

    Fields:
    - group_name: Name of the user group.
    - activation_code: Activation code for the user.
    - serial_number: Serial number of the user.
    - mac_address: MAC address of the user.
    """
    group_name: str
    activation_code: str
    serial_number: str
    mac_address: str
    branch_name: Optional[str] = None

    # ตรวจที่นี่เพราะเป็นด่านจริง: หน้าเว็บมี pattern ให้ผู้ใช้เห็นทันทีก็จริง
    # แต่ใครยิง API ตรงก็ข้ามไปได้ และค่าที่พิมพ์มั่วจะไปโผล่เป็น MAC ของสาขา
    # ซึ่งระบบใช้ผูกสิทธิ์และถอดรหัสรหัสผ่านกล้อง
    @field_validator("mac_address")
    @classmethod
    def _check_mac(cls, v: str) -> str:
        v = (v or "").strip()
        if not _MAC_RE.match(v):
            raise ValueError(
                "MAC address ไม่ถูกฟอร์แมต — ต้องเป็นเลขฐานสิบหก 6 ชุด "
                "คั่นด้วย : หรือ - เช่น 00:1B:44:11:3A:B7"
            )
        return v.lower().replace("-", ":")

    @field_validator("serial_number")
    @classmethod
    def _check_serial(cls, v: str) -> str:
        v = (v or "").strip()
        if not _SERIAL_RE.match(v):
            raise ValueError(
                "Serial number ไม่ถูกฟอร์แมต — ซีเรียลเครื่อง Mac เป็นตัวอักษร"
                "กับตัวเลขล้วน ยาว 10 หรือ 12 ตัว ไม่มีขีดและไม่มีเว้นวรรค"
            )
        return v.upper()

class UserReadResponse(BaseModel):
    """
    Class for user read response schema.
    Fields:
    - activation_code: str
    - serial_number: str
    - mac_address: str
    - package: str
    - version: str
    - is_onlint: int
    - is_activated: int
    """
    activation_code: str
    serial_number: str
    mac_address: str
    package: str
    version: str
    is_online: int
    is_activated: int
    start_date: str
    last_update: str
    branch_name: Optional[str] = None

class GroupedUsers(BaseModel):
    """
    Class for grouped users schema.
    Fields:
    - group_name: str
    - users: list of UserReadResponse
    """
    group_name: str
    users: List[UserReadResponse]

class UserOnlineStatus(BaseModel):
    """
    Class for user online status schema.
    Fields:
    - activation_code: str
    - is_online: int
    """
    current_client_list: List[str]
class UserServiceCreateRequest(BaseModel):
    """
    Class for user service create request schema.
    Fields:
    - package: str
    - service_ids: List[int]
    """
    package: str
    version: str
    service_ids: List[int]

class UpdateVersionRequest(BaseModel):
    """
    Class for update version request schema.
    Fields:
    - activation_code_list: List[str]
    """
    activation_code_list: List[str]