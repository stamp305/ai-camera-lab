from dotenv import load_dotenv
import os

load_dotenv()

DB_USER = os.getenv("AI_CAMERA_ADMIN_DB_USER")
DB_PASSWORD = os.getenv("AI_CAMERA_ADMIN_DB_PASSWORD")
DB_HOST = os.getenv("AI_CAMERA_ADMIN_DB_HOST", "localhost")
DB_PORT = int(os.getenv("AI_CAMERA_ADMIN_DB_PORT", 3306))
DB_NAME = os.getenv("AI_CAMERA_ADMIN_DB_NAME")