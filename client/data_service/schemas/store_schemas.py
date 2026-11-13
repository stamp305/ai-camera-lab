from pydantic import BaseModel, Field
from typing import Optional
from datetime import time

class BranchModel(BaseModel):
    """
    Branch model schema.
    Fields:
    - id: str - Unique identifier for the branch.
    - name: str - Name of the branch.
    - region: str - Region where the branch is located.
    - province: str - Province where the branch is located.
    - open_time: time - Time when program starts.
    - close_time: time - Time when program shuts down.
    - staff_check_minutes: int | None - Security: minutes before opening in which a person
      in the store waits for the lights (staff opening up) before an alert. None: the
      monitor's default (an hour and a half). Left out of a request: kept as it was.
    """
    id: str
    name: str
    region: str
    province: str
    open_time: time
    close_time: time
    staff_check_minutes: Optional[int] = Field(default=None, ge=0, le=12 * 60)