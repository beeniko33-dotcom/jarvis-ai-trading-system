from __future__ import annotations

import asyncio
import unittest
from typing import Any

from mcp.server.fastmcp.exceptions import ToolError

from jarvis_server.mcp_server import BearerAuthMiddleware, create_mcp_server


class FakeJarvisApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def request(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append((path, payload))
        return {"ok": True}


class McpServerTests(unittest.TestCase):
    def test_trading_tools_are_hidden_by_default(self) -> None:
        server = create_mcp_server(FakeJarvisApi(), enable_trading=False)
        names = {tool.name for tool in asyncio.run(server.list_tools())}

        self.assertIn("get_market", names)
        self.assertIn("get_account", names)
        self.assertNotIn("place_option_order", names)
        self.assertNotIn("start_autobot", names)

    def test_trading_tools_can_be_enabled_explicitly(self) -> None:
        server = create_mcp_server(FakeJarvisApi(), enable_trading=True)
        names = {tool.name for tool in asyncio.run(server.list_tools())}

        self.assertIn("place_option_order", names)
        self.assertIn("run_autobot_once", names)
        self.assertIn("start_autobot", names)
        self.assertIn("stop_autobot", names)

    def test_trade_tool_requires_confirmation_before_api_call(self) -> None:
        api = FakeJarvisApi()
        server = create_mcp_server(api, enable_trading=True)

        with self.assertRaises(ToolError):
            asyncio.run(server.call_tool("place_option_order", {
                "symbol": "R_100",
                "contract_type": "CALL",
                "stake": 1,
                "duration": 5,
                "duration_unit": "m",
                "confirm": False,
            }))
        self.assertEqual(api.calls, [])


class BearerAuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_bearer_token_is_rejected(self) -> None:
        forwarded = False
        responses: list[dict[str, Any]] = []

        async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
            nonlocal forwarded
            forwarded = True

        async def send(event: dict[str, Any]) -> None:
            responses.append(event)

        middleware = BearerAuthMiddleware(app, "a" * 32)
        await middleware({"type": "http", "headers": []}, None, send)

        self.assertFalse(forwarded)
        self.assertEqual(responses[0]["status"], 401)

    async def test_unrelated_host_is_rejected_after_authentication(self) -> None:
        forwarded = False
        responses: list[dict[str, Any]] = []

        async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
            nonlocal forwarded
            forwarded = True

        async def send(event: dict[str, Any]) -> None:
            responses.append(event)

        middleware = BearerAuthMiddleware(app, "a" * 32)
        await middleware({
            "type": "http",
            "headers": [
                (b"authorization", b"Bearer " + b"a" * 32),
                (b"host", b"evil.example.com"),
            ],
        }, None, send)

        self.assertFalse(forwarded)
        self.assertEqual(responses[0]["status"], 421)

    async def test_quick_tunnel_host_is_accepted_after_authentication(self) -> None:
        forwarded = False

        async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
            nonlocal forwarded
            forwarded = True

        async def send(event: dict[str, Any]) -> None:
            return None

        middleware = BearerAuthMiddleware(app, "a" * 32)
        await middleware({
            "type": "http",
            "headers": [
                (b"authorization", b"Bearer " + b"a" * 32),
                (b"host", b"mature-vienna-seo-bracelets.trycloudflare.com"),
            ],
        }, None, send)

        self.assertTrue(forwarded)