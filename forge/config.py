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
  "loop": {"maxAttempts": 3, "autoCommit": false, "retryCycles": 0},
  "ui": {"host": "127.0.0.1", "port": 8799}
}
```
"""

from __future__ import annotations

import json
import re
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


# Language names people write in config, and the extensions each one owns. The
# expansion matters more than the spelling: a project that says "javascript"
# means its `.mjs` files too, and a `.mjs` file nothing claims is a language
# the loop would report as having no runner.
_LANGUAGE_SUFFIXES: dict[str, tuple[str, ...]] = {
    "rust": (".rs",),
    "python": (".py",),
    "javascript": (".js", ".mjs", ".cjs", ".jsx"),
    "typescript": (".ts", ".tsx", ".mts", ".cts"),
    "go": (".go",),
    "ruby": (".rb",),
    "java": (".java",),
    "kotlin": (".kt", ".kts"),
    "swift": (".swift",),
    "c": (".c", ".h"),
    "cpp": (".cpp", ".cc", ".cxx", ".hpp"),
    "csharp": (".cs",),
    "php": (".php",),
    "shell": (".sh", ".bash"),
    "powershell": (".ps1",),
    "lua": (".lua",),
    "elixir": (".ex", ".exs"),
    "scala": (".scala",),
    "dart": (".dart",),
}

# Spellings that mean one of the above.
_LANGUAGE_ALIASES = {
    "rs": "rust",
    "py": "python",
    "js": "javascript",
    "node": "javascript",
    "ts": "typescript",
    "golang": "go",
    "rb": "ruby",
    "kt": "kotlin",
    "c++": "cpp",
    "cplusplus": "cpp",
    "c#": "csharp",
    "cs": "csharp",
    "sh": "shell",
    "bash": "shell",
    "pwsh": "powershell",
    "ps1": "powershell",
    "ex": "elixir",
}

# A command that names one of these is running that language and no other, so a
# key claiming otherwise is a configuration mistake worth catching at startup
# rather than one failing ticket at a time.
_RUNNER_LANGUAGES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("cargo", (".rs",)),
    ("pytest", (".py",)),
    ("unittest", (".py",)),
    ("mypy", (".py",)),
    ("ruff", (".py",)),
    ("go test", (".go",)),
    ("go vet", (".go",)),
    ("dotnet test", (".cs",)),
    ("swift test", (".swift",)),
    ("xcodebuild", (".swift",)),
    ("rspec", (".rb",)),
    ("phpunit", (".php",)),
    ("eslint", (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx")),
    ("tsc", (".ts", ".tsx", ".mts", ".cts")),
    ("jest", (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx")),
    ("vitest", (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx")),
    ("mocha", (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx")),
    ("node", (".js", ".mjs", ".cjs", ".jsx")),
    ("npm", (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx")),
    ("pnpm", (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx")),
    ("yarn", (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx")),
    ("deno", (".js", ".ts", ".tsx", ".mjs")),
    ("bun", (".js", ".ts", ".tsx", ".mjs", ".jsx")),
    ("clippy", (".rs",)),
)

# Every command applies to this when the config gives one string rather than a
# map, and it is a legal key in its own right for a runner that covers the lot.
ANY_LANGUAGE = "*"

# What a config writes to say "nothing runs this language, on purpose". `false`
# is unambiguous in JSON and cannot be mistaken for a command; "skip" and
# "none" are accepted because they are what people type.
_EXEMPTIONS = frozenset({"skip", "none", "false", "no"})


def _is_exemption(value: object) -> bool:
    if value is False:
        return True
    return isinstance(value, str) and value.strip().lower() in _EXEMPTIONS


def _named_runners(command: str) -> list[tuple[str, ...]]:
    """The languages of every runner this command names.

    Word boundaries matter more than they look: `cargo test` contains the
    substring "go test", and a plain `in` check read a Rust project as a Go one
    and reported its own `.rs` files as uncovered.
    """
    lowered = command.lower()
    return [
        suffixes
        for needle, suffixes in _RUNNER_LANGUAGES
        if re.search(r"\b" + re.escape(needle) + r"\b", lowered)
    ]


def _wrong_language(suffix: str, command: str) -> str:
    """What a command runs, when none of it can run the given extension.

    Answers empty for a command naming no runner this knows — `make test`
    under `.js` is nobody's business here — and for a compound command where
    any part covers the language, which is how `cargo test && node --test`
    legitimately covers two.
    """
    if suffix == ANY_LANGUAGE:
        return ""
    named = _named_runners(command)
    if not named or any(suffix in suffixes for suffixes in named):
        return ""
    return ", ".join(sorted({s for suffixes in named for s in suffixes}))


def normalize_language(key: str) -> tuple[str, ...]:
    """The extensions a `commands` key covers.

    Extensions are canonical, names are accepted: `.rs`, `rs` and `rust` are
    the same key, and `javascript` expands to every extension it owns. An
    unknown key that looks like an extension is taken at its word — the loop
    meets languages this table has never heard of, and refusing them would be
    worse than not knowing their name.
    """
    raw = key.strip().lower()
    if not raw:
        return ()
    if raw == ANY_LANGUAGE:
        return (ANY_LANGUAGE,)
    name = _LANGUAGE_ALIASES.get(raw.lstrip("."), raw.lstrip("."))
    if name in _LANGUAGE_SUFFIXES:
        return _LANGUAGE_SUFFIXES[name]
    return (raw if raw.startswith(".") else f".{raw}",)


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
    # Whole-backlog retry cycles once the run has ended anything other than
    # done. Each one requeues every ticket that failed, blocked or was skipped
    # and starts the loop over them again. 0 hands back to a human, which is
    # the safe default; -1 means keep going until the backlog is clean or
    # somebody stops the run.
    retry_cycles: int = 0
    # Have the planner rewrite each requeued ticket from why it failed before
    # the next cycle starts — `forge retry --respec`, applied automatically.
    # On by default: a cycle that re-runs the spec which already failed is a
    # slower version of the same failure.
    respec_on_retry: bool = True
    # Let a respec rewrite the acceptance criteria as well as the spec. Off,
    # and the loop already enforces the same rule one level down: the party
    # being judged does not write the standard it is judged against. Left on,
    # a ticket that keeps failing accumulates criteria derived from its own
    # failures until they contradict the plan — one ended up asserting the
    # opposite of what its author wrote, and every role downstream believed it.
    respec_criteria: bool = False
    # Re-open a ticket that passed on top of a dependency a respec has since
    # rewritten. Its `done` was earned against a contract that no longer
    # exists, and leaving it green reports a clean backlog over a spec that
    # moved underneath it. On, because that misreport is the failure this
    # whole mechanism exists to prevent — but it can re-open a large part of a
    # backlog after one respec, and on local models that is minutes per
    # ticket. Turn it off to be warned instead.
    reopen_stale_dependents: bool = True
    # Probe every configured model before the first ticket. `forge doctor`
    # has always caught a dead endpoint in two seconds; without this the
    # loop found out one ticket at a time and reported the symptom rather
    # than the cause. Off is for a run started immediately after a doctor
    # that already passed, or a model slow enough that loading it twice is
    # worth avoiding.
    preflight: bool = True
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
    # Prior attempts replayed to the executor as real conversation turns — its
    # own reply as an `assistant` message, the failure that followed as the
    # next `user` one — capped at this many. 0 keeps the single-message shape.
    #
    # Off by default because it is an experiment with a known risk in both
    # directions. The executor has never seen its own output: it is shown the
    # files as they exist on disk, with nothing saying it wrote them, which is
    # the state that produced "Looking at the files provided, I can see they
    # already implement the spec correctly." As a conversation that confusion
    # cannot arise, and the turns append rather than mutate, so the KV prefix
    # stays stable instead of being re-prefilled every attempt. Against that:
    # a model shown its own wrong answer as an assistant turn defends it more
    # readily. The current shape already anchors through disk state, so the
    # trade is not clean either way — which is why this is measured on a
    # backlog rather than switched on.
    executor_turns: int = 0
    # Hypotheses a bug ticket may go through before it parks for a human. The
    # first is the planner's reading of the report; each one after it is a
    # re-diagnosis, asked for when the reproduction could not be written —
    # because a test that passes against the named code has *disproved* that
    # reading, and disproof is evidence, not a dead end. 1 parks on the first
    # wrong guess, which is what this did before the re-diagnosis existed.
    bug_hypotheses: int = 3


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
            retry_cycles=int(loop.get("retryCycles", 0)),
            respec_on_retry=bool(loop.get("respecOnRetry", True)),
            respec_criteria=bool(loop.get("respecCriteria", False)),
            reopen_stale_dependents=bool(
                loop.get("reopenStaleDependents", True)
            ),
            preflight=bool(loop.get("preflight", True)),
            poll_seconds=float(loop.get("pollSeconds", 2.0)),
            max_runtime_seconds=int(loop.get("maxRuntimeSeconds", 0)),
            baseline_verify=bool(loop.get("baselineVerify", True)),
            executor_turns=int(loop.get("executorTurns", 0)),
            bug_hypotheses=int(loop.get("bugHypotheses", 3)),
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
        if self.loop.retry_cycles < -1:
            # Caught here rather than clamped: a negative number that is not -1
            # is a typo, and guessing which one it meant would either burn the
            # user's tokens forever or silently do nothing.
            raise ConfigError(
                f"loop.retryCycles is {self.loop.retry_cycles}; expected 0 (hand "
                f"back to a human), a positive count, or -1 (retry until the "
                f"backlog is clean or the run is stopped)."
            )
        for kind, raw in self.commands.items():
            if not isinstance(raw, (str, dict)):
                raise ConfigError(
                    f"commands.{kind} is {type(raw).__name__}; expected a "
                    f'command string, or a map of language to command like '
                    f'{{".rs": "cargo test", ".js": "node --test"}}.'
                )
            if isinstance(raw, dict):
                for key, value in raw.items():
                    if _is_exemption(value):
                        continue
                    if not isinstance(value, str):
                        raise ConfigError(
                            f"commands.{kind}.{key} is {type(value).__name__}; "
                            f"expected a command string."
                        )
            for suffix, command in self.commands_for(kind).items():
                mismatch = _wrong_language(suffix, command)
                if mismatch:
                    raise ConfigError(
                        f"commands.{kind} runs {command!r} for {suffix} files, "
                        f"but that command runs {mismatch}. A command keyed to "
                        f"a language it cannot run fails every ticket in that "
                        f"language and reports it as the ticket's fault."
                    )

        if self.loop.bug_hypotheses < 1:
            raise ConfigError(
                f"loop.bugHypotheses is {self.loop.bug_hypotheses}; expected 1 "
                f"(park on the first hypothesis that cannot be reproduced) or "
                f"more."
            )
        if self.loop.executor_turns < 0:
            raise ConfigError(
                f"loop.executorTurns is {self.loop.executor_turns}; expected 0 "
                f"(the single-message prompt) or the number of prior attempts "
                f"to replay to the executor as conversation turns."
            )
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

    # ------------------------------------------------------------------
    # Verify commands
    # ------------------------------------------------------------------

    def commands_for(self, kind: str) -> dict[str, str]:
        """One verify step's commands, keyed by extension, plus `*`.

        A repository is not one language, and a single command per step says it
        is. Everything downstream inherited that: which language the tester
        writes in, what verification proves, and whether a bug in an unrun
        layer can be reproduced at all. One project shipped a green ticket over
        JavaScript that threw on its second line, because the suite was
        `cargo test` and nothing else was ever run.

        A plain string still means what it always did — every language, one
        command — so no existing config changes meaning by being read here.
        """
        raw = self.commands.get(kind, "")
        if isinstance(raw, str):
            return {ANY_LANGUAGE: raw.strip()} if raw.strip() else {}
        found: dict[str, str] = {}
        for key, value in (raw or {}).items():
            if _is_exemption(value):
                continue
            command = str(value or "").strip()
            if not command:
                continue
            for suffix in normalize_language(str(key)):
                found[suffix] = command
        return found

    def command_for(self, kind: str, path: str) -> str:
        """The command that verifies one file's language, or "" if none does.

        A catch-all answers for anything with no entry of its own, which is
        what makes a one-language project's single string keep working.
        """
        commands = self.commands_for(kind)
        suffix = Path(path).suffix.lower() if path else ""
        return commands.get(suffix) or commands.get(ANY_LANGUAGE, "")

    def exempt(self, kind: str, suffix: str) -> bool:
        """Whether this language is declared as one nothing needs to run.

        The third state, and it earns its place. A shell wrapper and a
        PowerShell build script have no behavior a unit test could assert, and
        the gate is meant to catch a language nobody thought about — not to
        stall a backlog over `build.sh`. Saying so in config is a decision on
        the record; leaving the key out is an oversight, and those are exactly
        what this feature exists to surface.
        """
        raw = self.commands.get(kind, "")
        if isinstance(raw, str):
            return False
        wanted = suffix.lower()
        for key, value in (raw or {}).items():
            if _is_exemption(value) and wanted in normalize_language(str(key)):
                return True
        return False

    def covers(self, kind: str, suffix: str) -> bool:
        """Whether this step has something that genuinely runs the extension.

        A catch-all counts — until it names a runner that cannot possibly run
        the language. A project whose only command is `cargo test` does not
        cover its JavaScript, and saying it does is how a ticket ships green
        over code nothing ran. That claim was true of a real run.
        """
        return bool(self.covering(kind, suffix)[0])

    def covering(self, kind: str, suffix: str) -> tuple[str, str]:
        """`(command, how)` for one extension: 'exact', 'catch-all', or ''.

        The empty answer is the interesting one — it is what a gate and a
        coverage report are both asking about. A catch-all that cannot run the
        language answers empty and says why in `how`.
        """
        if self.exempt(kind, suffix):
            return "", "declared as needing none"
        commands = self.commands_for(kind)
        suffix = suffix.lower()
        exact = commands.get(suffix)
        if exact:
            return exact, "exact"
        fallback = commands.get(ANY_LANGUAGE, "")
        if not fallback:
            return "", ""
        mismatch = _wrong_language(suffix, fallback)
        if mismatch:
            return "", f"runs {mismatch}"
        return fallback, "catch-all"

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
                "retryCycles": self.loop.retry_cycles,
                "respecOnRetry": self.loop.respec_on_retry,
                "respecCriteria": self.loop.respec_criteria,
                "reopenStaleDependents": self.loop.reopen_stale_dependents,
                "preflight": self.loop.preflight,
                "pollSeconds": self.loop.poll_seconds,
                "maxRuntimeSeconds": self.loop.max_runtime_seconds,
                "baselineVerify": self.loop.baseline_verify,
                "executorTurns": self.loop.executor_turns,
                "bugHypotheses": self.loop.bug_hypotheses,
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
