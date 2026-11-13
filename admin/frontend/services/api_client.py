# frontend/services/api_client.py
"""External API client helpers for communicating with the backend service."""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

import requests
from flask import current_app


def api_base() -> str:
    """Get the base URL for the data service API."""
    return current_app.config["DATA_SERVICE_API_BASE"]


def api_get(path: str, timeout: Optional[int] = None) -> requests.Response:
    """Make a GET request to the data service API."""
    t = timeout or current_app.config["HTTP_TIMEOUT_SHORT"]
    return requests.get(f"{api_base()}{path}", timeout=t)


def api_post(path: str, payload: Any = None, timeout: Optional[int] = None) -> requests.Response:
    """Make a POST request to the data service API."""
    t = timeout or current_app.config["HTTP_TIMEOUT_LONG"]
    return requests.post(f"{api_base()}{path}", json=payload, timeout=t)


def api_put(path: str, payload: Any = None, timeout: Optional[int] = None) -> requests.Response:
    """Make a PUT request to the data service API."""
    t = timeout or current_app.config["HTTP_TIMEOUT_LONG"]
    return requests.put(f"{api_base()}{path}", json=payload, timeout=t)


def api_delete(path: str, timeout: Optional[int] = None) -> requests.Response:
    """Make a DELETE request to the data service API."""
    t = timeout or current_app.config["HTTP_TIMEOUT_SHORT"]
    return requests.delete(f"{api_base()}{path}", timeout=t)


def api_patch(path: str, payload: Any = None, timeout: Optional[int] = None) -> requests.Response:
    """Make a PATCH request to the data service API."""
    t = timeout or current_app.config["HTTP_TIMEOUT_LONG"]
    return requests.patch(f"{api_base()}{path}", json=payload, timeout=t)


def api_post_multipart(
    path: str,
    data: Optional[Dict[str, Any]] = None,
    files: Optional[Dict[str, Any]] = None,
    timeout: Optional[int] = None,
) -> requests.Response:
    """POST multipart/form-data to the data service API (file streaming)."""
    t = timeout or current_app.config["HTTP_TIMEOUT_STORE"]
    return requests.post(f"{api_base()}{path}", data=data, files=files, timeout=t)


# -------------------------------------------------------------------------------------
# User Search Utilities
# -------------------------------------------------------------------------------------
def _iter_users(group_list: Iterable[Dict[str, Any]]) -> Iterable[Dict[str, Any]]:
    """Iterate over all users across all groups."""
    for group in group_list:
        for user in group.get("users", []):
            yield user


def search_user(groups: List[Dict[str, Any]], activation_code: str) -> Optional[Dict[str, Any]]:
    """Search for a user by activation code across all groups."""
    for user in _iter_users(groups):
        if user.get("activation_code") == activation_code:
            return user
    return None
