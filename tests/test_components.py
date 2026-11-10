from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Callable
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

SERVER_DIR = Path(__file__).resolve().parents[1] / "jarvis_server"
sys.path.insert(0, str(SERVER_DIR))

from config_store import ConfigStore
from deriv_client import DerivClient
from journal_store import JournalStore
from live_server import AutobotScheduler, JarvisHandler, JarvisHTTPServer
import live_server
from optimizer import StrategyOptimizer


class FakeWebSocket:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.closed = False

    def send(self, raw: str) -> None:
        request = json.loads(raw)
        self.messages.append(request)
        response: dict = {"req_id": request["req_id"]}
        if "active_symbols" in request:
            response["active_symbols"] = [
                {"underlying_symbol": "frxEURUSD", "underlying_symbol_name": "EUR/USD", "market": "forex", "submarket": "major_pairs"},
                {"underlying_symbol": "cryBTCUSD", "underlying_symbol_name": "Bitcoin/USD", "market": "cryptocurrency", "submarket": "major"},
                {"underlying_symbol": "R_50", "underlying_symbol_name": "Volatility 50", "market": "synthetic_index", "submarket": "random_index"},
            ]
        elif "ticks" in request:
            response["tick"] = {"quote": "1.0842", "epoch": 1700000000}
        elif "ticks_history" in request:
            response["history"] = {"prices": [str(1.0 + index / 1000) for index in range(30)]}
        elif "balance" in request:
            response["balance"] = {"balance": 100.0, "currency": "USD", "loginid": "VRTC123"}
        elif "portfolio" in request:
            response["portfolio"] = {"contracts": []}
        elif "contracts_for" in request:
            response["contracts_for"] = {"available": [{"contract_type": "CALL"}, {"contract_type": "PUT"}]}
        elif "proposal" in request:
            response["proposal"] = {"id": "proposal-1", "ask_price": request["amount"]}
        elif "buy" in request:
            response["buy"] = {
                "contract_id": 456,
                "buy_price": request["price"],
                "payout": request["price"] * 1.8,
                "contract_status": "open",
            }
        self.responses = getattr(self, "responses", [])
        self.responses.append(response)

    def recv(self) -> str:
        return json.dumps(self.responses.pop(0))

    def close(self) -> None:
        self.closed = True


def make_websocket_factory(*sockets: FakeWebSocket) -> Callable[..., FakeWebSocket]:
    available = list(sockets)

    def create(url: str, *, timeout: float) -> FakeWebSocket:
        if not url.startswith("wss://") or timeout <= 0:
            raise AssertionError("Expected a secure WebSocket URL and positive timeout")
        return available.pop(0)

    return create


class ConfigTests(unittest.TestCase):
    def test_legacy_exchange_credentials_are_cleared_on_migration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({
                "exchange": "gateio",
                "api_key": "old-key",
                "api_secret": "old-secret",
                "allow_live_execution": True,
                "autobot": {"enabled": True},
            }), encoding="utf-8")
            settings = ConfigStore(str(path)).load()
            stored = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(settings["provider"], "deriv")
        self.assertEqual(settings["deriv_account_type"], "demo")
        self.assertFalse(settings["allow_live_execution"])
        self.assertTrue(settings["kill_switch"])
        self.assertFalse(settings["autobot"]["enabled"])
        self.assertNotIn("api_key", stored)
        self.assertNotIn("api_secret", stored)

    def test_identity_change_locks_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(str(Path(directory) / "config.json"))
            store.set_deriv_config(account_id="demo-1", token="first", app_id="10")
            settings = store.set_deriv_config(
                account_id="demo-1",
                token="second",
                app_id="10",
                allow_live_execution=True,
                kill_switch=False,
            )

        self.assertFalse(settings["allow_live_execution"])
        self.assertTrue(settings["kill_switch"])
        self.assertFalse(settings["autobot"]["enabled"])

    def test_risk_caps_cannot_be_raised_above_hard_limits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(str(Path(directory) / "config.json"))
            settings = store.set_deriv_config(max_order_value=100, max_daily_loss=100, max_open_contracts=50)

        self.assertEqual(settings["max_order_value"], 5)
        self.assertEqual(settings["max_daily_loss"], 10)
        self.assertEqual(settings["max_open_contracts"], 2)

    def test_daily_loss_stop_persists_until_utc_day_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(str(Path(directory) / "config.json"))
            first = store.daily_risk_status(100)
            stopped = store.daily_risk_status(90)
            still_stopped = store.daily_risk_status(101)

        self.assertFalse(first["halted"])
        self.assertTrue(stopped["halted"])
        self.assertTrue(still_stopped["halted"])


