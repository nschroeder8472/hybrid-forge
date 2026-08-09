#!/usr/bin/env bash
# Sets up the always-on services on the executor host (the 5090 desktop).
# Run this once on the host. Clients do not run this.
#
# Deliberately does NOT install Ollama or MemPalace for you — it verifies they
# are present, binds them to the Tailscale interface only, and checks the result.

set -euo pipefail

MODEL="${FORGE_MODEL:-qwen3.6:35b-a3b}"
PALACE_PORT="${FORGE_PALACE_PORT:-8787}"

red()  { printf '\033[31m%s\033[0m\n' "$1"; }
green(){ printf '\033[32m%s\033[0m\n' "$1"; }

need() {
  command -v "$1" >/dev/null 2>&1 || { red "missing: $1 — install it first"; exit 1; }
}

echo "==> checking prerequisites"
need ollama
need tailscale
need python3
green "prerequisites present"

echo "==> resolving tailscale address"
TS_IP="$(tailscale ip -4 | head -n1)"
TS_NAME="$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
green "tailscale: ${TS_NAME} (${TS_IP})"

echo "==> pulling executor model (${MODEL})"
ollama pull "${MODEL}"

echo "==> binding ollama to tailscale only"
# Ollama has no authentication. Binding to 0.0.0.0 exposes an unauthenticated
# inference endpoint to your entire LAN. Bind to the Tailscale address instead.
sudo mkdir -p /etc/systemd/system/ollama.service.d
sudo tee /etc/systemd/system/ollama.service.d/override.conf >/dev/null <<EOF
[Service]
Environment="OLLAMA_HOST=${TS_IP}:11434"
Environment="OLLAMA_KEEP_ALIVE=30m"
EOF
sudo systemctl daemon-reload
sudo systemctl restart ollama
green "ollama bound to ${TS_IP}:11434"

echo "==> verifying executor endpoint"
sleep 2
curl -fsS "http://${TS_IP}:11434/v1/models" >/dev/null && green "executor responding"

cat <<EOF

$(green "host setup complete")

Executor base URL:  http://${TS_NAME}:11434/v1
Executor model:     ${MODEL}

Remaining manual step — MemPalace:
  1. Install MemPalace on this host and confirm its MCP transport options.
     Its MCP server may default to stdio; this pipeline needs it reachable over
     the network, so either run it with an HTTP transport on port ${PALACE_PORT},
     or front the stdio server with an MCP proxy. Check the current MemPalace
     docs for which of these it supports before wiring clients to it.
  2. Bind it to ${TS_IP} only, for the same reason as Ollama.
  3. Verify: curl http://${TS_NAME}:${PALACE_PORT}/mcp

Then on each client machine run:
  claude plugin install hybrid-forge@<your-marketplace>
and set executorBaseUrl / memPalaceUrl to the URLs above.
EOF
