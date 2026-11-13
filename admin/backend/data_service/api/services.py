# data_service/api/services.py
"""Service API endpoints."""
from __future__ import annotations

from fastapi import APIRouter

from data_service.core.schemas.service_schemas import ServiceCreateRequest
from data_service.core.services import service_service

router = APIRouter(prefix="/services", tags=["services"])


# =============================================================
# Service Endpoints
# =============================================================
@router.post("")
def create_service(service_request: ServiceCreateRequest):
    """Create a new service."""
    return service_service.create_service(service_request)


@router.get("")
def read_services():
    """Get all available services."""
    return service_service.get_all_services()


@router.delete("/{service_id}")
def delete_service(service_id: int):
    """Delete a service by ID."""
    return service_service.delete_service(service_id)


@router.get("/packages/{package_name}")
def get_services_by_package(package_name: str):
    """Get services filtered by package name."""
    return service_service.get_services_by_package(package_name)