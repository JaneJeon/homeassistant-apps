"""Disposable integration fixture: REST works; privileged Core WS calls do not."""

import asyncio
import json

from websockets.asyncio.server import serve


def http_response(connection, status, payload):
    response = connection.respond(status, json.dumps(payload))
    response.headers["Content-Type"] = "application/json"
    return response


async def process_request(connection, request):
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return None
    if request.headers.get("Authorization") != "Bearer fixture-app-token":
        return http_response(connection, 401, {"result": "error"})
    if request.path == "/addons":
        return http_response(connection, 200, {"result": "ok", "data": {"addons": [{
            "name": "Fixture Samba", "slug": "core_samba", "state": "started",
            "version": "fixture", "update_available": False,
        }]}})
    if request.path == "/addons/core_samba/info":
        return http_response(connection, 200, {"result": "ok", "data": {
            "name": "Fixture Samba", "slug": "core_samba", "state": "started",
            "options": {"username": "fixture", "password": "fixture-value"},
        }})
    if request.path == "/store":
        return http_response(connection, 200, {"result": "ok", "data": {
            "addons": [], "repositories": [],
        }})
    if request.path == "/core/api/config":
        return http_response(connection, 200, {"version": "2026.9.4",
            "components": [], "time_zone": "UTC", "location_name": "Fixture"})
    if request.path in ("/core/api/states", "/core/api/services"):
        return http_response(connection, 200, [])
    if request.path == "/core/api/states/sensor.fixture":
        return http_response(connection, 200, {"entity_id": "sensor.fixture", "state": "ready",
            "attributes": {"friendly_name": "Fixture Sensor"},
            "last_changed": "2026-10-03T00:00:00+00:00", "last_updated": "2026-10-03T00:00:00+00:00"})
    return http_response(connection, 404, {"result": "error"})


async def websocket(connection):
    await connection.send(json.dumps({"type": "auth_required", "ha_version": "2026.9.4"}))
    await connection.recv()
    await connection.send(json.dumps({"type": "auth_ok", "ha_version": "2026.9.4"}))
    async for message in connection:
        request = json.loads(message)
        await connection.send(json.dumps({"id": request.get("id"), "type": "result",
            "success": False, "error": {"code": "unauthorized", "message": "Unauthorized"}}))


async def main():
    async with serve(websocket, "0.0.0.0", 80, process_request=process_request):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
