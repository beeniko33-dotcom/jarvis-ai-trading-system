#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

if [[ -f .env ]]; then
    set -a
    source .env
    set +a
fi

if [[ -z "${JARVIS_MCP_BEARER_TOKEN:-}" ]]; then
    token_file="$ROOT/.jarvis_mcp_token"
    if [[ ! -s "$token_file" ]]; then
        if ! command -v openssl >/dev/null 2>&1; then
            echo "openssl is required to generate the local MCP bearer token." >&2
            exit 1
        fi
        (umask 077 && openssl rand -hex 32 > "$token_file")
    fi
    chmod 600 "$token_file"
    JARVIS_MCP_BEARER_TOKEN="$(<"$token_file")"
    export JARVIS_MCP_BEARER_TOKEN
    echo "Generated a private MCP token in .jarvis_mcp_token; read it locally to configure your MCP client."
fi

if [[ -z "${JARVIS_MCP_BEARER_TOKEN:-}" || "$JARVIS_MCP_BEARER_TOKEN" == replace-* || ${#JARVIS_MCP_BEARER_TOKEN} -lt 32 ]]; then
    echo "Set JARVIS_MCP_BEARER_TOKEN to a random secret of at least 32 characters." >&2
    echo "Generate one with: openssl rand -hex 32" >&2
    exit 1
fi
if [[ "${JARVIS_ACCESS_PASSWORD:-}" == replace-* ]]; then
    echo "Replace the sample JARVIS_ACCESS_PASSWORD in .env before starting." >&2
    exit 1
fi
if ! command -v cloudflared >/dev/null 2>&1 && ! command -v docker >/dev/null 2>&1; then
    echo "Install cloudflared or Docker to create a temporary HTTPS URL." >&2
    exit 1
fi

app_status="$(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/api/health || true)"
app_started=0
app_pid=""
if [[ "$app_status" == "000" ]]; then
    ./deploy_jarvis_v6.sh &
    app_pid=$!
    app_started=1
elif [[ "$app_status" != "200" && "$app_status" != "401" ]]; then
    echo "Jarvis returned unexpected health status: $app_status" >&2
    exit 1
fi

mcp_status="$(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:8001/mcp || true)"
if [[ "$mcp_status" != "000" ]]; then
    if [[ "$app_started" == "1" ]]; then
        kill "$app_pid" 2>/dev/null || true
    fi
    echo "Port 8001 is already in use; stop its process before starting the MCP tunnel." >&2
    exit 1
fi

python3 -m jarvis_server.mcp_server &
mcp_pid=$!

cleanup() {
    kill "$mcp_pid" 2>/dev/null || true
    if [[ "$app_started" == "1" ]]; then
        kill "$app_pid" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

echo "Starting a temporary public MCP URL. Keep this terminal open while using Jarvis."
echo "The MCP endpoint is protected by JARVIS_MCP_BEARER_TOKEN."
if command -v cloudflared >/dev/null 2>&1; then
    cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8001
else
    docker run --rm --network host cloudflare/cloudflared:latest tunnel --no-autoupdate --url http://127.0.0.1:8001
fi