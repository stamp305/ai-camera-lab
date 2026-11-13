# data_service/core/exceptions.py
"""Custom exception classes for the data service."""
from __future__ import annotations

from typing import Optional


class DataServiceError(Exception):
    """Base exception for all data service errors."""
    
    def __init__(self, message: str, detail: Optional[str] = None):
        self.message = message
        self.detail = detail or message
        super().__init__(self.message)


class NotFoundError(DataServiceError):
    """Raised when a requested resource is not found."""
    
    def __init__(self, resource: str, identifier: str):
        message = f"{resource} not found"
        detail = f"{resource} with identifier '{identifier}' was not found"
        super().__init__(message, detail)
        self.resource = resource
        self.identifier = identifier


class DuplicateError(DataServiceError):
    """Raised when attempting to create a resource that already exists."""
    
    def __init__(self, resource: str, field: str, value: str):
        message = f"{field} already exists"
        detail = f"{resource} with {field} '{value}' already exists"
        super().__init__(message, detail)
        self.resource = resource
        self.field = field
        self.value = value


class DeviceKeyExistsError(DuplicateError):
    """Raised when an activated Branch that already holds a Device Key activates again."""

    def __init__(self, serial_number: str):
        super().__init__("User", "device_key", serial_number)
        # client-contract.md promises exactly this error string on the 409.
        self.message = "device_key_exists"
        self.detail = f"User '{serial_number}' already holds a device key; an admin must clear it first"


class ValidationError(DataServiceError):
    """Raised when input validation fails."""
    
    def __init__(self, message: str, field: Optional[str] = None):
        detail = f"Validation error on field '{field}': {message}" if field else message
        super().__init__(message, detail)
        self.field = field


class DatabaseError(DataServiceError):
    """Raised when a database operation fails."""
    
    def __init__(self, operation: str, original_error: Optional[Exception] = None):
        message = f"Database operation failed: {operation}"
        detail = f"{message}. Original error: {original_error}" if original_error else message
        super().__init__(message, detail)
        self.operation = operation
        self.original_error = original_error


class AuthenticationError(DataServiceError):
    """Raised when authentication fails."""
    
    def __init__(self, reason: str):
        message = reason
        detail = f"Authentication failed: {reason}"
        super().__init__(message, detail)
        self.reason = reason
