# Hybrid Forge

A Claude Code plugin for a plan-and-execute coding pipeline. Claude handles
planning, triage, and review. A locally hosted Qwen3.6-35B-A3B handles
implementation. MemPalace carries project decisions across sessions so the
executor does not contradict conventions established weeks ago.

The point is not to replace Claude with a local model. It is to spend expensive
reasoning tokens on the parts that need reasoning, and let the local model absorb
the token-heavy bulk generation for the cost of electricity.

## How it works

```
Claude: plan          Backlog + implementation spec, with explicit triage
   ↓
Qwen3.6-35B: build    Implements against the spec; encodes given criteria as tests
   ↓
Automated checks      Lint, type-check, test suite — before Claude sees anything
   ↓
Claude: review        Diff against the spec, not against "tests passed"
   ↓
Merged                Durable decisions written back to project memory
```

## What it deliberately does not do

**The executor never decides scope.** It receives an explicit spec, an allowed
file list, and acceptance criteria. If the spec is ambiguous it returns
`BLOCKED:` rather than guessing.

**The executor never authors its own acceptance criteria.** A model that writes
both the implementation and the test it is judged against will encode its bugs as
passing tests. Criteria come from planning; the executor only encodes them.

**Triage is not delegated.** Deciding whether a ticket is safe to hand off
requires knowing what you do not know, which smaller models are unreliable at.
Auth, concurrency, migrations, and public API surface stay with Claude.

## Architecture

Heavy components live on one host and are reached over Tailscale, so you can code
from any machine against the same model and the same project memory:

- **Executor** — Ollama serving Qwen3.6-35B-A3B over an OpenAI-compatible HTTP
  API. Not MCP; MCP exposes tools, not inference endpoints.
- **`forge-executor`** — a small stdio MCP server in this repo that wraps that
  HTTP API as a delegation tool. Stateless, runs on the client.
- **MemPalace** — hosted centrally, one authoritative palace. The repo stores a
  room pointer, never the database.

## Setup

See [docs/SETUP.md](docs/SETUP.md).

## Layout

```
.claude-plugin/plugin.json   Manifest and user-configurable endpoints
.mcp.json                    Bundled MCP servers
commands/                    /forge-init, /forge-plan, /forge-run
skills/delegation-protocol/  Triage rules and the ticket contract
skills/project-memory/       Memory read/write discipline
mcp/executor_server.py       The delegation shim
scripts/setup-host.sh        Host verification and bind configuration
templates/ticket.md          Ticket shape
```

## Status

Early. The delegation shim and skills are the parts worth reading; the MemPalace
integration assumes a network-reachable MCP transport that you should verify
against your installed version.
