"""Regression checks for app-mode Supervisor transport and safe errors."""

import os
import unittest
from unittest.mock import patch

import httpx
from fastmcp.exceptions import ToolError

import supervisor_api


class SupervisorTransportTests(unittest.IsolatedAsyncioTestCase):
    async def call_with_transport(self, handler, **kwargs):
        original_client = httpx.AsyncClient

        def client_factory(**options):
            return original_client(transport=httpx.MockTransport(handler), **options)

        with patch.dict(os.environ, {"SUPERVISOR_TOKEN": "fixture-app-token"}), patch(
            "supervisor_api.httpx.AsyncClient", side_effect=client_factory
        ):
            return await supervisor_api.supervisor_api_call(
                object(), "/addons/core_samba/info", **kwargs
            )

    async def test_direct_route_and_app_token(self):
        def handler(request):
            self.assertEqual(str(request.url), "http://supervisor/addons/core_samba/info")
            self.assertEqual(request.headers["Authorization"], "Bearer fixture-app-token")
            self.assertEqual(request.method, "GET")
            return httpx.Response(200, json={"result": "ok", "data": {"slug": "core_samba"}})

        self.assertEqual(
            await self.call_with_transport(handler),
            {"success": True, "result": {"slug": "core_samba"}},
        )

    async def test_non_read_methods_fail_before_request(self):
        with patch("supervisor_api.httpx.AsyncClient") as client:
            with self.assertRaises(ToolError) as error:
                await supervisor_api.supervisor_api_call(object(), "/addons", method="POST")
            self.assertIn("VALIDATION_INVALID_PARAMETER", str(error.exception))
            client.assert_not_called()

    async def test_slug_cannot_escape_the_info_endpoint(self):
        slugs = ["../../backups?", "../../addons/core_samba/options/config?", ".", "..",
                 "core_samba?x=y", "/core_samba", "core_samba%2Fother", "core_samba#fragment",
                 "core_samba\\other"]
        for slug in slugs:
            with self.subTest(slug=slug), patch("supervisor_api.httpx.AsyncClient") as client:
                with self.assertRaises(ToolError) as error:
                    await supervisor_api.supervisor_api_call(object(), f"/addons/{slug}/info")
                self.assertIn("VALIDATION_INVALID_PARAMETER", str(error.exception))
                client.assert_not_called()

    async def test_permission_failure_is_not_invalid_token(self):
        with self.assertRaises(ToolError) as error:
            await self.call_with_transport(
                lambda request: httpx.Response(403, text="private response body")
            )
        self.assertIn("AUTH_INSUFFICIENT_PERMISSIONS", str(error.exception))
        self.assertNotIn("AUTH_INVALID_TOKEN", str(error.exception))
        self.assertNotIn("private response body", str(error.exception))
        self.assertNotIn("fixture-app-token", str(error.exception))

    async def test_invalid_token_and_missing_resource(self):
        for status, code in [(401, "AUTH_INVALID_TOKEN"), (404, "RESOURCE_NOT_FOUND")]:
            with self.subTest(status=status), self.assertRaises(ToolError) as error:
                await self.call_with_transport(lambda request: httpx.Response(status))
            self.assertIn(code, str(error.exception))

    async def test_invalid_and_unsuccessful_api_responses(self):
        for response in [httpx.Response(502), httpx.Response(200, text="not json"),
                         httpx.Response(200, json={"result": "error", "message": "private body"})]:
            with self.subTest(response=response.status_code), self.assertRaises(ToolError) as error:
                await self.call_with_transport(lambda request: response)
            self.assertIn("SERVICE_CALL_FAILED", str(error.exception))
            self.assertNotIn("private body", str(error.exception))

    async def test_network_and_timeout_errors_do_not_copy_details(self):
        for error_type, code in [(httpx.ConnectError, "CONNECTION_FAILED"),
                                 (httpx.ReadTimeout, "TIMEOUT_OPERATION")]:
            def handler(request):
                raise error_type("private transport details", request=request)

            with self.subTest(error=error_type), self.assertRaises(ToolError) as error:
                await self.call_with_transport(handler)
            self.assertIn(code, str(error.exception))
            self.assertNotIn("private transport details", str(error.exception))

    async def test_missing_token_fails_before_request(self):
        with patch.dict(os.environ, {}, clear=True), patch("supervisor_api.httpx.AsyncClient") as client:
            with self.assertRaises(ToolError) as error:
                await supervisor_api.supervisor_api_call(object(), "/addons")
            self.assertIn("AUTH_INVALID_TOKEN", str(error.exception))
            client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
