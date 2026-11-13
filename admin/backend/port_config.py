from __future__ import annotations

import os
from typing import Dict



# --------------------------------------------------------------------------------------
# Core host/port mappings
# --------------------------------------------------------------------------------------
API_HOST: str = "0.0.0.0"
API_PORTS: Dict[str, str] = {
    # Mock admin: every port can move, so it runs beside the real test admin.
    "data_service": os.getenv("AI_CAMERA_ADMIN_BACKEND_PORT", "8011"),
}

SOCKET_PORTS: Dict[str, str] = {
    "client_update": os.getenv("AI_CAMERA_ADMIN_CLIENT_UPDATE_PORT", "65432"),
    "update_control": os.getenv("AI_CAMERA_ADMIN_UPDATE_CONTROL_PORT", "65433")
}