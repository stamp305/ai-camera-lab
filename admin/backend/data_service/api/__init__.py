# data_service/api/__init__.py
"""API routes and exception handlers."""
from data_service.api import users, services, auth, packages, dummy_data
from data_service.api.exception_handlers import register_exception_handlers

__all__ = ["users", "services", "auth", "packages", "dummy_data", "register_exception_handlers"]
