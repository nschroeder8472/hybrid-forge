#!/usr/bin/env bash
# Sets up the always-on services on the executor host (the GPU machine).
# Run this once on the host. Clients do not run this.
#
# Deliberately does NOT install llama.cpp or MemPalace for you — it verifies
# they are present, starts the router bound to one specific interface, and
# checks the result.
#
# FORGE_BIND_ADDR is the address the services listen on. It must be an address
# your client machines can actually reach, and it should be the narrowest one
# that satisfies that: both services are unauthenticated, so whatever network
# this address belongs to gets an open inference endpoint and an open memory
# store. Set FORGE_BIND_HOST too if clients reach the host by name.
#
# FORGE_PRESET is the llama.cpp preset to serve. `forge models` writes it from
# a project's config.json into .hybridforge/models/llamacpp.ini — generated
# rather than hand-written so that `ctx-size` here and `contextWindow` there
# cannot drift apart.

set -euo pipefail

PRESET="${FORGE_PRESET:-}"
PORT="${FORGE_PORT:-8080}"
MODELS_MAX="${FORGE_MODELS_MAX:-1}"
PALACE_PORT="${FORGE_PALACE_PORT:-8787}"
BIND_ADDR="${FORGE_BIND_ADDR:-}"

red()  { printf '\033[31m%s\033[0m\n' "$1"; }
green(){ printf '\033[32m%s\033[0m\n' "$1"; }

need() {
  command -v "$1" >/dev/null 2>&1 || { red "missing: $1 — install it first"; exit 1; }
}

echo "==> checking prerequisites"
need llama-server
need python3
green "prerequisites present"

# The backend llama-server was built with decides whether this is usable, not
# the version. Measured on a 5090 with a 30B A3B MoE at Q4_K_M: 16 tok/s on the
# Vulkan build against 353 tok/s on CUDA.
if ! llama-server --version 2>&1 | grep -qi 'cuda\|metal\|rocm'; then
  red "WARNING: llama-server does not report a GPU backend (CUDA/Metal/ROCm)."
  red "         A CPU or Vulkan build will run and will be many times slower."
fi

echo "==> resolving preset"
if [ -z "${PRESET}" ]; then
  red "FORGE_PRESET is not set."
  echo
  echo "Point it at the preset to serve. Generate one from a project's config with:"
  echo "  forge models        # writes .hybridforge/models/llamacpp.ini"
  echo
  echo "Then re-run:  FORGE_PRESET=<path> FORGE_BIND_ADDR=<address> $0"
  exit 1
fi
if [ ! -f "${PRESET}" ]; then
  red "no such preset: ${PRESET}"
  exit 1
fi
green "serving preset: ${PRESET}"

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

echo "==> installing the router as a service"
# --models-max 1 keeps one checkpoint resident. Raising it is only safe when
# every model in the preset fits in VRAM together; the role that finds out
# otherwise is the one whose child server exits mid-run.
sudo tee /etc/systemd/system/llama-server.service >/dev/null <<EOF
[Unit]
Description=llama.cpp router for hybrid-forge
After=network-online.target

[Service]
ExecStart=$(command -v llama-server) \\
  --host ${BIND_ADDR} --port ${PORT} \\
  --models-preset ${PRESET} \\
  --models-max ${MODELS_MAX}
Restart=on-failure
User=${SUDO_USER:-${USER}}

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now llama-server
sudo systemctl restart llama-server
green "llama-server bound to ${BIND_ADDR}:${PORT}"

echo "==> verifying router endpoint"
sleep 2
curl -fsS "http://${BIND_ADDR}:${PORT}/health" >/dev/null && green "router responding"

echo "==> models the router will serve"
# These ids are what `model` in config.json must name. The router 400s an id it
# does not have, which is the good failure — but finding out here is better.
curl -fsS "http://${BIND_ADDR}:${PORT}/v1/models" \
  | python3 -c 'import json,sys; [print("  %s (%s)" % (m["id"], (m.get("status") or {}).get("value","?"))) for m in json.load(sys.stdin).get("data", [])]' \
  || red "  (could not read the catalogue — check the router log)"

cat <<EOF

$(green "host setup complete")

Router base URL:  http://${BIND_HOST}:${PORT}/v1
Preset:           ${PRESET}
Resident at once: ${MODELS_MAX}

Remaining manual step — MemPalace:
  1. Install MemPalace on this host and confirm its MCP transport options.
     Its MCP server may default to stdio; this pipeline needs it reachable over
     the network, so either run it with an HTTP transport on port ${PALACE_PORT},
     or front the stdio server with an MCP proxy. Check the current MemPalace
     docs for which of these it supports before wiring clients to it.
  2. Bind it to ${BIND_ADDR} only, for the same reason as llama-server.
  3. Verify: curl http://${BIND_HOST}:${PALACE_PORT}/mcp

Verify from a client machine as well — reaching an endpoint from the host it
runs on proves nothing about whether your clients can:
  curl http://${BIND_HOST}:${PORT}/v1/models

Then on each client machine run:
  claude plugin install hybrid-forge@<your-marketplace>
and set executorBaseUrl / memPalaceUrl to the URLs above.

Re-run this after \`forge models\` rewrites the preset — the router reads it at
startup, so a changed preset needs a restart to take effect.
EOF
