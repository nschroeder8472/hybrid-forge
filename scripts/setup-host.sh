#!/usr/bin/env bash
# Sets up the always-on services on the executor host (the GPU machine).
# Run this once on the host. Clients do not run this.
#
# Deliberately does NOT install Ollama or MemPalace for you — it verifies they
# are present, binds them to one specific interface, and checks the result.
#
# FORGE_BIND_ADDR is the address the services listen on. It must be an address
# your client machines can actually reach, and it should be the narrowest one
# that satisfies that: both services are unauthenticated, so whatever network
# this address belongs to gets an open inference endpoint and an open memory
# store. Set FORGE_BIND_HOST too if clients reach the host by name.

set -euo pipefail

MODEL="${FORGE_MODEL:-qwen3.6:35b-a3b}"
PALACE_PORT="${FORGE_PALACE_PORT:-8787}"
BIND_ADDR="${FORGE_BIND_ADDR:-}"

red()  { printf '\033[31m%s\033[0m\n' "$1"; }
green(){ printf '\033[32m%s\033[0m\n' "$1"; }

need() {
  command -v "$1" >/dev/null 2>&1 || { red "missing: $1 — install it first"; exit 1; }
}

echo "==> checking prerequisites"
need ollama
need python3
green "prerequisites present"

echo "==> resolving bind address"
if [ -z "${BIND_ADDR}" ]; then
  red "FORGE_BIND_ADDR is not set."
  echo
  echo "Pick the address of the interface your client machines reach this host on."
  echo "Candidates on this machine:"
  ip -4 -o addr show scope global 2>/dev/null \
    | awk '{printf "  %-12s %s\n", $2, $4}' \
    || echo "  (could not enumerate; check \`ip -4 addr\` by hand)"
  echo
  echo "Then re-run:  FORGE_BIND_ADDR=<address> $0"
  exit 1
fi
# 0.0.0.0 is a real choice on a trusted isolated network, but it is never the
# default here — these services have no authentication of their own.
if [ "${BIND_ADDR}" = "0.0.0.0" ]; then
  red "WARNING: binding to 0.0.0.0 exposes an UNAUTHENTICATED inference endpoint"
  red "         and memory store on every network this host is attached to."
  red "         Only do this behind a firewall or on an isolated network."
fi
BIND_HOST="${FORGE_BIND_HOST:-${BIND_ADDR}}"
green "binding to: ${BIND_ADDR} (clients will use ${BIND_HOST})"

echo "==> pulling executor model (${MODEL})"
ollama pull "${MODEL}"

echo "==> binding ollama to ${BIND_ADDR}"
# Ollama has no authentication. Binding to 0.0.0.0 exposes an unauthenticated
# inference endpoint to every reachable network. Bind to one interface instead.
sudo mkdir -p /etc/systemd/system/ollama.service.d
sudo tee /etc/systemd/system/ollama.service.d/override.conf >/dev/null <<EOF
[Service]
Environment="OLLAMA_HOST=${BIND_ADDR}:11434"
Environment="OLLAMA_KEEP_ALIVE=30m"
EOF
sudo systemctl daemon-reload
sudo systemctl restart ollama
green "ollama bound to ${BIND_ADDR}:11434"

echo "==> verifying executor endpoint"
sleep 2
curl -fsS "http://${BIND_ADDR}:11434/v1/models" >/dev/null && green "executor responding"

cat <<EOF

$(green "host setup complete")

Executor base URL:  http://${BIND_HOST}:11434/v1
Executor model:     ${MODEL}

Remaining manual step — MemPalace:
  1. Install MemPalace on this host and confirm its MCP transport options.
     Its MCP server may default to stdio; this pipeline needs it reachable over
     the network, so either run it with an HTTP transport on port ${PALACE_PORT},
     or front the stdio server with an MCP proxy. Check the current MemPalace
     docs for which of these it supports before wiring clients to it.
  2. Bind it to ${BIND_ADDR} only, for the same reason as Ollama.
  3. Verify: curl http://${BIND_HOST}:${PALACE_PORT}/mcp

Verify from a client machine as well — reaching an endpoint from the host it
runs on proves nothing about whether your clients can:
  curl http://${BIND_HOST}:11434/v1/models

Then on each client machine run:
  claude plugin install hybrid-forge@<your-marketplace>
and set executorBaseUrl / memPalaceUrl to the URLs above.
EOF
