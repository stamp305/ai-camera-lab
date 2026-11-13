from pydantic import BaseModel

class ServiceCreateRequest(BaseModel):
    name: str
    package: str
    description: str
    dlc_folder: str

class ServiceResponse(BaseModel):
    id: int
    name: str
    package: str
    description: str
    dlc_folder: str