class JournalTests(unittest.TestCase):
    def test_legacy_simulation_entries_are_removed_and_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.json"
            path.write_text(json.dumps([
                {"symbol": "OLD", "dry_run": True},
                {"symbol": "PAPER", "source": "paper"},
                {"symbol": "frxEURUSD", "source": "deriv", "dry_run": False},
            ]), encoding="utf-8")

            journal = JournalStore(str(path))

            self.assertEqual(
                journal.entries(),
                [{"symbol": "frxEURUSD", "source": "deriv", "dry_run": False}],
            )
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), journal.entries())


class DeploymentTests(unittest.TestCase):
    def test_data_directory_env_keeps_config_and_journal_on_persistent_volume(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"JARVIS_DATA_DIR": directory}
        ):
            config = ConfigStore()
            journal = JournalStore()
            config.load()
            journal.append({"source": "deriv", "symbol": "frxEURUSD"})

            self.assertEqual(config.path.parent, Path(directory))
            self.assertEqual(journal.path.parent, Path(directory))
            self.assertTrue(config.path.exists())
            self.assertTrue(journal.path.exists())

    def test_remote_bind_requires_credentials_and_https_proxy(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "app credentials"):
                live_server.run_server(host="0.0.0.0", port=0)

        with patch.dict(
            os.environ,
            {
                "JARVIS_ACCESS_USERNAME": "phone-user",
                "JARVIS_ACCESS_PASSWORD": "long-test-password-123",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "trusted HTTPS reverse proxy"):
                live_server.run_server(host="0.0.0.0", port=0)


class DerivClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = {
            "deriv_account_id": "demo-1",
            "deriv_token": "local-test-token",
            "deriv_app_id": "1234",
            "deriv_account_type": "demo",
            "max_order_value": 5,
        }

    def test_active_symbols_excludes_unrequested_synthetic_markets(self) -> None:
        ws = FakeWebSocket()
        client = DerivClient(self.settings, websocket_factory=make_websocket_factory(ws))

        symbols = client.active_symbols()

        self.assertEqual([item["symbol"] for item in symbols], ["frxEURUSD", "cryBTCUSD"])

    def test_quote_history_and_account_use_provider_data(self) -> None:
        public = FakeWebSocket()
        account = FakeWebSocket()
        otp_response = MagicMock()
        otp_response.__enter__.return_value.read.return_value = b'{"data":{"url":"wss://authorized.example"}}'
        urlopen_mock = MagicMock(return_value=otp_response)
        sockets = [public, public, account]
        client = DerivClient(
            self.settings,
            urlopen=urlopen_mock,
            websocket_factory=make_websocket_factory(*sockets),
        )

        quote = client.fetch_tick("frxEURUSD")
        history = client.tick_history("frxEURUSD")
        snapshot = client.account_snapshot()

        self.assertEqual(quote["price"], 1.0842)
        self.assertEqual(len(history), 30)
        self.assertEqual(snapshot["balance"], 100)
        self.assertEqual(snapshot["currency"], "USD")
        self.assertEqual(urlopen_mock.call_args.args[0].full_url, "https://api.derivws.com/trading/v1/options/accounts/demo-1/otp")

    def test_buy_option_requests_quote_then_buys_fixed_stake(self) -> None:
        ws = FakeWebSocket()
        otp_response = MagicMock()
        otp_response.__enter__.return_value.read.return_value = b'{"data":{"url":"wss://authorized.example"}}'
        client = DerivClient(
            self.settings,
            urlopen=MagicMock(return_value=otp_response),
            websocket_factory=make_websocket_factory(ws),
        )

        contract = client.buy_option(
            symbol="frxEURUSD",
            contract_type="CALL",
            stake=5,
            currency="USD",
            duration=5,
            duration_unit="m",
        )

        self.assertEqual(contract["contract_id"], 456)
        self.assertEqual(contract["stake"], 5)
        proposal = next(item for item in ws.messages if "proposal" in item)
        buy = next(item for item in ws.messages if "buy" in item)
        self.assertEqual(proposal["basis"], "stake")
        self.assertEqual(proposal["contract_type"], "CALL")
        self.assertEqual(proposal["duration_unit"], "m")
        self.assertEqual(buy["price"], 5)

    def test_contract_type_and_stake_are_validated(self) -> None:
        client = DerivClient(self.settings, websocket_factory=make_websocket_factory(FakeWebSocket()))
        with self.assertRaises(ValueError):
            client.buy_option(
                symbol="frxEURUSD",
                contract_type="BUY",
                stake=5,
                currency="USD",
                duration=5,
                duration_unit="m",
            )
        with self.assertRaises(ValueError):
            client.buy_option(
                symbol="frxEURUSD",
                contract_type="CALL",
                stake=5.01,
                currency="USD",
                duration=5,
                duration_unit="m",
            )


class RiskCycleTests(unittest.TestCase):
    def test_autobot_stake_uses_only_remaining_daily_loss_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = ConfigStore(str(Path(directory) / "config.json"))
            config.set_deriv_config(account_id="demo-1", token="token", app_id="1234")
            config.set_deriv_config(allow_live_execution=True, kill_switch=False)
            config.set_autobot_profile(symbols=["frxEURUSD"])
            config.daily_risk_status(100)
            config.daily_risk_status(92)
            journal = JournalStore(str(Path(directory) / "journal.json"))
            client = MagicMock()
            client.account_snapshot.return_value = {"currency": "USD", "balance": 92, "positions": []}
            client.tick_history.return_value = [1.0] * 30
            client.buy_option.side_effect = lambda **values: {
                "contract_id": 456,
                "stake": values["stake"],
            }

            with patch.object(live_server, "_client", return_value=client), patch.object(
                live_server, "_strategy_signal", return_value="CALL"
            ):
                result = live_server._run_live_cycle(config, journal)

        self.assertEqual(client.buy_option.call_args.kwargs["stake"], 2)
        self.assertEqual(result["daily_risk"]["loss"], 10)
        self.assertTrue(result["daily_risk"]["halted"])


class ResearchTests(unittest.TestCase):
    def test_optimizer_is_deterministic_and_isolated(self) -> None:
        optimizer = StrategyOptimizer()
        self.assertEqual(optimizer.optimize(cycles=30), optimizer.optimize(cycles=30))


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_directory = tempfile.TemporaryDirectory()
        cls.server = JarvisHTTPServer(("127.0.0.1", 0), JarvisHandler)
        root = Path(cls.temp_directory.name)
        cls.server.config = ConfigStore(str(root / "config.json"))
        cls.server.journal = JournalStore(str(root / "journal.json"))
        cls.server.execution_lock = threading.Lock()
        cls.server.autobot = AutobotScheduler(cls.server.config, cls.server.journal, cls.server.execution_lock)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.temp_directory.cleanup()

    def test_health_reports_deriv_service(self) -> None:
        with urlopen(self.base_url + "/api/health") as response:
            payload = json.loads(response.read())
        self.assertEqual(payload["provider"], "deriv")

    def test_public_mode_requires_auth_and_rejects_cross_site_posts(self) -> None:
        server = self.server
        server.require_auth = True
        server.require_https_origin = True
        server.auth_username = "phone-user"
        server.auth_password = "long-test-password-123"
        authorization = "Basic " + base64.b64encode(
            b"phone-user:long-test-password-123"
        ).decode("ascii")
        try:
            with self.assertRaises(HTTPError) as context:
                urlopen(self.base_url + "/api/health")
            self.assertEqual(context.exception.code, 401)
            context.exception.close()

            request = Request(self.base_url + "/api/health", headers={"Authorization": authorization})
            with urlopen(request) as response:
                self.assertEqual(response.status, 200)

            request = Request(
                self.base_url + "/api/config",
                data=b"{}",
                headers={
                    "Authorization": authorization,
                    "Origin": "https://attacker.example",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with self.assertRaises(HTTPError) as context:
                urlopen(request)
            self.assertEqual(context.exception.code, 403)
            context.exception.close()
        finally:
            server.require_auth = False
            server.require_https_origin = False

    def test_account_without_credentials_returns_client_error(self) -> None:
        with self.assertRaises(HTTPError) as context:
            urlopen(self.base_url + "/api/account")
        self.assertEqual(context.exception.code, 400)
        payload = json.loads(context.exception.read())
        self.assertIn("Configure Deriv account ID", payload["error"])
        context.exception.close()

    def test_old_simulation_order_endpoints_are_removed(self) -> None:
        request = Request(self.base_url + "/api/trade", data=b"{}", method="POST")
        with self.assertRaises(HTTPError) as context:
            urlopen(request)
        self.assertEqual(context.exception.code, 410)
        context.exception.close()

    def test_legacy_exchange_order_endpoint_is_removed(self) -> None:
        request = Request(self.base_url + "/api/exchange/order", data=b"{}", method="POST")
        with self.assertRaises(HTTPError) as context:
            urlopen(request)
        self.assertEqual(context.exception.code, 410)
        context.exception.close()

    def test_config_api_never_returns_token_and_enforces_caps(self) -> None:
        request = Request(
            self.base_url + "/api/config",
            data=json.dumps({"account_id": "demo-2", "app_id": "1234", "token": "secret", "max_order_stake": 100}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as context:
            urlopen(request)
        self.assertEqual(context.exception.code, 400)
        context.exception.close()

        request = Request(
            self.base_url + "/api/config",
            data=json.dumps({"account_id": "demo-2", "app_id": "1234", "token": "secret"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request) as response:
            payload = json.loads(response.read())
        self.assertTrue(payload["config"]["token_configured"])
        self.assertNotIn("token", payload["config"])

    def test_scheduler_cannot_be_enabled_while_locked(self) -> None:
        request = Request(
            self.base_url + "/api/autobot/profile",
            data=json.dumps({"enabled": True, "symbols": ["frxEURUSD"]}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as context:
            urlopen(request)
        self.assertEqual(context.exception.code, 403)
        context.exception.close()

    def test_manual_option_order_is_locked_by_default(self) -> None:
        request = Request(
            self.base_url + "/api/options/order",
            data=json.dumps({
                "symbol": "frxEURUSD",
                "contract_type": "CALL",
                "stake": 5,
                "duration": 5,
                "duration_unit": "m",
            }).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as context:
            urlopen(request)
        self.assertEqual(context.exception.code, 403)
        context.exception.close()

    def test_live_market_route_uses_provider_snapshot_without_fallback(self) -> None:
        with patch.object(live_server, "_client") as client_factory:
            client_factory.return_value.market_snapshot.return_value = {
                "markets": [{"symbol": "frxEURUSD", "price": 1.08}],
                "unquoted_symbols": [],
            }
            with urlopen(self.base_url + "/api/market") as response:
                payload = json.loads(response.read())

        self.assertEqual(payload["markets"][0]["symbol"], "frxEURUSD")
        client_factory.return_value.market_snapshot.assert_called_once()


if __name__ == "__main__":
    unittest.main()
