# frontend/app_support_frontend.py
"""AI Camera Admin — Support Frontend.

หน้าเดียวกับแอดมิน แต่ตัดหมวด Configuration ออกสำหรับ IT support
- ใช้งานได้เต็มที่: ดู/เพิ่ม/ลบ/แก้ไขผู้ใช้, จัดบริการกับแพ็คเกจรายสาขา (manage_dlc),
  สั่งอัปเดตเวอร์ชันให้ผู้ใช้
- เข้าไม่ได้: จัดการบริการ / จัดการแพ็คเกจ / จัดการเวอร์ชัน / Log Dashboard
  ทั้งหน้าเว็บและ API ที่แก้ของกลางพวกนั้น

ที่นี่ใช้ denylist (ไม่ใช่ allowlist) เพราะ IT support ต้องใช้งานได้เกือบทุกอย่าง
=> ถ้าเพิ่ม endpoint ใหม่ที่เป็นงานฝั่ง Configuration ต้องมาเติมใน _DENIED ด้วย

รันคู่กับ app_ai_camera_frontend.py (8002) โดยชี้ไป data_service ตัวเดียวกัน
"""
from __future__ import annotations

import logging
import os
import re
import sys

from flask import Flask, abort, jsonify, request

from config import Config

# ---------------------------------------------------------------------------
# Denylist — งานฝั่ง Configuration ที่สงวนไว้ให้แอดมินเท่านั้น
# แต่ละรายการคือ (เมธอดที่ห้าม หรือ "*" = ทุกเมธอด, regex ของ path เต็ม)
# ---------------------------------------------------------------------------
_DENIED = [
    # หน้าเว็บหมวด Configuration
    ("*", r"/ai-camera/manage_service"),
    ("*", r"/ai-camera/manage_package"),
    ("*", r"/ai-camera/version-manager"),
    ("*", r"/ai-camera/logs.*"),
    ("*", r"/ai-camera/log-dashboard"),
    ("*", r"/ai-camera/api/logs/.*"),

    # แก้ไขบริการ/แพ็คเกจ "ของกลาง" (การอ่านยังเปิด เพราะหน้า manage_dlc ต้องใช้)
    ("*", r"/ai-camera/api/services/add"),
    ("DELETE", r"/ai-camera/api/services/[^/]+"),
    ("POST", r"/ai-camera/api/packages"),
    ("DELETE", r"/ai-camera/api/packages/[^/]+"),

    # จัดการเวอร์ชัน / build farm
    ("*", r"/ai-camera/api/bitbucket/.*"),
    ("*", r"/ai-camera/api/managed-versions"),
    ("*", r"/ai-camera/api/build/upload"),
    ("POST", r"/ai-camera/api/branches"),
    ("DELETE", r"/ai-camera/api/branches/[^/]+"),
    ("*", r"/ai-camera/api/branches/[^/]+/assign-version"),
    ("*", r"/ai-camera/api/branches/[^/]+/force-update"),
    ("*", r"/ai-camera/api/branches/[^/]+/trigger-build"),

    # สั่งอัปเดต/อัปโหลดตัวไคลเอนต์ทั้งระบบ
    ("*", r"/ai-camera/api/force-main-update/.*"),
    ("*", r"/ai-camera/api/broadcast-force-update"),
    ("*", r"/ai-camera/api/system/upload-client-zip"),
]

_DENIED_RE = [(m, re.compile(p)) for m, p in _DENIED]


def _is_denied(method: str, path: str) -> bool:
    return any((m == "*" or m == method) and rx.fullmatch(path)
               for m, rx in _DENIED_RE)


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.from_object(Config)
    app.config.update(
        DATA_SERVICE_API_BASE=Config.DATA_SERVICE_API_BASE,
        HTTP_TIMEOUT_SHORT=Config.HTTP_TIMEOUT_SHORT,
        HTTP_TIMEOUT_LONG=Config.HTTP_TIMEOUT_LONG,
        SUPPORT_MODE=True,
    )

    @app.before_request
    def guard():
        forwarded_for = request.headers.get("X-Forwarded-For", "")
        client_ip = forwarded_for.split(",")[0] if forwarded_for else request.remote_addr
        if client_ip not in Config.OFFICE_IPS:
            app.logger.info(f"[SUPPORT] blocked IP: {client_ip}")
            abort(403)

        if _is_denied(request.method, request.path):
            app.logger.info(f"[SUPPORT] blocked: {request.method} {request.path}")
            return jsonify({"success": False,
                            "message": "เมนูนี้สงวนไว้สำหรับผู้ดูแลระบบ"}), 403

    app.logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)s in %(module)s: %(message)s"))
    if not app.logger.handlers:
        app.logger.addHandler(handler)

    from blueprints import bp
    app.register_blueprint(bp)

    app.logger.info("[INIT] Support frontend initialized (no Configuration)")
    return app


app = create_app()


if __name__ == "__main__":  # pragma: no cover
    app.run(host="0.0.0.0",
            port=int(os.getenv("AI_CAMERA_SUPPORT_PORT", "8003")),
            debug=False)
