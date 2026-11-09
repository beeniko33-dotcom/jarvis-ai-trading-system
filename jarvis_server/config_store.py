#!/usr/bin/env python3
"""Local persistence for Jarvis runtime configuration and Deriv credentials."""

from __future__ import annotations

import json
import os
from functools import wraps
from pathlib import Path
from threading import RLock
from typing import Any
from datetime import datetime, timezone


def synchronized(method: Any) -> Any:
    @wraps(method)
    def wrapper(self: ConfigStore, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class ConfigStore:
    """Stores local config data in a JSON file next to the repo root."""

    def __init__(self, path: str | None = None) -> None:
        self._lock = RLock()
        repo_root = Path(__file__).resolve().parents[1]
        self.path = Path(path) if path else repo_root / ".jarvis_config.json"
        self.default = {
            "provider": "deriv",
            "deriv_account_id": "",
            "deriv_token": "",
            "deriv_app_id": "",
            "deriv_account_type": "demo",
            "account_currency": "USD",
            "allow_live_execution": False,
            "kill_switch": True,
            "max_order_value": 5.0,
            "max_daily_loss": 10.0,
            "max_open_contracts": 2,
            "symbol_allowlist": [],
            "daily_risk": {},
            "strategy": {
                "short_window": 5,
                "long_window": 12,
                "buy_threshold": 1.02,
                "sell_threshold": 0.98,
            },
            "autobot": {
                "enabled": False,
                "interval_seconds": 30,
                "cycles": 3,
                "duration": 5,
                "duration_unit": "m",
                "symbols": [],
            },
        }

    @staticmethod
    def _normalize_allowlist(value: Any) -> list[str]:
        if isinstance(value, str):
            items = [part.strip().upper() for part in value.split(",") if part.strip()]
            return items
        if isinstance(value, list):
            return [str(item).strip().upper() for item in value if str(item).strip()]
        return []

    @synchronized
    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            self.save(self.default)
            return dict(self.default)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ValueError(f"Configuration file contains invalid JSON: {self.path}") from error
        if not isinstance(data, dict):
            raise ValueError(f"Configuration file must contain a JSON object: {self.path}")
        legacy_provider = data.get("provider") != "deriv"
        merged = dict(self.default)
        merged.update({str(key): value for key, value in (data or {}).items()})
        if legacy_provider:
            merged.update({
                "provider": "deriv",
                "deriv_account_id": "",
                "deriv_token": "",
                "deriv_app_id": "",
                "deriv_account_type": "demo",
                "allow_live_execution": False,
                "kill_switch": True,
                "symbol_allowlist": [],
                "autobot": dict(self.default["autobot"]),
            })
        for legacy_key in (
            "api_key",
            "api_secret",
            "exchange",
            "dry_run",
            "sandbox",
            "max_position_value",
            "daily_loss_limit",
        ):
            merged.pop(legacy_key, None)
        if not isinstance(merged.get("symbol_allowlist", []), list):
            merged["symbol_allowlist"] = self._normalize_allowlist(merged.get("symbol_allowlist", []))
        else:
            merged["symbol_allowlist"] = self._normalize_allowlist(merged["symbol_allowlist"])
        merged["max_order_value"] = min(5.0, max(0.01, float(merged.get("max_order_value", 5.0))))
        merged["max_daily_loss"] = min(10.0, max(0.01, float(merged.get("max_daily_loss", 10.0))))
        merged["max_open_contracts"] = min(2, max(1, int(merged.get("max_open_contracts", 2))))
        merged["deriv_account_type"] = str(merged.get("deriv_account_type") or "demo").lower()
        if legacy_provider:
            return self.save(merged)
        return merged

    @synchronized
    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        merged = dict(self.default)
        merged.update({str(key): value for key, value in (payload or {}).items()})
        if merged.get("provider") != "deriv":
            raise ValueError("Only the Deriv provider is supported by this runtime")
        for legacy_key in (
            "api_key",
            "api_secret",
            "exchange",
            "dry_run",
            "sandbox",
            "max_position_value",
            "daily_loss_limit",
        ):
            merged.pop(legacy_key, None)
        if not isinstance(merged.get("symbol_allowlist", []), list):
            merged["symbol_allowlist"] = self._normalize_allowlist(merged.get("symbol_allowlist", []))
        else:
            merged["symbol_allowlist"] = [str(item).strip().upper() for item in merged["symbol_allowlist"] if str(item).strip()]
        strategy_cfg = merged.get("strategy") or {}
        if isinstance(strategy_cfg, dict):
            merged["strategy"] = {
                "short_window": int(strategy_cfg.get("short_window", 5)),
                "long_window": int(strategy_cfg.get("long_window", 12)),
                "buy_threshold": float(strategy_cfg.get("buy_threshold", 1.02)),
                "sell_threshold": float(strategy_cfg.get("sell_threshold", 0.98)),
            }
        merged["max_order_value"] = min(5.0, max(0.01, float(merged.get("max_order_value", 5.0))))
        merged["max_daily_loss"] = min(10.0, max(0.01, float(merged.get("max_daily_loss", 10.0))))
        merged["max_open_contracts"] = min(2, max(1, int(merged.get("max_open_contracts", 2))))
        if merged.get("deriv_account_type") not in {"demo", "real"}:
            raise ValueError("deriv_account_type must be demo or real")
        autobot_cfg = merged.get("autobot") or {}
        if isinstance(autobot_cfg, dict):
            symbols = autobot_cfg.get("symbols") or []
            duration_unit = str(autobot_cfg.get("duration_unit", "m"))
            if duration_unit not in {"s", "m", "h", "d"}:
                raise ValueError("duration_unit must be one of s, m, h, d")
            merged["autobot"] = {
                "enabled": bool(autobot_cfg.get("enabled", False)),
                "interval_seconds": max(5, int(autobot_cfg.get("interval_seconds", 30))),
                "cycles": max(1, int(autobot_cfg.get("cycles", 3))),
                "duration": max(1, int(autobot_cfg.get("duration", 5))),
                "duration_unit": duration_unit,
                "symbols": self._normalize_allowlist(symbols),
            }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.path.with_name(self.path.name + ".tmp")
        temporary_path.write_text(json.dumps(merged, indent=2), encoding="utf-8")
        os.chmod(temporary_path, 0o600)
        temporary_path.replace(self.path)
        return dict(merged)

    @synchronized
    def set_deriv_config(
        self,
        *,
        account_id: str | None = None,
        token: str | None = None,
        app_id: str | None = None,
        account_type: str | None = None,
        currency: str | None = None,
        allow_live_execution: bool | None = None,
        kill_switch: bool | None = None,
        max_order_value: float | None = None,
        max_daily_loss: float | None = None,
        max_open_contracts: int | None = None,
        symbol_allowlist: list[str] | str | None = None,
    ) -> dict[str, Any]:
        settings = self.load()
        identity_changed = any(
            supplied is not None and str(supplied).strip() != str(settings.get(key) or "")
            for supplied, key in (
                (account_id, "deriv_account_id"),
                (token, "deriv_token"),
                (app_id, "deriv_app_id"),
            )
        ) or (account_type is not None and account_type != settings.get("deriv_account_type"))
        if account_id is not None:
            settings["deriv_account_id"] = account_id.strip()
        if token is not None:
            settings["deriv_token"] = token.strip()
        if app_id is not None:
            settings["deriv_app_id"] = app_id.strip()
        if account_type is not None and account_type != settings.get("deriv_account_type"):
            settings["deriv_account_type"] = account_type
        if identity_changed:
            settings["allow_live_execution"] = False
            settings["kill_switch"] = True
            settings["autobot"]["enabled"] = False
        if currency is not None:
            settings["account_currency"] = currency.strip().upper()
        if allow_live_execution is not None and not identity_changed:
            settings["allow_live_execution"] = bool(allow_live_execution)
        if kill_switch is not None and not identity_changed:
            settings["kill_switch"] = bool(kill_switch)
        if max_order_value is not None:
            settings["max_order_value"] = float(max_order_value)
        if max_daily_loss is not None:
            settings["max_daily_loss"] = float(max_daily_loss)
        if max_open_contracts is not None:
            settings["max_open_contracts"] = int(max_open_contracts)
        if symbol_allowlist is not None:
            settings["symbol_allowlist"] = self._normalize_allowlist(symbol_allowlist)
        return self.save(settings)

    @synchronized
    def daily_risk_status(self, balance: float) -> dict[str, Any]:
        settings = self.load()
        state = dict(settings.get("daily_risk") or {})
        utc_day = datetime.now(timezone.utc).date().isoformat()
        if state.get("utc_day") != utc_day:
            state = {"utc_day": utc_day, "starting_balance": float(balance), "halted": False}
        starting_balance = float(state.get("starting_balance", balance))
        loss = max(0.0, starting_balance - float(balance))
        if loss >= float(settings["max_daily_loss"]):
            state["halted"] = True
        settings["daily_risk"] = state
        self.save(settings)
        return {
            "utc_day": utc_day,
            "starting_balance": starting_balance,
            "current_balance": float(balance),
            "loss": loss,
            "limit": float(settings["max_daily_loss"]),
            "halted": bool(state.get("halted", False)),
        }

    @synchronized
    def set_strategy_profile(self, **values: Any) -> dict[str, Any]:
        settings = self.load()
        strategy_cfg = dict(settings.get("strategy") or {})
        if "short_window" in values:
            strategy_cfg["short_window"] = int(values["short_window"])
        if "long_window" in values:
            strategy_cfg["long_window"] = int(values["long_window"])
        if "buy_threshold" in values:
            strategy_cfg["buy_threshold"] = float(values["buy_threshold"])
        if "sell_threshold" in values:
            strategy_cfg["sell_threshold"] = float(values["sell_threshold"])
        settings["strategy"] = strategy_cfg
        return self.save(settings)

    @synchronized
    def set_autobot_profile(self, *, enabled: bool | None = None, interval_seconds: int | None = None, cycles: int | None = None, duration: int | None = None, duration_unit: str | None = None, symbols: list[str] | str | None = None) -> dict[str, Any]:
        settings = self.load()
        autobot_cfg = dict(settings.get("autobot") or {})
        if enabled and settings.get("kill_switch"):
            raise ValueError("Clear the kill switch in Settings before enabling the scheduler")
        if enabled and not settings.get("allow_live_execution") and settings.get("deriv_account_type") == "real":
            raise ValueError("Live execution must be explicitly enabled before starting the scheduler")
        if enabled is not None:
            autobot_cfg["enabled"] = bool(enabled)
        if interval_seconds is not None:
            autobot_cfg["interval_seconds"] = max(5, int(interval_seconds))
        if cycles is not None:
            autobot_cfg["cycles"] = max(1, int(cycles))
        if duration is not None:
            autobot_cfg["duration"] = max(1, int(duration))
        if duration_unit is not None:
            autobot_cfg["duration_unit"] = duration_unit
        if symbols is not None:
            autobot_cfg["symbols"] = self._normalize_allowlist(symbols)
        settings["autobot"] = autobot_cfg
        return self.save(settings)
