"""Remote MCP interface for the Jarvis Deriv terminal."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hmac
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings


class JarvisApi:
    def __init__(self) -> None:
        self.base_url = os.environ.get("JARVIS_API_URL", "http://127.0.0.1:8000").rstrip("/")
        username = os.environ.get("JARVIS_ACCESS_USERNAME", "")
        password = os.environ.get("JARVIS_ACCESS_PASSWORD", "")
        if bool(username) != bool(password):
            raise ValueError("Set both JARVIS_ACCESS_USERNAME and JARVIS_ACCESS_PASSWORD")
        self.authorization = ""
        if username:
            encoded = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
            self.authorization = f"Basic {encoded}"

    def request(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self.authorization:
            headers["Authorization"] = self.authorization
        request = urllib.request.Request(f"{self.base_url}{path}", data=body, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                result = json.loads(exc.read().decode("utf-8"))
                message = result.get("error", f"Jarvis API returned HTTP {exc.code}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                message = f"Jarvis API returned HTTP {exc.code}"
            raise RuntimeError(message) from exc
        if not isinstance(result, dict):
            raise RuntimeError("Jarvis API returned an invalid response")
        return result


class BearerAuthMiddleware:
    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self.expected = f"Bearer {token}".encode("utf-8")

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        supplied = headers.get(b"authorization", b"")
        if not hmac.compare_digest(supplied, self.expected):
            body = b'{"error":"Authentication required"}'
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b"Bearer"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"cache-control", b"no-store"),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return
        raw_host = headers.get(b"host", b"").decode("ascii", "ignore")
        hostname = urllib.parse.urlsplit(f"//{raw_host}").hostname
        if not self._host_is_allowed(hostname):
            body = b'{"error":"Invalid Host header"}'
            await send({
                "type": "http.response.start",
                "status": 421,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"cache-control", b"no-store"),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)

    @staticmethod
    def _host_is_allowed(hostname: str | None) -> bool:
        if not hostname:
            return False
        host = hostname.lower().rstrip(".")
        if host in {"localhost", "127.0.0.1", "::1"}:
            return True
        suffix = ".trycloudflare.com"
        if not host.endswith(suffix):
            return False
        label = host[:-len(suffix)]
        return bool(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label))


def create_mcp_server(api: JarvisApi | None = None, *, enable_trading: bool | None = None) -> FastMCP:
    backend = api or JarvisApi()
    trading_enabled = (
        os.environ.get("JARVIS_MCP_ENABLE_TRADING", "false").lower() == "true"
        if enable_trading is None
        else enable_trading
    )
    server = FastMCP(
        "Jarvis Deriv Trading",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    async def call_api(path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return await asyncio.to_thread(backend.request, path, payload)

    @server.tool()
    async def get_health() -> dict[str, Any]:
        """Check whether the local Jarvis trading service is available."""
        return await call_api("/api/health")

    @server.tool()
    async def get_market() -> dict[str, Any]:
        """Get current provider market data from Deriv."""
        return await call_api("/api/market")

    @server.tool()
    async def get_quote(symbol: str) -> dict[str, Any]:
        """Get a current Deriv quote for a symbol."""
        return await call_api(f"/api/market/quote?symbol={urllib.parse.quote(symbol)}")

    @server.tool()
    async def get_tick_history(symbol: str) -> dict[str, Any]:
        """Get Deriv tick history for a symbol."""
        return await call_api(f"/api/market/history?symbol={urllib.parse.quote(symbol)}")

    @server.tool()
    async def get_account() -> dict[str, Any]:
        """Get the configured Deriv account snapshot and risk status."""
        return await call_api("/api/account")

    @server.tool()
    async def get_autobot_status() -> dict[str, Any]:
        """Get the current scheduler state and recent journal entries."""
        return await call_api("/api/autobot/status")

    @server.tool()
    async def get_journal(limit: int = 100, symbol: str = "") -> dict[str, Any]:
        """Read recent trade journal entries, optionally filtered by symbol."""
        if not 1 <= limit <= 500:
            raise ValueError("limit must be from 1 to 500")
        query = f"?limit={limit}"
        if symbol:
            query += f"&symbol={urllib.parse.quote(symbol)}"
        return await call_api(f"/api/autobot/journal{query}")

    if trading_enabled:
        @server.tool()
        async def place_option_order(
            symbol: str,
            contract_type: str,
            stake: float,
            duration: int,
            duration_unit: str,
            confirm: bool,
        ) -> dict[str, Any]:
            """Place one Deriv CALL/PUT order; requires confirm=true and Jarvis execution unlock."""
            if not confirm:
                raise ValueError("Set confirm=true only after explicitly approving this order")
            return await call_api("/api/options/order", {
                "symbol": symbol,
                "contract_type": contract_type,
                "stake": stake,
                "duration": duration,
                "duration_unit": duration_unit,
            })

        @server.tool()
        async def run_autobot_once(cycles: int = 1, confirm: bool = False) -> dict[str, Any]:
            """Run one to five live bot cycles; requires confirm=true and Jarvis execution unlock."""
            if not confirm:
                raise ValueError("Set confirm=true only after explicitly approving a live bot run")
            return await call_api("/api/autobot/run", {"cycles": cycles})

        @server.tool()
        async def start_autobot(confirm: bool = False) -> dict[str, Any]:
            """Start repeated scheduled trading using the saved profile; requires confirm=true and Jarvis execution unlock."""
            if not confirm:
                raise ValueError("Set confirm=true only after explicitly approving scheduled trading")
            return await call_api("/api/autobot/profile", {"enabled": True})

        @server.tool()
        async def stop_autobot() -> dict[str, Any]:
            """Stop scheduled trading immediately."""
            return await call_api("/api/autobot/stop", {})

        @server.tool()
        async def update_autobot_profile(
            interval_seconds: int | None = None,
            cycles: int | None = None,
            duration: int | None = None,
            duration_unit: str | None = None,
            symbols: list[str] | None = None,
        ) -> dict[str, Any]:
            """Update the saved scheduler profile without changing its enabled state."""
            payload = {
                key: value for key, value in {
                    "interval_seconds": interval_seconds,
                    "cycles": cycles,
                    "duration": duration,
                    "duration_unit": duration_unit,
                    "symbols": symbols,
                }.items() if value is not None
            }
            if not payload:
                raise ValueError("Provide at least one profile setting")
            return await call_api("/api/autobot/profile", payload)

    return server


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Jarvis remote MCP server")
    parser.add_argument("--host", default=os.environ.get("JARVIS_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("JARVIS_MCP_PORT", "8001")))
    args = parser.parse_args()
    token = os.environ.get("JARVIS_MCP_BEARER_TOKEN", "")
    if len(token) < 32:
        raise SystemExit("Set JARVIS_MCP_BEARER_TOKEN to a random secret of at least 32 characters")

    import uvicorn

    server = create_mcp_server()
    app = BearerAuthMiddleware(server.streamable_http_app(), token)
    print(f"Jarvis MCP listening on http://{args.host}:{args.port}/mcp")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())