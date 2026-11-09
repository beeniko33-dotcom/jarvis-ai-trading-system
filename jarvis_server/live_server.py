#!/usr/bin/env python3
"""HTTP API and scheduler for the Deriv live-data trading terminal."""

from __future__ import annotations

import argparse
import json
import math
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

from backtester import BacktestEngine
from config_store import ConfigStore
from deriv_client import DerivAPIError, DerivClient, DerivConfigurationError
from journal_store import JournalStore
from optimizer import StrategyOptimizer

ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = ROOT / "frontend"
MAX_ORDER_STAKE = 5.0
MAX_DAILY_LOSS = 10.0
MAX_OPEN_CONTRACTS = 2


class TradingBlocked(DerivAPIError):
    """A local execution-policy check prevented a trading action."""


def _client(settings: dict[str, Any]) -> DerivClient:
    return DerivClient(settings=settings)


def _execution_gate(settings: dict[str, Any]) -> None:
    if settings.get("kill_switch", True):
        raise TradingBlocked("Trading is blocked by the kill switch")
    if not settings.get("allow_live_execution", False):
        raise TradingBlocked("Trading is disabled; explicitly enable it in Settings")
    if not settings.get("deriv_account_id") or not settings.get("deriv_token") or not settings.get("deriv_app_id"):
        raise TradingBlocked("Configure your Deriv account ID, API token, and app ID in Settings")
    if float(settings.get("max_order_value", MAX_ORDER_STAKE)) > MAX_ORDER_STAKE:
        raise TradingBlocked("Per-contract stake limit exceeds the $5 hard cap")


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _strategy_signal(prices: list[float], short_window: int, long_window: int, buy_threshold: float, sell_threshold: float) -> str:
    if len(prices) < long_window:
        return "hold"
    short_average = sum(prices[-short_window:]) / short_window
    long_average = sum(prices[-long_window:]) / long_window
    if long_average <= 0:
        return "hold"
    ratio = short_average / long_average
    if ratio > buy_threshold:
        return "CALL"
    if ratio < sell_threshold:
        return "PUT"
    return "HOLD"


def _run_live_cycle(config: ConfigStore, journal: JournalStore) -> dict[str, Any]:
    settings = config.load()
    _execution_gate(settings)
    client = _client(settings)
    account = client.account_snapshot()
    currency = str(settings.get("account_currency") or "USD").upper()
    if account["currency"].upper() != currency:
        raise TradingBlocked(
            f"Configured currency {currency} does not match Deriv account currency {account['currency']}"
        )
    risk = config.daily_risk_status(float(account["balance"]))
    if risk["halted"]:
        raise TradingBlocked(
            f"Daily loss stop reached: {risk['loss']:.2f} {currency} of {risk['limit']:.2f}"
        )

    profile = settings.get("autobot") or {}
    symbols = profile.get("symbols") or settings.get("symbol_allowlist") or []
    if not symbols:
        symbols = [item["symbol"] for item in client.active_symbols()]
    max_open = min(MAX_OPEN_CONTRACTS, int(settings.get("max_open_contracts", MAX_OPEN_CONTRACTS)))
    open_count = len(account["positions"])
    results: list[dict[str, Any]] = []
    strategy = settings.get("strategy") or {}
    short_window = max(2, int(strategy.get("short_window", 5)))
    long_window = max(short_window + 1, int(strategy.get("long_window", 12)))
    buy_threshold = float(strategy.get("buy_threshold", 1.02))
    sell_threshold = float(strategy.get("sell_threshold", 0.98))
    duration = int(profile.get("duration", 5))
    duration_unit = str(profile.get("duration_unit", "m"))
    stake = min(MAX_ORDER_STAKE, float(settings.get("max_order_value", MAX_ORDER_STAKE)))

    for symbol in symbols:
        if open_count >= max_open:
            break
        prices = client.tick_history(str(symbol), count=max(100, long_window))
        signal = _strategy_signal(prices, short_window, long_window, buy_threshold, sell_threshold)
        entry: dict[str, Any] = {
            "timestamp": _utc_timestamp(),
            "symbol": str(symbol).upper(),
            "signal": signal,
            "source": "deriv",
            "reason": "Moving-average momentum signal",
        }
        if signal in {"CALL", "PUT"}:
            current_balance = float(account["balance"]) - sum(
                float(result.get("stake", 0))
                for result in results
                if result.get("status") == "open"
            )
            risk = config.daily_risk_status(current_balance)
            if risk["halted"]:
                entry["trade"] = {"status": "blocked", "reason": "daily_loss_limit"}
                journal.append(entry)
                results.append(entry)
                break
            order_stake = min(stake, risk["limit"] - risk["loss"])
            if order_stake <= 0:
                entry["trade"] = {"status": "blocked", "reason": "daily_loss_budget"}
                journal.append(entry)
                results.append(entry)
                break
            contract = client.buy_option(
                symbol=str(symbol),
                contract_type=signal,
                stake=order_stake,
                currency=currency,
                duration=duration,
                duration_unit=duration_unit,
            )
            entry["trade"] = contract
            entry["stake"] = float(contract["stake"])
            open_count += 1
            risk = config.daily_risk_status(current_balance - float(contract["stake"]))
        else:
            entry["trade"] = {"status": "no_trade"}
        journal.append(entry)
        results.append(entry)

    return {
        "status": "completed",
        "account_type": settings.get("deriv_account_type", "demo"),
        "signals_processed": len(results),
        "opened_contracts": sum(1 for item in results if item.get("trade", {}).get("contract_id") is not None),
        "daily_risk": risk,
        "results": results,
    }


