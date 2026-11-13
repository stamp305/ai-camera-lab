from pydantic import BaseModel

class UserLogin(BaseModel):
    """
    Class for user login schema.
    Fields:
    - username: str
    - password: str
    """
    username: str
    password: str

class Location(BaseModel):
    """
    Class for user location schema.
    Fields:
    - latitude: float
    - longitude: float
    """
    lat: float
    lng: float

class UserActivation(BaseModel):
    """
    Class for user activation schema.
    Fields:
    - activation_code: str
    - [LEGACY] user_id: str
    - [LEGACY] position: Location
    """
    activation_code: str
    # user_id: str
    # position: Location