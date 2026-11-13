import os

MICROSERVICES_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATA_SERVICES_DIR = os.path.join(MICROSERVICES_DIR, "data_service")
STATIC_DIR = os.path.join(DATA_SERVICES_DIR, "static")
IMAGE_DIR = os.path.join(STATIC_DIR, "images")
FLOORPLAN_IMAGE_DIR = os.path.join(IMAGE_DIR, "floorplans")