class AutobotScheduler:
    def __init__(
        self,
        config: ConfigStore,
        journal: JournalStore,
        execution_lock: Any | None = None,
    ) -> None:
        self.config = config
        self.journal = journal
        self.execution_lock = execution_lock or threading.Lock()
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.last_execution: dict[str, Any] | None = None
        self.last_error = ""

    def profile(self) -> dict[str, Any]:
        return self.config.load().get("autobot", {})

    def status(self) -> dict[str, Any]:
        profile = self.profile()
        return {
            "profile": profile,
            "enabled": bool(profile.get("enabled")),
            "last_execution": self.last_execution,
            "last_error": self.last_error,
            "journal": self.journal.entries()[-20:],
        }

    def apply_profile(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("enabled"):
            _execution_gate(self.config.load())
            selected_symbols = payload.get("symbols", self.profile().get("symbols", []))
            if not selected_symbols:
                raise TradingBlocked("Select at least one supported symbol before starting automation")
        saved = self.config.set_autobot_profile(
            enabled=payload.get("enabled"),
            interval_seconds=payload.get("interval_seconds"),
            cycles=payload.get("cycles"),
            duration=payload.get("duration"),
            duration_unit=payload.get("duration_unit"),
            symbols=payload.get("symbols"),
        ).get("autobot", {})
        self._refresh_loop()
        return saved

    def start(self) -> dict[str, Any]:
        profile = dict(self.profile())
        profile["enabled"] = True
        self._execution_gate_for_start()
        return self.apply_profile(profile)

    def _execution_gate_for_start(self) -> None:
        _execution_gate(self.config.load())
        settings = self.config.load()
        if not (self.profile().get("symbols") or settings.get("symbol_allowlist")):
            raise TradingBlocked("Select at least one supported symbol before starting automation")

    def stop(self) -> dict[str, Any]:
        profile = dict(self.profile())
        profile["enabled"] = False
        return self.apply_profile(profile)

    def stop_for_restart(self) -> None:
        self.stop_event.set()
        settings = self.config.load()
        if settings.get("autobot", {}).get("enabled"):
            self.config.set_autobot_profile(enabled=False)

    def run_once(self, cycles: int = 1) -> dict[str, Any]:
        self._execution_gate_for_start()
        outputs = []
        with self.execution_lock:
            for _ in range(cycles):
                outputs.append(_run_live_cycle(self.config, self.journal))
                if outputs[-1].get("daily_risk", {}).get("halted"):
                    break
        self.last_execution = {
            "timestamp": _utc_timestamp(),
            "runs": outputs,
            "account_type": self.config.load().get("deriv_account_type", "demo"),
        }
        self.last_error = ""
        return {"status": "completed", "runs": outputs, "last_execution": self.last_execution}

    def _refresh_loop(self) -> None:
        with self.lock:
            if self.profile().get("enabled"):
                self.stop_event.clear()
                if self.thread is None or not self.thread.is_alive():
                    self.thread = threading.Thread(target=self._loop, daemon=True)
                    self.thread.start()
            else:
                self.stop_event.set()

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            profile = self.profile()
            if not profile.get("enabled"):
                return
            try:
                result = self.run_once(max(1, min(5, int(profile.get("cycles", 1)))))
                self.last_execution = result["last_execution"]
            except (DerivAPIError, ValueError, OSError) as exc:
                self.last_error = str(exc)
                self.config.set_autobot_profile(enabled=False)
                return
            self.stop_event.wait(max(15, int(profile.get("interval_seconds", 60))))


class JarvisHTTPServer(ThreadingHTTPServer):
    config: ConfigStore
    journal: JournalStore
    execution_lock: threading.Lock
    autobot: AutobotScheduler


class JarvisHandler(BaseHTTPRequestHandler):
    server_version = "JarvisDeriv/2.0"
    max_request_bytes = 1_048_576

    @property
    def app_server(self) -> JarvisHTTPServer:
        return cast(JarvisHTTPServer, self.server)

    def do_GET(self) -> None:  # noqa: N802
        route = urlsplit(self.path).path
        if route in {"/", "/index.html"}:
            self._serve_file(FRONTEND_DIR / "terminal.html")
            return
        if route.startswith("/api/"):
            self._handle_get(route)
            return
        candidate = (FRONTEND_DIR / route.lstrip("/")).resolve()
        if candidate.is_file() and FRONTEND_DIR in candidate.parents:
            self._serve_file(candidate)
            return
        self.send_error(404, "Not found")

    def do_POST(self) -> None:  # noqa: N802
        route = urlsplit(self.path).path
        if route.startswith("/api/"):
            self._handle_post(route)
            return
        self.send_error(405, "Unsupported method")

    def _read_json(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > self.max_request_bytes:
                raise ValueError("request body size is invalid")
            payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._json({"ok": False, "error": f"Invalid JSON request: {exc}"}, 400)
            return None
        if not isinstance(payload, dict):
            self._json({"ok": False, "error": "Request body must be a JSON object"}, 400)
            return None
        return payload

    def _json(self, payload: dict[str, Any], status: int = 200) -> None:
        content = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _serve_file(self, path: Path) -> None:
        try:
            content = path.read_bytes()
        except OSError as exc:
            self.send_error(404, f"UI file unavailable: {exc}")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _public_config(self) -> dict[str, Any]:
        settings = self.app_server.config.load()
        return {
            "provider": "deriv",
            "account_id": settings.get("deriv_account_id", ""),
            "app_id": settings.get("deriv_app_id", ""),
            "account_type": settings.get("deriv_account_type", "demo"),
            "currency": settings.get("account_currency", "USD"),
            "token_configured": bool(settings.get("deriv_token")),
            "allow_live_execution": bool(settings.get("allow_live_execution")),
            "kill_switch": bool(settings.get("kill_switch")),
            "max_order_stake": settings.get("max_order_value", 5.0),
            "max_daily_loss": settings.get("max_daily_loss", 10.0),
            "max_open_contracts": settings.get("max_open_contracts", 2),
            "symbols": settings.get("symbol_allowlist", []),
        }

    def _handle_get(self, route: str) -> None:
        settings = self.app_server.config.load()
        try:
            if route == "/api/health":
                payload = {"status": "ok", "service": "jarvis-deriv-terminal", "provider": "deriv"}
            elif route == "/api/config":
                payload = self._public_config()
            elif route == "/api/market":
                payload = _client(settings).market_snapshot()
            elif route == "/api/market/history":
                query = parse_qs(urlsplit(self.path).query)
                symbol = query.get("symbol", [""])[0]
                if not symbol:
                    self._json({"ok": False, "error": "symbol is required"}, 400)
                    return
                payload = {"symbol": symbol, "prices": _client(settings).tick_history(symbol)}
            elif route == "/api/market/quote":
                query = parse_qs(urlsplit(self.path).query)
                symbol = query.get("symbol", [""])[0]
                if not symbol:
                    self._json({"ok": False, "error": "symbol is required"}, 400)
                    return
                payload = _client(settings).fetch_tick(symbol)
            elif route == "/api/account":
                account = _client(settings).account_snapshot()
                risk = self.app_server.config.daily_risk_status(float(account["balance"]))
                payload = {
                    "account": account,
                    "daily_risk": risk,
                    "execution": {
                        "enabled": bool(settings.get("allow_live_execution")) and not bool(settings.get("kill_switch")),
                        "kill_switch": bool(settings.get("kill_switch")),
                        "account_type": settings.get("deriv_account_type", "demo"),
                    },
                }
            elif route == "/api/autobot/profile":
                payload = {"autobot": self.app_server.autobot.profile()}
            elif route == "/api/autobot/status":
                payload = self.app_server.autobot.status()
            elif route == "/api/autobot/journal":
                query = parse_qs(urlsplit(self.path).query)
                limit = min(5000, max(1, int(query.get("limit", ["100"])[0])))
                entries = self.app_server.journal.entries()
                symbol = query.get("symbol", [""])[0].upper()
                signal = query.get("signal", ["all"])[0].upper()
                if symbol:
                    entries = [item for item in entries if str(item.get("symbol", "")).upper() == symbol]
                if signal != "ALL":
                    entries = [item for item in entries if str(item.get("signal", "")).upper() == signal]
                payload = {"journal": entries[-limit:], "total": len(entries)}
            elif route == "/api/backtest":
                payload = {"message": "POST to run isolated offline research backtesting"}
            elif route == "/api/optimize":
                payload = {"message": "POST to run isolated offline research optimization"}
            elif route in {"/api/trade", "/api/bot/run", "/api/strategy/run", "/api/exchange/order"}:
                self._json({"ok": False, "error": "Simulation order endpoints have been removed"}, 410)
                return
            else:
                self._json({"ok": False, "error": "Unknown API route"}, 404)
                return
            self._json(payload)
        except (DerivAPIError, ValueError, OSError) as exc:
            status = (
                403 if isinstance(exc, TradingBlocked)
                else 400 if isinstance(exc, DerivConfigurationError)
                else 502 if isinstance(exc, DerivAPIError)
                else 400
            )
            self._json({"ok": False, "error": str(exc)}, status)

    def _handle_post(self, route: str) -> None:
        data = self._read_json()
        if data is None:
            return
        try:
            if route == "/api/exchange/order":
                self._json({"ok": False, "error": "The legacy exchange order endpoint has been removed"}, 410)
                return
            if route == "/api/config":
                self._save_config(data)
                return
            if route == "/api/autobot/profile":
                self._save_profile(data)
                return
            if route == "/api/options/order":
                with self.app_server.execution_lock:
                    contract = self._submit_manual_option(data)
                if contract is not None:
                    self._json({"ok": True, "contract": contract})
                return
            if route == "/api/autobot/run":
                cycles = data.get("cycles", 1)
                if isinstance(cycles, bool) or not isinstance(cycles, int) or not 1 <= cycles <= 5:
                    self._json({"ok": False, "error": "cycles must be an integer from 1 to 5"}, 400)
                    return
                result = self.app_server.autobot.run_once(cycles=cycles)
                self._json({"ok": True, "autobot": result})
                return
            if route == "/api/autobot/stop":
                self._json({"ok": True, "autobot": self.app_server.autobot.stop()})
                return
            if route == "/api/backtest":
                cycles = data.get("cycles", 60)
                if isinstance(cycles, bool) or not isinstance(cycles, int) or not 1 <= cycles <= 5000:
                    self._json({"ok": False, "error": "cycles must be an integer from 1 to 5000"}, 400)
                    return
                cash = float(data.get("cash", 100000))
                symbols = data.get("symbols", ["BTCUSD", "ETHUSD"])
                self._json({"ok": True, "backtest": BacktestEngine(cash, symbols).run(cycles=cycles)})
                return
            if route == "/api/optimize":
                cycles = data.get("cycles", 60)
                if isinstance(cycles, bool) or not isinstance(cycles, int) or not 1 <= cycles <= 5000:
                    self._json({"ok": False, "error": "cycles must be an integer from 1 to 5000"}, 400)
                    return
                symbol = str(data.get("symbol", "BTCUSD"))
                self._json({"ok": True, "optimization": StrategyOptimizer().optimize(cycles, symbol)})
                return
            if route in {"/api/trade", "/api/bot/run", "/api/strategy/run"}:
                self._json({"ok": False, "error": "Simulation order endpoints have been removed"}, 410)
                return
            self._json({"ok": False, "error": "Unknown API route"}, 404)
        except (DerivAPIError, ValueError, OSError, TypeError) as exc:
            status = 403 if isinstance(exc, TradingBlocked) else 502 if isinstance(exc, DerivAPIError) else 400
            self._json({"ok": False, "error": str(exc)}, status)

    def _submit_manual_option(self, data: dict[str, Any]) -> dict[str, Any] | None:
        settings = self.app_server.config.load()
        _execution_gate(settings)
        symbol = data.get("symbol")
        contract_type = data.get("contract_type")
        if not isinstance(symbol, str) or not symbol.strip():
            self._json({"ok": False, "error": "symbol must be a non-empty string"}, 400)
            return None
        if contract_type not in {"CALL", "PUT"}:
            self._json({"ok": False, "error": "contract_type must be CALL or PUT"}, 400)
            return None
        raw_stake = data.get("stake")
        raw_duration = data.get("duration")
        if (
            isinstance(raw_stake, bool)
            or not isinstance(raw_stake, (int, float))
            or isinstance(raw_duration, bool)
            or not isinstance(raw_duration, int)
        ):
            self._json({"ok": False, "error": "stake must be numeric and duration must be an integer"}, 400)
            return
        stake = float(raw_stake)
        duration = raw_duration
        if not math.isfinite(stake) or stake <= 0 or stake > min(
            MAX_ORDER_STAKE, float(settings.get("max_order_value", MAX_ORDER_STAKE))
        ):
            self._json({"ok": False, "error": "stake must be greater than 0 and no more than the $5 cap"}, 400)
            return None
        if duration < 1 or duration > 1000:
            self._json({"ok": False, "error": "duration must be from 1 to 1000"}, 400)
            return None
        allowed = settings.get("symbol_allowlist") or []
        if symbol.upper() not in allowed:
            self._json({"ok": False, "error": "Save this market in the allowed-symbol list before trading"}, 403)
            return None
        duration_unit = data.get("duration_unit", "m")
        if duration_unit not in {"s", "m", "h", "d"}:
            self._json({"ok": False, "error": "duration_unit must be s, m, h, or d"}, 400)
            return None
        client = _client(settings)
        account = client.account_snapshot()
        currency = str(settings.get("account_currency") or "USD").upper()
        if str(account["currency"]).upper() != currency:
            self._json({"ok": False, "error": "Configured currency does not match Deriv account currency"}, 409)
            return None
        risk = self.app_server.config.daily_risk_status(float(account["balance"]))
        if risk["halted"]:
            self._json({"ok": False, "error": "Daily loss stop has been reached"}, 409)
            return None
        if risk["loss"] + stake > risk["limit"]:
            self._json({"ok": False, "error": "Stake would exceed the remaining daily loss budget"}, 409)
            return None
        if len(account["positions"]) >= min(
            MAX_OPEN_CONTRACTS, int(settings.get("max_open_contracts", MAX_OPEN_CONTRACTS))
        ):
            self._json({"ok": False, "error": "Maximum open-contract limit has been reached"}, 409)
            return None
        contract = client.buy_option(
            symbol=symbol,
            contract_type=contract_type,
            stake=stake,
            currency=currency,
            duration=duration,
            duration_unit=duration_unit,
        )
        self.app_server.journal.append({
            "timestamp": _utc_timestamp(),
            "symbol": symbol.upper(),
            "signal": contract_type,
            "stake": contract["stake"],
            "currency": currency,
            "trade": contract,
            "source": "manual",
        })
        return contract

    def _save_config(self, data: dict[str, Any]) -> None:
        string_fields = ("account_id", "token", "app_id", "account_type", "currency")
        if any(field in data and not isinstance(data[field], str) for field in string_fields):
            self._json({"ok": False, "error": "account credentials and account settings must be strings"}, 400)
            return
        for field in ("allow_live_execution", "kill_switch"):
            if field in data and not isinstance(data[field], bool):
                self._json({"ok": False, "error": f"{field} must be a boolean"}, 400)
                return
        raw_stake = data.get("max_order_stake", MAX_ORDER_STAKE)
        raw_daily_loss = data.get("max_daily_loss", MAX_DAILY_LOSS)
        raw_max_open = data.get("max_open_contracts", MAX_OPEN_CONTRACTS)
        if (
            isinstance(raw_stake, bool)
            or not isinstance(raw_stake, (int, float))
            or isinstance(raw_daily_loss, bool)
            or not isinstance(raw_daily_loss, (int, float))
            or isinstance(raw_max_open, bool)
            or not isinstance(raw_max_open, int)
        ):
            self._json({"ok": False, "error": "Risk limits must be numeric and max_open_contracts must be an integer"}, 400)
            return
        max_stake = float(raw_stake)
        daily_loss = float(raw_daily_loss)
        max_open = raw_max_open
        if not all(math.isfinite(value) for value in (max_stake, daily_loss)):
            self._json({"ok": False, "error": "Risk limits must be finite"}, 400)
            return
        if max_stake <= 0 or max_stake > MAX_ORDER_STAKE:
            self._json({"ok": False, "error": "Per-contract stake must be between 0 and $5"}, 400)
            return
        if daily_loss <= 0 or daily_loss > MAX_DAILY_LOSS:
            self._json({"ok": False, "error": "Daily loss stop must be between 0 and $10"}, 400)
            return
        if not 1 <= max_open <= MAX_OPEN_CONTRACTS:
            self._json({"ok": False, "error": "At most 2 open contracts are allowed"}, 400)
            return
        symbols = data.get("symbols")
        if symbols is not None and (
            not isinstance(symbols, list) or any(not isinstance(symbol, str) for symbol in symbols)
        ):
            self._json({"ok": False, "error": "symbols must be an array of strings"}, 400)
            return
        account_type = data.get("account_type")
        if account_type is not None and account_type not in {"demo", "real"}:
            self._json({"ok": False, "error": "account_type must be demo or real"}, 400)
            return
        try:
            self.app_server.config.set_deriv_config(
                account_id=data.get("account_id"),
                token=data.get("token"),
                app_id=data.get("app_id"),
                account_type=account_type,
                currency=data.get("currency"),
                allow_live_execution=data.get("allow_live_execution"),
                kill_switch=data.get("kill_switch"),
                max_order_value=max_stake,
                max_daily_loss=daily_loss,
                max_open_contracts=max_open,
                symbol_allowlist=symbols,
            )
        except ValueError as exc:
            self._json({"ok": False, "error": str(exc)}, 400)
            return
        self._json({"ok": True, "config": self._public_config()})

    def _save_profile(self, data: dict[str, Any]) -> None:
        if "enabled" in data and not isinstance(data["enabled"], bool):
            self._json({"ok": False, "error": "enabled must be a boolean"}, 400)
            return
        for field, minimum, maximum in (
            ("interval_seconds", 15, 86400),
            ("cycles", 1, 5),
            ("duration", 1, 1000),
        ):
            if field in data and (
                isinstance(data[field], bool)
                or not isinstance(data[field], int)
                or not minimum <= data[field] <= maximum
            ):
                self._json({"ok": False, "error": f"{field} must be an integer from {minimum} to {maximum}"}, 400)
                return
        if "duration_unit" in data and data["duration_unit"] not in {"s", "m", "h", "d"}:
            self._json({"ok": False, "error": "duration_unit must be s, m, h, or d"}, 400)
            return
        if "symbols" in data and (
            not isinstance(data["symbols"], list)
            or any(not isinstance(symbol, str) for symbol in data["symbols"])
        ):
            self._json({"ok": False, "error": "symbols must be an array of strings"}, 400)
            return
        result = self.app_server.autobot.apply_profile(data)
        self._json({"ok": True, "autobot": result})

    def log_message(self, format: str, *args: Any) -> None:
        _ = format, args
        return


def run_server(host: str = "127.0.0.1", port: int = 8000) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError(
            "Live-trading server is restricted to loopback; deploy behind authenticated HTTPS before remote access"
        )
    server = JarvisHTTPServer((host, port), JarvisHandler)
    server.config = ConfigStore()
    server.journal = JournalStore()
    server.execution_lock = threading.Lock()
    server.autobot = AutobotScheduler(server.config, server.journal, server.execution_lock)
    server.autobot.stop_for_restart()
    print(f"Jarvis Deriv terminal listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down Jarvis server")
    finally:
        server.autobot.stop_for_restart()
        server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Jarvis Deriv trading terminal")
    parser.add_argument("--host", default="127.0.0.1", help="Loopback interface only")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind")
    args = parser.parse_args()
    run_server(host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
