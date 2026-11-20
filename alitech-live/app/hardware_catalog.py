"""Hardware Integration catalogue: product type -> brands (one row each) -> devices (left to right).

Only the Tuya siren is real (it opens its setup page). Everything marked demo is a mock-up
for the layout and opens nothing.
"""
from __future__ import annotations

SIREN_SETUP = "/my-products/ai-camera/security/hardware/tuya-siren"

CATEGORIES = [
    {"key": "siren", "name": "ไซเรนแจ้งเตือน", "brands": [
        {"name": "Tuya", "devices": [
            {"name": "3 in 1 WiFi Siren Alarm", "model": "A03Pro", "icon": "/static/vendor/icons/Bell_Ring.svg", "href": SIREN_SETUP},
            {"name": "Outdoor Strobe Siren", "model": "S20", "icon": "/static/img/hw/siren.svg"},
        ]},
        {"name": "Sonoff", "devices": [
            {"name": "Zigbee Siren", "model": "ZBSR", "icon": "/static/img/hw/siren.svg"},
        ]},
    ]},
    {"key": "door-lock", "name": "กลอนประตูดิจิทัล (Door Lock)", "brands": [
        {"name": "Tuya", "devices": [
            {"name": "Smart Door Lock", "model": "L8", "icon": "/static/img/hw/lock.svg"},
            {"name": "Fingerprint Lock", "model": "F1", "icon": "/static/img/hw/lock.svg"},
        ]},
        {"name": "Yale", "devices": [
            {"name": "Digital Door Lock", "model": "YDM4109", "icon": "/static/img/hw/lock.svg"},
        ]},
        {"name": "Samsung", "devices": [
            {"name": "Smart Door Lock", "model": "SHP-DP609", "icon": "/static/img/hw/lock.svg"},
        ]},
    ]},
    {"key": "sensor", "name": "เซนเซอร์ประตู / หน้าต่าง", "brands": [
        {"name": "Tuya", "devices": [
            {"name": "Door & Window Sensor", "model": "DS01", "icon": "/static/img/hw/sensor.svg"},
            {"name": "PIR Motion Sensor", "model": "PIR02", "icon": "/static/img/hw/sensor.svg"},
        ]},
        {"name": "Aqara", "devices": [
            {"name": "Door and Window Sensor", "model": "P2", "icon": "/static/img/hw/sensor.svg"},
        ]},
    ]},
    {"key": "plug", "name": "ปลั๊กอัจฉริยะ (Smart Plug)", "brands": [
        {"name": "Tuya", "devices": [
            {"name": "WiFi Smart Plug", "model": "SP10", "icon": "/static/img/hw/plug.svg"},
        ]},
        {"name": "TP-Link Tapo", "devices": [
            {"name": "Mini Smart Plug", "model": "P100", "icon": "/static/img/hw/plug.svg"},
        ]},
    ]},
]

KEYS = {c["key"] for c in CATEGORIES}
DEFAULT = "siren"
