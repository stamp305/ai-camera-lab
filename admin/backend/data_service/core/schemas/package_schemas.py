from pydantic import BaseModel


class PackageCreateRequest(BaseModel):
    name: str


class PackageResponse(BaseModel):
    id: int
    name: str
