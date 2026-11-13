# data_service/api/packages.py
"""Package API endpoints."""
from __future__ import annotations

from typing import List

from fastapi import APIRouter

from data_service.core.schemas.package_schemas import PackageCreateRequest, PackageResponse
from data_service.core.services import package_service

router = APIRouter(prefix="/packages", tags=["packages"])


@router.get("", response_model=List[PackageResponse])
def list_packages():
    """Get all packages."""
    return package_service.list_packages()


@router.post("")
def create_package(pkg: PackageCreateRequest):
    """Create a new package."""
    return package_service.create_package(pkg.name)


@router.delete("/{package_id}")
def delete_package(package_id: int):
    """Delete a package by ID."""
    return package_service.delete_package(package_id)
