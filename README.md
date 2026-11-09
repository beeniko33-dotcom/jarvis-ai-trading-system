# Jarvis Deriv Trading Terminal

The app uses Deriv's live market-data and fixed-stake options APIs. Runtime prices,
account balances, positions, and trades come from Deriv; there are no synthetic
market fallbacks in the terminal. The offline backtester remains synthetic and is
isolated from the trading path.

## Requirements and local startup

- Python 3.10+
- A Deriv account and a Deriv API token with the required trade scope for account
  access and contract purchase
- A Deriv App ID and the target account ID

```bash
python -m pip install -r requirements.txt
./deploy_jarvis_v6.sh
```

By default the terminal listens on `http://127.0.0.1:8000`. It intentionally binds
to loopback only. Do not expose this unauthenticated local server through a public
address, port-forward, or unsecured tunnel. Phone access requires a separately
configured HTTPS deployment with authentication and appropriate secret management;
this repository does not deploy or provision that infrastructure.

Enter credentials in Settings in the local terminal. Credentials are stored in the
ignored `.jarvis_config.json` file with owner-only permissions on POSIX systems and
are never returned by configuration APIs. Never paste an API token in chat or commit
it to source control.

## Trading behavior and safeguards

- Provider instruments are discovered from Deriv's active-symbol API; the app asks
  Deriv for current quotes and tick history. Unsupported or unavailable symbols are
  reported without fallback prices.
- Option orders use fixed-stake CALL/PUT proposals. This is not MT5 CFD or exchange
  futures trading. Product availability must be confirmed by Deriv for each symbol.
- Defaults are Deriv demo, locked execution, kill switch on, and scheduler stopped.
  Changing the account identity locks execution again. Starting the scheduler is a
  separate explicit action.
- Hard limits cannot be raised above **$5 per contract**, **$10 daily account-balance
  loss**, and **2 open contracts**. A daily stop is persisted and uses UTC day
  boundaries. Use a dedicated account; external deposits, withdrawals, and unrelated
  trades affect balance-based loss tracking.
- A CALL/PUT option can lose its entire stake. This bot is experimental and is not
  financial advice. Verify product eligibility, account access, and local rules with
  Deriv before enabling real-account execution.
- The scheduler is not restarted automatically after app restart. Provider/API
  failures stop the scheduler and are shown in the UI and status API.
- On startup, legacy journal rows marked as paper or dry-run are removed; recorded
  Deriv and manual entries are retained.

## Offline research

`POST /api/backtest` and `POST /api/optimize` remain isolated synthetic-data
research tools. They do not place orders or populate live market state.

```bash
curl -sS -X POST http://127.0.0.1:8000/api/backtest \
  -H 'Content-Type: application/json' \
  -d '{"cycles":60,"cash":100000,"symbols":["BTCUSD","ETHUSD"]}'
```

## API overview

- `GET /api/health`
- `GET /api/config` (redacted)
- `GET /api/market`
- `GET /api/market/quote?symbol=...`
- `GET /api/market/history?symbol=...`
- `GET /api/account`
- `POST /api/config`
- `POST /api/options/order` (fixed-stake option, explicit browser confirmation)
- `GET|POST /api/autobot/profile`
- `GET /api/autobot/status`
- `GET /api/autobot/journal?limit=100&symbol=...`
- `POST /api/autobot/run`
- `POST /api/autobot/stop`
- `POST /api/backtest` and `POST /api/optimize` (offline only)

The former simulation order endpoints return HTTP 410.

## Deriv API references

- [API overview and authentication](https://developers.deriv.com/docs/intro/api-overview/)
- [OTP WebSocket connection](https://developers.deriv.com/docs/options/websocket/)
- [Active symbols](https://developers.deriv.com/schemas/active_symbols_request.schema.json)
- [Tick history](https://developers.deriv.com/schemas/ticks_history_request.schema.json)
- [Options proposals](https://developers.deriv.com/docs/trading/proposal/)
- [Buy contracts](https://developers.deriv.com/docs/trading/buy/)
- [Account portfolio](https://developers.deriv.com/docs/account/portfolio/)
- [Closed-contract profit table](https://developers.deriv.com/docs/account/profit-table/)

## Tests

```bash
python -m unittest discover -s tests -v
python -m py_compile jarvis_server/*.py tests/*.py
git diff --check
```
