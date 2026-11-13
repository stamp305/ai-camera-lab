from typing import Optional

from pydantic import BaseModel

class UserLogin(BaseModel):
    """
    Class for user login schema.
    Fields:
    - username: str
    - password: str
    - serial_number: str
    - mac_address: str
    """
    username: str
    password: str
    serial_number: str
    mac_address: str

class UserActivation(BaseModel):
    activation_code: str
    serial_number: str
    mac_address: str

class DeviceVerification(BaseModel):
    activation_code: str
    device_key: Optional[str] = None
