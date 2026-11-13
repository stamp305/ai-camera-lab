# data_service/core/services/package_service.py
"""Business logic for package management."""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from data_service.core.exceptions import DatabaseError
from data_service.core.database.crud.package_crud import (
    read_all_packages,
    create_package_in_db,
    delete_package_in_db,
)
from data_service.core.schemas.package_schemas import PackageResponse

logger = logging.getLogger(__name__)


def list_packages() -> List[PackageResponse]:
    """Get all packages."""
    try:
        rows = read_all_packages()
        return [PackageResponse(**row) for row in rows]
    except Exception as e:
        logger.error(f"Failed to list packages: {e}")
        raise DatabaseError("read packages", e)


def create_package(name: str) -> Dict[str, str]:
    """Create a new package."""
    try:
        create_package_in_db(name)
        logger.info(f"Created package: {name}")
        return {"message": "Package created successfully"}
    except Exception as e:
        logger.error(f"Failed to create package '{name}': {e}")
        raise DatabaseError("create package", e)


def delete_package(package_id: int) -> Dict[str, str]:
    """Delete a package by ID."""
    try:
        delete_package_in_db(package_id)
        logger.info(f"Deleted package with ID={package_id}")
        return {"message": f"Package {package_id} deleted successfully"}
    except Exception as e:
        logger.error(f"Failed to delete package {package_id}: {e}")
        raise DatabaseError("delete package", e)
