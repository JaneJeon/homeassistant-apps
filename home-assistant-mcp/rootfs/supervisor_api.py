"""Supervisor transport for the pinned ha-mcp add-on tools."""

import os
import re
from typing import Any

import httpx

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.tools.helpers import raise_tool_error


async def supervisor_api_call(
    client: Any,
    endpoint: str,
    method: str = "GET",
    data: dict[str, Any] | None = None,
    timeout: int | None = None,
) -> dict[str, Any]:
    """Call Supervisor with the app token rather than Core's WebSocket proxy."""
    addon_info = re.fullmatch(r"/addons/([A-Za-z0-9_.-]+)/info", endpoint)
    if (
        method.upper() != "GET"
        or (
            endpoint not in ("/addons", "/store")
            and (not addon_info or addon_info.group(1) in (".", ".."))
        )
    ):
        raise_tool_error(create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            "Only add-on discovery and validated add-on info requests are supported",
        ))
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise_tool_error(create_error_response(
            ErrorCode.AUTH_INVALID_TOKEN,
            "Supervisor token is unavailable in the app environment",
        ))

    try:
        async with httpx.AsyncClient(
            base_url="http://supervisor",
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout if timeout is not None else 30,
            trust_env=False,
        ) as supervisor:
            response = await supervisor.request(method, endpoint, json=data)
    except httpx.TimeoutException:
        raise_tool_error(create_error_response(
            ErrorCode.TIMEOUT_OPERATION,
            "Supervisor API request timed out",
            context={"endpoint": endpoint},
        ))
    except httpx.RequestError:
        raise_tool_error(create_error_response(
            ErrorCode.CONNECTION_FAILED,
            "Unable to reach the Supervisor API",
            context={"endpoint": endpoint},
        ))

    if response.status_code == 403:
        raise_tool_error(create_error_response(
            ErrorCode.AUTH_INSUFFICIENT_PERMISSIONS,
            "The app's Supervisor role does not permit this operation",
            suggestions=["Verify the Home Assistant MCP app has hassio_role: manager"],
            context={"endpoint": endpoint},
        ))
    if response.status_code == 401:
        raise_tool_error(create_error_response(
            ErrorCode.AUTH_INVALID_TOKEN,
            "Supervisor rejected the app token",
            context={"endpoint": endpoint},
        ))
    if response.status_code == 404:
        raise_tool_error(create_error_response(
            ErrorCode.RESOURCE_NOT_FOUND,
            "Supervisor API resource was not found",
            context={"endpoint": endpoint},
        ))
    if response.is_error:
        raise_tool_error(create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            "Supervisor API request failed",
            context={"endpoint": endpoint, "status": response.status_code},
        ))

    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict) or payload.get("result") != "ok":
        raise_tool_error(create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            "Supervisor returned an unsuccessful API response",
            context={"endpoint": endpoint},
        ))
    return {"success": True, "result": payload.get("data", {})}
