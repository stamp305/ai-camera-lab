import port_store

API_HOST = port_store.API_HOST
_port = port_store.get_port

# Ports are read through _port(), never typed as literals: when camera_service
# moved 8003 -> 8002 a separately typed URL stayed on 8003 (ConnectionRefused).
_CAMERA = _port("camera_service", 8003)

TRIGGER_APIS = {
    "update_camera": f"http://{API_HOST}:{_CAMERA}/update_cameras",
    "activate_success": f"http://{API_HOST}:{_port('activation_service', 8001)}/trigger_activate",
}