from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel
from typing import List, Literal, Optional


class PercentPoint(BaseModel):
    """A point in percent (0-100) of a picture; anything outside is pulled to the edge."""
    x: float
    y: float

    @field_validator("x", "y")
    @classmethod
    def _in_picture(cls, v: float) -> float:
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("point must be a finite number")
        return min(100.0, max(0.0, v))


class HotzoneFloorZone(BaseModel):
    """One hot zone's place on a floor plan, set by hand on Calibrate Hot Zone.

    camera_points is the zone as drawn on Zone Area when it was placed, so a zone
    redrawn there afterwards can be told apart from the one that was placed.
    """
    zone_id: str = Field(min_length=1, max_length=64)
    type: Literal["count", "checkout"] = "count"
    name: str = Field(default="", max_length=60)
    camera_points: List[PercentPoint] = Field(min_length=3, max_length=64)
    floor_points: List[PercentPoint] = Field(min_length=3, max_length=64)

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class HotzoneFloorInput(BaseModel):
    hotzone_floor: List[HotzoneFloorZone] = Field(default_factory=list, max_length=100)

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class FloorplanInput(BaseModel):
    name: str
    description: str | None = ""
    image_url: str | None = ""
    floorplan_group: int = 1

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True
    )


class FloorplanOutput(BaseModel):
    id: int
    name: str
    description: Optional[str] = ""
    image_url: Optional[str]
    floorplan_group: int = 1
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True
    )


class CameraCalibrationInput(BaseModel):
    floorplan_id: int
    camera_ip: str
    camera_channel: int
    camera_name: Optional[str] = None # เผื่อรับมาจาก frontend แต่ไม่ลง DB
    src_points: List[List[float]] = []
    dst_points: List[List[float]] = []
    roi_zones: list = []

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)

class CameraCalibrationOutput(BaseModel):
    id: int
    floorplan_id: int
    camera_ip: Optional[str] = None
    camera_channel: Optional[int] = None
    camera_name: Optional[str] = None
    src_points: List[List[float]] = []
    dst_points: List[List[float]] = []
    matrix: Optional[List[List[float]]] = None
    roi_zones: list = []
    hotzone_floor: list = []

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True
    )

class FloorplanWithCamerasOutput(BaseModel):
    id: int
    name: str
    description: Optional[str] = ""
    image_url: Optional[str]
    floorplan_group: int = 1
    camera_count: int
    is_calibrated: bool = False
    cameras: List[CameraCalibrationOutput]

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True
    )