#!/usr/bin/env python3
"""Deriv Options API client using the documented OTP-authenticated WebSocket API."""

from __future__ import annotations

import json
import math
import time
from typing import Any
from urllib import error, request

import websocket


class DerivAPIError(RuntimeError):
    """An API request failed or returned an unusable response."""


class DerivConfigurationError(DerivAPIError):
    """Local Deriv credentials or settings are missing or invalid."""


class DerivClient:
    API_BASE = "https://api.derivws.com"
    PUBLIC_WS = "wss://api.derivws.com/trading/v1/options/ws/public"

    def __init__(
        self,
        settings: dict[str, Any],
        *,
        urlopen: Any = request.urlopen,
        websocket_factory: Any = websocket.create_connection,
    ) -> None:
        self.settings = settings
        self._urlopen = urlopen
        self._websocket_factory = websocket_factory
        self.account_id = str(settings.get("deriv_account_id") or "").strip()
        self.token = str(settings.get("deriv_token") or "").strip()
        self.app_id = str(settings.get("deriv_app_id") or "").strip()
        self.account_type = str(settings.get("deriv_account_type") or "demo").lower()
        if self.account_type not in {"demo", "real"}:
            raise DerivConfigurationError("deriv_account_type must be demo or real")

    def _open_public(self) -> Any:
        try:
            return self._websocket_factory(self.PUBLIC_WS, timeout=10)
        except (OSError, websocket.WebSocketException) as exc:
            raise DerivAPIError(f"Could not connect to Deriv public data: {exc}") from exc

    def _open_account(self) -> Any:
        if not self.account_id or not self.token or not self.app_id:
            raise DerivConfigurationError("Configure Deriv account ID, API token, and app ID in Settings")
        otp_url = f"{self.API_BASE}/trading/v1/options/accounts/{self.account_id}/otp"
        body = json.dumps({}).encode("utf-8")
        req = request.Request(
            otp_url,
            data=body,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Deriv-App-ID": self.app_id,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with self._urlopen(req, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (error.URLError, TimeoutError, ValueError) as exc:
            raise DerivAPIError(f"Could not authenticate with Deriv: {exc}") from exc
        url = (payload.get("data") or {}).get("url") if isinstance(payload, dict) else None
        if not isinstance(url, str) or not url.startswith("wss://"):
            raise DerivAPIError("Deriv OTP response did not contain a secure WebSocket URL")
        try:
            return self._websocket_factory(url, timeout=10)
        except (OSError, websocket.WebSocketException) as exc:
            raise DerivAPIError(f"Could not open the authenticated Deriv WebSocket: {exc}") from exc

    @staticmethod
    def _request(ws: Any, payload: dict[str, Any], request_id: int) -> dict[str, Any]:
        message = dict(payload)
        message["req_id"] = request_id
        try:
            ws.send(json.dumps(message))
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                response = json.loads(ws.recv())
                if response.get("req_id") != request_id:
                    continue
                if response.get("error"):
                    error_info = response["error"]
                    raise DerivAPIError(
                        f"Deriv rejected the request: "
                        f"{error_info.get('code', 'APIError')}: "
                        f"{error_info.get('message', 'unknown error')}"
                    )
                return response
        except (websocket.WebSocketTimeoutException, TimeoutError) as exc:
            raise DerivAPIError("Timed out waiting for a Deriv response") from exc
        except websocket.WebSocketException as exc:
            raise DerivAPIError(f"Deriv WebSocket failed: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise DerivAPIError("Deriv returned invalid JSON") from exc
        raise DerivAPIError("Timed out waiting for a matching Deriv response")

    @staticmethod
    def _close(ws: Any) -> None:
        try:
            ws.close()
        except (OSError, websocket.WebSocketException):
            pass

    def active_symbols(self) -> list[dict[str, Any]]:
        ws = self._open_public()
        try:
            response = self._request(
                ws,
                {"active_symbols": "full", "contract_type": ["CALL", "PUT"]},
                1,
            )
        finally:
            self._close(ws)
        symbols = response.get("active_symbols")
        if not isinstance(symbols, list):
            raise DerivAPIError("Deriv active-symbol response is missing its symbol list")
        supported: list[dict[str, Any]] = []
        for item in symbols:
            if not isinstance(item, dict):
                continue
            market = str(item.get("market") or "").lower()
            if market not in {"forex", "commodities", "cryptocurrency"}:
                continue
            symbol = item.get("underlying_symbol")
            if isinstance(symbol, str) and symbol:
                item["symbol"] = symbol
                item["display_name"] = item.get("underlying_symbol_name", symbol)
                supported.append(item)
        if not supported:
            raise DerivAPIError("Deriv returned no supported forex, commodity, or cryptocurrency option symbols")
        return supported

    def market_snapshot(self) -> dict[str, Any]:
        symbols = self.active_symbols()
        quote_symbols = symbols[:100]
        ws = self._open_public()
        results: dict[int, tuple[str, dict[str, Any]]] = {}
        try:
            for request_id, item in enumerate(quote_symbols, start=2):
                ws.send(json.dumps({"ticks": item["symbol"], "req_id": request_id}))
                results[request_id] = (item["symbol"], item)
            quotes: dict[str, dict[str, Any]] = {}
            pending = set(results)
            deadline = time.monotonic() + 20
            while pending and time.monotonic() < deadline:
                try:
                    response = json.loads(ws.recv())
                except websocket.WebSocketTimeoutException:
                    break
                except json.JSONDecodeError as exc:
                    raise DerivAPIError("Deriv returned invalid JSON in the market stream") from exc
                except websocket.WebSocketException as exc:
                    raise DerivAPIError(f"Deriv market stream failed: {exc}") from exc
                request_id = response.get("req_id")
                if request_id not in pending:
                    continue
                pending.remove(request_id)
                symbol, metadata = results[request_id]
                tick = response.get("tick") or {}
                if response.get("error") or not tick:
                    continue
                try:
                    price = float(tick["quote"])
                except (KeyError, TypeError, ValueError):
                    continue
                if not math.isfinite(price) or price <= 0:
                    continue
                quotes[symbol] = {
                    "symbol": symbol,
                    "display_name": metadata.get("display_name", symbol),
                    "market": metadata.get("market", ""),
                    "submarket": metadata.get("submarket", ""),
                    "price": price,
                    "epoch": tick.get("epoch"),
                    "source": "live",
                }
            markets = [{
                "symbol": item["symbol"],
                "display_name": item.get("display_name", item["symbol"]),
                "market": item.get("market", ""),
                "submarket": item.get("submarket", ""),
                "price": quotes.get(item["symbol"], {}).get("price"),
                "epoch": quotes.get(item["symbol"], {}).get("epoch"),
                "source": "live" if item["symbol"] in quotes else None,
            } for item in symbols]
            return {
                "markets": markets,
                "quoted_count": len(quotes),
                "unquoted_symbols": sorted(item["symbol"] for item in symbols if item["symbol"] not in quotes),
            }
        except websocket.WebSocketException as exc:
            raise DerivAPIError(f"Deriv market stream failed: {exc}") from exc
        finally:
            self._close(ws)

    def fetch_tick(self, symbol: str) -> dict[str, Any]:
        ws = self._open_public()
        try:
            response = self._request(ws, {"ticks": symbol}, 1)
        finally:
            self._close(ws)
        tick = response.get("tick") or {}
        try:
            price = float(tick["quote"])
        except (KeyError, TypeError, ValueError) as exc:
            raise DerivAPIError(f"Deriv returned no valid quote for {symbol}") from exc
        if not math.isfinite(price) or price <= 0:
            raise DerivAPIError(f"Deriv returned an invalid quote for {symbol}")
        return {"symbol": symbol, "price": price, "epoch": tick.get("epoch"), "source": "live"}

    def tick_history(self, symbol: str, count: int = 100) -> list[float]:
        ws = self._open_public()
        try:
            response = self._request(
                ws,
                {
                    "ticks_history": symbol,
                    "end": "latest",
                    "count": min(1000, max(2, int(count))),
                    "style": "ticks",
                },
                1,
            )
        finally:
            self._close(ws)
        prices = (response.get("history") or {}).get("prices")
        if not isinstance(prices, list):
            raise DerivAPIError("Deriv tick-history response is missing prices")
        normalized = [float(price) for price in prices]
        if any(not math.isfinite(price) or price <= 0 for price in normalized):
            raise DerivAPIError("Deriv returned invalid tick history")
        return normalized

    def account_snapshot(self) -> dict[str, Any]:
        ws = self._open_account()
        try:
            balance_response = self._request(ws, {"balance": 1}, 1)
            portfolio_response = self._request(ws, {"portfolio": 1}, 2)
        finally:
            self._close(ws)
        balance = balance_response.get("balance")
        portfolio = portfolio_response.get("portfolio")
        if not isinstance(balance, dict) or "balance" not in balance or not balance.get("currency"):
            raise DerivAPIError("Deriv balance response is missing account balance or currency")
        positions = portfolio.get("contracts") if isinstance(portfolio, dict) else None
        if not isinstance(positions, list):
            raise DerivAPIError("Deriv portfolio response is missing open contracts")
        try:
            amount = float(balance["balance"])
        except (TypeError, ValueError) as exc:
            raise DerivAPIError("Deriv returned an invalid account balance") from exc
        if not math.isfinite(amount) or amount < 0:
            raise DerivAPIError("Deriv returned an invalid account balance")
        return {
            "balance": amount,
            "currency": str(balance.get("currency") or ""),
            "login_id": str(balance.get("loginid") or ""),
            "positions": positions,
        }

    def buy_option(
        self,
        *,
        symbol: str,
        contract_type: str,
        stake: float,
        currency: str,
        duration: int,
        duration_unit: str,
    ) -> dict[str, Any]:
        normalized_type = contract_type.upper()
        if normalized_type not in {"CALL", "PUT"}:
            raise ValueError("contract_type must be CALL or PUT")
        if not math.isfinite(stake) or stake <= 0:
            raise ValueError("stake must be a positive finite number")
        if stake > float(self.settings.get("max_order_value", 5.0)):
            raise ValueError("Stake exceeds the configured per-contract limit")
        if duration < 1 or duration_unit not in {"s", "m", "h", "d"}:
            raise ValueError("Invalid option duration")
        ws = self._open_account()
        try:
            contracts_response = self._request(ws, {"contracts_for": symbol}, 1)
            available = (contracts_response.get("contracts_for") or {}).get("available")
            if isinstance(available, list) and not any(
                item.get("contract_type") == normalized_type
                for item in available
                if isinstance(item, dict)
            ):
                raise DerivAPIError(f"{normalized_type} options are not available for {symbol}")
            proposal_response = self._request(
                ws,
                {
                    "proposal": 1,
                    "amount": stake,
                    "basis": "stake",
                    "contract_type": normalized_type,
                    "currency": currency,
                    "underlying_symbol": symbol,
                    "duration": duration,
                    "duration_unit": duration_unit,
                },
                2,
            )
            proposal = proposal_response.get("proposal") or {}
            proposal_id = proposal.get("id")
            ask_price = float(proposal.get("ask_price", 0))
            if not proposal_id or not math.isfinite(ask_price) or ask_price <= 0:
                raise DerivAPIError("Deriv returned an invalid option proposal")
            if ask_price > float(self.settings.get("max_order_value", 5.0)):
                raise DerivAPIError("Deriv proposal exceeds the configured stake limit")
            buy_response = self._request(
                ws,
                {"buy": str(proposal_id), "price": ask_price},
                3,
            )
        finally:
            self._close(ws)
        contract = buy_response.get("buy") or {}
        contract_id = contract.get("contract_id")
        if contract_id is None:
            raise DerivAPIError("Deriv buy response did not contain a contract ID")
        return {
            "contract_id": contract_id,
            "symbol": symbol,
            "contract_type": normalized_type,
            "stake": ask_price,
            "currency": currency,
            "buy_price": contract.get("buy_price", ask_price),
            "payout": contract.get("payout"),
            "status": contract.get("contract_status", "open"),
            "source": self.account_type,
        }
