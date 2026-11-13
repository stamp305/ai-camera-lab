# data_service/api/exception_handlers.py
"""FastAPI exception handlers for consistent error responses."""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from data_service.core.exceptions import (
    DataServiceError,
    NotFoundError,
    DuplicateError,
    ValidationError,
    DatabaseError,
    AuthenticationError,
)


def register_exception_handlers(app):
    """Register all exception handlers with the FastAPI app."""
    
    @app.exception_handler(NotFoundError)
    async def not_found_handler(request: Request, exc: NotFoundError):
        return JSONResponse(
            status_code=404,
            content={"error": exc.message, "detail": exc.detail}
        )
    
    @app.exception_handler(DuplicateError)
    async def duplicate_handler(request: Request, exc: DuplicateError):
        return JSONResponse(
            status_code=409,
            content={"error": exc.message, "detail": exc.detail}
        )
    
    @app.exception_handler(ValidationError)
    async def validation_handler(request: Request, exc: ValidationError):
        return JSONResponse(
            status_code=400,
            content={"error": exc.message, "detail": exc.detail}
        )
    
    @app.exception_handler(DatabaseError)
    async def database_handler(request: Request, exc: DatabaseError):
        return JSONResponse(
            status_code=500,
            content={"error": exc.message, "detail": exc.detail}
        )
    
    @app.exception_handler(AuthenticationError)
    async def authentication_handler(request: Request, exc: AuthenticationError):
        return JSONResponse(
            status_code=401,
            content={"error": exc.message, "detail": exc.detail}
        )
    
    @app.exception_handler(DataServiceError)
    async def generic_handler(request: Request, exc: DataServiceError):
        return JSONResponse(
            status_code=500,
            content={"error": exc.message, "detail": exc.detail}
        )
