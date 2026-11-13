# frontend/blueprints/__init__.py
"""Blueprints package for route organization."""
import os
from flask import Blueprint

# Get the frontend directory path
_FRONTEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Main blueprint with /ai-camera prefix, serving static files and templates
bp = Blueprint(
    "ai_camera",
    __name__,
    url_prefix="/ai-camera",
    static_folder=os.path.join(_FRONTEND_DIR, "static"),
    static_url_path="/static",
    template_folder=os.path.join(_FRONTEND_DIR, "templates"),
)

# Import all route modules to register routes on the blueprint
from blueprints import pages  # noqa: F401, E402
from blueprints import api_users  # noqa: F401, E402
from blueprints import api_services  # noqa: F401, E402
from blueprints import api_dlc  # noqa: F401, E402
from blueprints import api_system  # noqa: F401, E402
from blueprints import api_versions  # noqa: F401, E402
from blueprints import api_dummy_data  # noqa: F401, E402
from blueprints import api_traffic  # noqa: F401, E402
from blueprints import api_camera_alert  # noqa: F401, E402
from blueprints import api_camera_placement  # noqa: F401, E402
from blueprints import api_logs  # noqa: F401, E402
