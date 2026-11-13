from typing import List, Optional
from pydantic import BaseModel, field_validator

class CameraConfigCreate(BaseModel):
    """
    Camera configuration input schema.
    Fields:
    - ip: str - IP address of the camera.
    - name: str - Name of the camera.
    - username: str - Username for camera authentication.
    - password: str - Password for camera authentication.
    - channel_count: 8, 16 or 32 - how many channels Camera Selection lists for this NVR.
    """
    ip: str
    name: str
    username: str
    password: str
    channel_count: int = 16

    @field_validator("channel_count")
    @classmethod
    def _channel_count_choice(cls, v):
        if v not in (8, 16, 32):
            raise ValueError("channel_count must be 8, 16 or 32")
        return v

class CameraConfigUpdate(BaseModel):
    """
    Camera configuration update schema.
    Fields:
    - ip: str - IP address of the camera.
    - name: str - Name of the camera.
    - username / password: optional; empty or missing keeps the stored value, so
      the edit form never has to show (or re-send) the current password.
    """
    ip: str
    name: str
    username: Optional[str] = None
    password: Optional[str] = None
    channel_count: Optional[int] = None   # 8, 16 or 32; missing keeps the current one

    @field_validator("channel_count")
    @classmethod
    def _channel_count_choice(cls, v):
        if v is not None and v not in (8, 16, 32):
            raise ValueError("channel_count must be 8, 16 or 32")
        return v

class CameraConfigOutput(BaseModel):
    """
    Camera configuration output schema.
    Fields:
    - id: int - Unique identifier of the camera configuration.
    - ip: str - IP address of the camera.
    - name: str - Name of the camera.
    - username: str - Username for camera authentication.
    """
    id: int
    ip: str
    name: str
    username: str
    channel_count: int = 16

class CameraChannel(BaseModel):
    """
    Camera channel schema.
    Fields:
    - ip: str - IP address of the camera.
    - network_name: str - Network name of the camera.
    - channel: int - Channel number of the camera.
    - status: int - Status of the camera channel (e.g., 1: active, 0: inactive).
    - snapshot_state / snapshot_last_try: the Camera Selection picture — "ok";
      "pending" (not tried yet); "no_signal" (tried, no picture, cause unknown);
      "broken" (the NVR has a camera set up on it that is not answering);
      "absent" (the NVR has no camera set up on it); "beyond" (past the NVR's own
      channel count) — and when it was last tried (epoch seconds).
    - snapshot_gave_up: the background has stopped retrying; only the Retry button tries.
    - snapshot_note: a short sentence for the tile, when the state needs one.
      All three only on /channels/{ip}.
    """
    ip: str
    network_name: str
    channel: int
    status: int
    snapshot_state: Optional[str] = None
    snapshot_last_try: Optional[float] = None
    snapshot_gave_up: Optional[bool] = None
    snapshot_note: Optional[str] = None

class CameraChannelOutput(BaseModel):
    status: str
    data: List[CameraChannel]

class CameraSelectInput(BaseModel):
    """
    Camera selection input schema.
    Fields:
    - ip: str - IP address of the camera.
    - channel: int - Channel number of the camera.
    - network_name: str - Name of the network/branch type this channel belongs to.
    """
    ip: str
    channel: int
    network_name: str


class CameraPlacement(BaseModel):
    """Inside or outside the shop, for one channel (camera_placement_crud).
    - placement: what the app goes by — the server's answer until a person chooses; None = not judged.
    - set_by: 'ai' or 'user'.
    - ai_*: the server's own answer, kept even after a person changes placement.
    - ai_error: why the server refused the picture, if it did.
    - needs_review: no person has chosen, and the answer is missing, uncertain or of low confidence.
    """
    ip: str
    channel: int
    placement: Optional[str] = None
    set_by: Optional[str] = None
    ai_choice: Optional[str] = None
    ai_confidence: Optional[float] = None
    ai_probabilities: Optional[dict] = None
    ai_area: Optional[str] = None
    ai_model: Optional[str] = None
    ai_at: Optional[str] = None
    ai_error: Optional[str] = None
    user_at: Optional[str] = None
    needs_review: bool = False


class CameraPlacementInput(BaseModel):
    """A person's choice for one channel: inside or outside."""
    placement: str

    @field_validator("placement")
    @classmethod
    def _inside_or_outside(cls, v: str) -> str:
        if v not in ("inside", "outside"):
            raise ValueError("placement must be inside or outside")
        return v
