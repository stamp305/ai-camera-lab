# frontend/app.py
"""AI Camera Admin Frontend - Flask Application Entry Point.

This is the main application file that initializes Flask, registers blueprints,
and configures Socket.IO. All routes are organized in the blueprints package.
"""
from __future__ import annotations

import logging
import os
import sys

import eventlet
eventlet.monkey_patch()
from flask import Flask

from config import Config
from extensions import socketio
from gate import check_request, record_activation_result


def create_app() -> Flask:
    """Application factory for creating the Flask app."""
    app = Flask(__name__)
    
    # Load configuration
    app.config.from_object(Config)
    app.config.update(
        DATA_SERVICE_API_BASE=Config.DATA_SERVICE_API_BASE,
        HTTP_TIMEOUT_SHORT=Config.HTTP_TIMEOUT_SHORT,
        HTTP_TIMEOUT_LONG=Config.HTTP_TIMEOUT_LONG,
    )
    
    # Request gate: admin, Branch REST or Activation, each with its own rule
    app.before_request(check_request)
    app.after_request(record_activation_result)
    
    # Configure logging
    app.logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.INFO)
    formatter = logging.Formatter("[%(asctime)s] %(levelname)s in %(module)s: %(message)s")
    handler.setFormatter(formatter)
    if not app.logger.handlers:
        app.logger.addHandler(handler)
    
    # Register blueprints
    from blueprints import bp
    app.register_blueprint(bp)
    
    # Initialize Socket.IO with path matching the blueprint prefix
    # ping_timeout ยาว: DLC ส่งไฟล์ทั้งไฟล์เป็น message เดียว (โมเดล 400MB ≈ 535MB base64)
    # ping ต้องรอต่อคิวหลัง frame นั้น ค่า default 20s ทำให้ทั้ง server และ client
    # ("packet queue is empty, aborting") ตัดสายกลางไฟล์ client รับค่านี้ตอน handshake
    socketio.init_app(app, cors_allowed_origins="*", async_mode="eventlet", path="/ai-camera/socket.io",
                      ping_interval=25, ping_timeout=600)
    
    # Import socket handlers to register event handlers
    import socket_handlers  # noqa: F401
    
    app.logger.info("[INIT] Application initialized successfully")
    app.logger.info(f"[INIT] Socket.IO async_mode=eventlet")
    app.logger.info(f"[INIT] DLC Master Path: {Config.DLC_MASTER_PATH}")
    app.logger.info(f"[INIT] Logs Base Path: {Config.LOGS_BASE_PATH}")
    app.logger.info(f"[INIT] Require Device Key: {Config.REQUIRE_DEVICE_KEY}")
    
    return app


# Create application instance
app = create_app()


# =====================================================================================
# ENTRYPOINT
# =====================================================================================
if __name__ == "__main__":  # pragma: no cover
    # Port is overridable so the admin can run beside a branch client on one
    # machine: the client's camera_service also binds 8002, and a loopback bind
    # beats this wildcard one, silently stealing every 127.0.0.1 request.
    # The remote origin (remote_origin.py): a branch's pages on a port of their own,
    # in this same eventlet hub, because the remote tunnel's state lives in this process.
    import eventlet.wsgi
    from remote_origin import remote_app
    eventlet.spawn(eventlet.wsgi.server,
                   eventlet.listen(("0.0.0.0", Config.REMOTE_ORIGIN_PORT)),
                   remote_app, log_output=False)
    app.logger.info(f"[INIT] Remote origin on port {Config.REMOTE_ORIGIN_PORT}")
    socketio.run(app, host="0.0.0.0", port=Config.FRONTEND_PORT, log_output=True)