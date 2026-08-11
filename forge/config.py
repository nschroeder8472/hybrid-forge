"""Project configuration: `.hybridforge/config.json`.

The important idea is **roles, not models**. The loop asks for "the planner" or
"the executor"; config decides which of the user's declared models plays that
part. Any model can play any role, so the same file supports a local 30B doing
everything, Claude planning while a local model builds, or a Gemini reviewer
second-guessing an OpenAI executor — with no code change.

```json
{
  "models": {
    "local":  {"kind": "openai",     "baseUrl": "http://forge:11434/v1",
               "model": "qwen3.6:35b-a3b", "contextWindow": 32768},
    "claude": {"kind": "claude-cli", "model": "opus",
               "rateLimit": {"tokensPerWindow": 0, "costPerWindow": 0,
                             "windowSeconds": 18000}}
  },
  "roles": {
    "planner": "claude", "executor": "local",
    "tester":  "local",  "reviewer": "claude"
  },
  "commands": {"lint": "...", "typecheck": "...", "test": "..."},
  "neverDelegate": ["src/auth/**"],
  "memory": {"command": ["mempalace-mcp"], "room": "image-marquee"},
  "loop": {"maxAttempts": 3, "autoCommit": false},
  "ui": {"host": "127.0.0.1", "port": 8799}
}
```
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .budget import RateLimitPolicy
from .providers import Provider, build_provider

CONFIG_DIR = ".hybridforge"
CONFIG_FILE = "config.json"
DB_FILE = "run.db"

ROLES = ("planner", "executor", "tester", "reviewer")


class ConfigError(Exception):
    """Configuration is missing or internally inconsistent."""


@dataclass
class LoopSettings:
    """Knobs governing how hard the loop tries before handing back to a human."""

    # Rework attempts per ticket before it is parked as blocked. Three is
    # enough to absorb a lint error and a shallow test failure; beyond that the
    # failure is usually a spec problem no amount of retrying fixes.
    max_attempts: int = 3
    # Commit each verified ticket. Off by default — the first runs of an
    # autonomous loop should leave their work in the tree for inspection.
    auto_commit: bool = False
    # Stop the whole run when a ticket blocks, rather than moving on. On means
    # a blocked ticket gets attention; off means the run keeps making progress
    # elsewhere and reports blockers at the end.
    stop_on_blocked: bool = False
    # Seconds between control-channel checks while waiting.
    poll_seconds: float = 2.0
    # Cap on unattended wall-clock time. 0 disables.
    max_runtime_seconds: int = 0
    # Run the verify commands once before each ticket, so a failure that was
    # already there is not blamed on the ticket that happened to run next.
    # Worth its cost on any project where the verify step is incremental; turn
    # it off when a full suite is slow enough that paying it per ticket costs
    # more than the attempts it saves.
    baseline_verify: bool = True


@dataclass
class UISettings:
    host: str = "127.0.0.1"
    port: int = 8799
    # Binding beyond loopback exposes pause/stop controls with no auth, so it
    # stays an explicit choice — same posture as the executor host itself.
    enabled: bool = True


@dataclass
class Config:
    root: Path
    room: str = ""
    models: dict[str, dict[str, Any]] = field(default_factory=dict)
    roles: dict[str, str] = field(default_factory=dict)
    commands: dict[str, str] = field(default_factory=dict)
    never_delegate: list[str] = field(default_factory=list)
    # Raw block; parsed by forge.memory so config.py stays free of MCP details.
    memory: dict[str, Any] = field(default_factory=dict)
    loop: LoopSettings = field(default_factory=LoopSettings)
    ui: UISettings = field(default_factory=UISettings)

    # ------------------------------------------------------------------

    @property
    def config_dir(self) -> Path:
        return self.root / CONFIG_DIR

    @property
    def db_path(self) -> Path:
        return self.config_dir / DB_FILE

    @property
    def tickets_dir(self) -> Path:
        return self.config_dir / "tickets"

    # ------------------------------------------------------------------

    @classmethod
    def load(cls, root: Path | str = ".") -> "Config":
        root = Path(root).resolve()
        path = root / CONFIG_DIR / CONFIG_FILE
        if not path.exists():
            raise ConfigError(
                f"no {CONFIG_DIR}/{CONFIG_FILE} in {root}. Run `forge init` first."
            )
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{path} is not valid JSON: {exc}") from exc

        config = cls(
            root=root,
            room=data.get("room", ""),
            models=data.get("models", {}) or {},
            roles=data.get("roles", {}) or {},
            commands=data.get("commands", {}) or {},
            never_delegate=data.get("neverDelegate", []) or [],
            memory=data.get("memory", {}) or {},
        )

        loop = data.get("loop", {}) or {}
        config.loop = LoopSettings(
            max_attempts=int(loop.get("maxAttempts", 3)),
            auto_commit=bool(loop.get("autoCommit", False)),
            stop_on_blocked=bool(loop.get("stopOnBlocked", False)),
            poll_seconds=float(loop.get("pollSeconds", 2.0)),
            max_runtime_seconds=int(loop.get("maxRuntimeSeconds", 0)),
            baseline_verify=bool(loop.get("baselineVerify", True)),
        )

        ui = data.get("ui", {}) or {}
        config.ui = UISettings(
            host=ui.get("host", "127.0.0.1"),
            port=int(ui.get("port", 8799)),
            enabled=bool(ui.get("enabled", True)),
        )

        config.validate()
        return config

    def validate(self) -> None:
        if not self.models:
            raise ConfigError("config declares no models under `models`.")
        for role in ROLES:
            name = self.roles.get(role)
            if not name:
                raise ConfigError(f"config has no model assigned to role {role!r}.")
            if name not in self.models:
                raise ConfigError(
                    f"role {role!r} points at model {name!r}, which is not declared "
                    f"in `models` (have: {', '.join(sorted(self.models))})."
                )

    # ------------------------------------------------------------------

    def model_block(self, name: str) -> dict[str, Any]:
        """A model's config with project-scoped defaults filled in.

        `cwd` is the load-bearing one. Adapters that shell out — `claude-cli`
        and `command` — otherwise inherit the daemon's process directory, and
        the daemon is routinely started from somewhere else: `forge --root
        <path>`, a scheduled task, an editor's terminal. A planner run in the
        wrong directory does not fail. It reads whatever repository it landed
        in and writes confident, well-formed tickets about that one, naming
        files and conventions the target project has never had.

        An explicit `cwd` in the block still wins, so a deliberate override —
        pointing the planner at a sibling checkout, say — survives.
        """
        block = dict(self.models[name])
        block.setdefault("cwd", str(self.root))
        return block

    def provider_for(self, role: str) -> Provider:
        """Build the provider playing a role.

        Constructed fresh rather than cached because credentials come from the
        environment and a long-running daemon should pick up a rotated key
        without a restart.
        """
        if role not in ROLES:
            raise ConfigError(f"unknown role {role!r}; expected one of {', '.join(ROLES)}")
        name = self.roles[role]
        return build_provider(name, self.model_block(name))

    def model_name_for(self, role: str) -> str:
        return self.roles[role]

    @property
    def record_role(self) -> str:
        """Which role decides what is worth writing to project memory.

        The reviewer by default: it is the judgment role, and it has just read
        the diff against the spec, so it is best placed to tell a durable
        decision from ticket narration. Override with `memory.recordRole` to
        put that (cheap, short) call on a different model.
        """
        role = str(self.memory.get("recordRole", "reviewer"))
        if role not in ROLES:
            raise ConfigError(
                f"memory.recordRole is {role!r}, which is not a role "
                f"(expected one of {', '.join(ROLES)})."
            )
        return role

    def rate_limit_policies(self) -> dict[str, RateLimitPolicy]:
        return {
            name: RateLimitPolicy.from_config(block.get("rateLimit"))
            for name, block in self.models.items()
        }

    def write(self) -> Path:
        """Serialize back to disk, preserving the documented shape."""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "room": self.room,
            "models": self.models,
            "roles": self.roles,
            "commands": self.commands,
            "neverDelegate": self.never_delegate,
            "memory": self.memory,
            "loop": {
                "maxAttempts": self.loop.max_attempts,
                "autoCommit": self.loop.auto_commit,
                "stopOnBlocked": self.loop.stop_on_blocked,
                "pollSeconds": self.loop.poll_seconds,
                "maxRuntimeSeconds": self.loop.max_runtime_seconds,
                "baselineVerify": self.loop.baseline_verify,
            },
            "ui": {"host": self.ui.host, "port": self.ui.port, "enabled": self.ui.enabled},
        }
        path = self.config_dir / CONFIG_FILE
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return path


def default_config(root: Path) -> Config:
    """A starting config that runs entirely against a local Ollama.

    Deliberately single-model: it works the moment Ollama is up, and the user
    can promote Claude into the planner and reviewer roles once they decide
    what they want reviewing their diffs.
    """
    return Config(
        root=root,
        models={
            "local": {
                "kind": "openai",
                "baseUrl": "http://localhost:11434/v1",
                "model": "qwen3.6:35b-a3b",
                "maxOutputTokens": 8192,
            }
        },
        roles={role: "local" for role in ROLES},
        commands={"lint": "", "typecheck": "", "test": ""},
        never_delegate=[],
        # Present but empty: the shape is discoverable without reading the
        # docs, and an empty url leaves retrieval off until it is filled in.
        memory={"url": "", "room": ""},
    )
