# data_service/core/services/service_service.py
"""Business logic for service management."""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from data_service.core.exceptions import DatabaseError
from data_service.core.database.crud.service_crud import (
    create_service_in_db,
    read_all_services_from_db,
    delete_service_from_db,
    read_services_by_package_from_db,
)
from data_service.core.schemas.service_schemas import ServiceCreateRequest

logger = logging.getLogger(__name__)


def create_service(service_request: ServiceCreateRequest) -> Dict[str, str]:
    """Create a new service."""
    try:
        created = create_service_in_db(service_request)
        if created:
            logger.info(f"Created service: {service_request.name}")
            return {"message": "Service created successfully"}
        logger.info(f"Updated existing service for folder: {service_request.dlc_folder}")
        return {"message": f"โฟลเดอร์ '{service_request.dlc_folder}' มีอยู่ในแพ็คเกจ "
                           f"'{service_request.package}' อยู่แล้ว — อัปเดตชื่อกับคำอธิบายให้แทน"}
    except Exception as e:
        logger.error(f"Failed to create service: {e}")
        raise DatabaseError("create service", e)


def get_all_services() -> List[Dict[str, Any]]:
    """Get all available services."""
    try:
        services = read_all_services_from_db()
        return services if services else []
    except Exception as e:
        logger.error(f"Failed to read services: {e}")
        raise DatabaseError("read services", e)


def delete_service(service_id: int) -> Dict[str, str]:
    """Delete a service by ID."""
    try:
        delete_service_from_db(service_id)
        logger.info(f"Deleted service with ID={service_id}")
        return {"message": f"Service with ID {service_id} deleted successfully"}
    except Exception as e:
        logger.error(f"Failed to delete service {service_id}: {e}")
        raise DatabaseError("delete service", e)


def get_services_by_package(package_name: str) -> List[Dict[str, Any]]:
    """Get services filtered by package name."""
    try:
        services = read_services_by_package_from_db(package_name)
        return services
    except Exception as e:
        logger.error(f"Failed to get services for package '{package_name}': {e}")
        raise DatabaseError("read services by package", e)
