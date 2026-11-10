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

By default the terminal listens on `http://127.0.0.1:8000` and binds to loopback
only. Do not expose that local server directly. For a public phone-accessible
deployment, use the HTTPS and authentication setup below.

Enter credentials in Settings in the local terminal. Credentials are stored in the
ignored `.jarvis_config.json` file with owner-only permissions on POSIX systems and
are never returned by configuration APIs. Never paste an API token in chat or commit
it to source control.

## Public HTTPS deployment (Docker Compose)

This provider-neutral setup uses Caddy for automatic HTTPS and browser HTTP Basic
Authentication for an additional app-level password. The app container is not
published to the host; only Caddy's HTTP/HTTPS ports are exposed. A persistent
Docker volume stores configuration, Deriv credentials, and the journal.

Requirements: a Linux host with Docker Compose, a domain name whose DNS `A`/`AAAA`
records point to that host, and inbound TCP ports 80 and 443 allowed by its firewall.
Do not use a Codespaces public port or expose port 8000 directly.

```bash
cp .env.example .env
chmod 600 .env
```

Edit `.env`: set `JARVIS_DOMAIN` to your domain, choose a username, and set a unique
strong password. Generate one with `openssl rand -hex 32`. Keep the `.env` file
private; never commit or share it.

```bash
docker compose up -d --build
docker compose ps
docker compose logs caddy
```

After Caddy obtains the HTTPS certificate, open `https://your-domain/` on the phone
and enter the app username and password when prompted. Then enter Deriv credentials
in the terminal's Settings page; they are stored in the persistent `/data` volume.
Keep the account in demo mode and execution locked until you have verified the
deployment and account. The application does not enable automation at startup.

Back up the `jarvis_data` Docker volume securely; it contains Deriv credentials.
Do not expose Docker's app port, remove the Basic Auth environment settings, or
terminate TLS outside a trusted HTTPS proxy. Deploying this setup requires your own
domain and host; this repository does not provision either.

## Temporary remote MCP endpoint

For prototype use, the MCP server can be exposed through a temporary Cloudflare
Quick Tunnel URL without buying a domain or provisioning a VPS. The URL changes
when restarted and works only while this computer and the launcher remain running.
Install `cloudflared` or Docker. The launcher creates a private bearer token in
`.jarvis_mcp_token` the first time if no token is configured. Alternatively,
configure the `.env` file:

```bash
cp .env.example .env
chmod 600 .env
openssl rand -hex 32
```

Put the generated value in `JARVIS_MCP_BEARER_TOKEN` in `.env`, replace the sample
access password, and start the endpoint. If using the generated token file, read
it locally with `cat .jarvis_mcp_token` when configuring the MCP client; do not
paste the token into chat or commit it.

```bash
bash ./deploy_jarvis_mcp.sh
```

The terminal prints a temporary HTTPS hostname. Configure an MCP client with
`https://<temporary-hostname>/mcp` and the bearer token from `.env`. This is a
standard Streamable HTTP MCP endpoint; support for custom remote MCP servers varies
between Grok products and API clients. If the Grok interface you use does not offer
custom MCP connections, use an MCP-capable client or an xAI API integration that
supports remote MCP.

The server exposes read-only tools by default. To expose one-shot orders and
scheduler controls, set `JARVIS_MCP_ENABLE_TRADING=true` in `.env` before launch.
Order placement, one-shot bot runs, and scheduler start require an explicit
`confirm=true`; Jarvis's existing execution lock, allowlist, stake cap, daily loss
limit, and open-contract limit still apply. The model cannot unlock execution or
change account credentials through MCP. Scheduler start can repeatedly place trades
while enabled, so keep trading tools disabled until the endpoint and account are
verified. Never share the bearer token or commit `.env`.

The web terminal remains at `http://127.0.0.1:8000`; only the MCP service on port
8001 is published by the tunnel.

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
