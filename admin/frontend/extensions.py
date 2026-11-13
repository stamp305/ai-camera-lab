# frontend/extensions.py
"""Shared Flask extensions to avoid circular imports."""
from __future__ import annotations

from flask_socketio import SocketIO

# SocketIO instance - initialized without app, will be configured in app.py
socketio = SocketIO()
