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
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .budget import RateLimitPolicy
# `neverDelegate`'s matcher, reused for a workspace's `excludes` so the two
# spellings of "these paths, please" behave identically. `patch` imports
# nothing from this package, so this cannot cycle.
from .patch import matches_any
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

# Languages whose test command does **not** type-check the project, and the
# checker their ecosystem settled on.
#
# The distinction this draws is the whole point of it. `cargo test`, `go test`
# and `gradle test` compile the code before running any of it, so a project
# with no `typecheck` entry for Rust, Go or Java is not missing anything — the
# test command already did it, and asking for a second one would be noise.
#
# TypeScript and Python are different: their test commands load the modules the
# tests reach and nothing else. A file no test imports is never parsed by
# anything, which is exactly how 4,000 lines with sixteen imports of modules
# that do not exist passed a run. `tsc --noEmit` would have found every one of
# them in about two seconds with no model involved.
#
# Deliberately short. A language belongs here only when its ecosystem has one
# near-universal answer; a list of plausible checkers for every language would
# turn a real gap into a wall of suggestions.
TYPECHECKERS: dict[str, tuple[str, ...]] = {
    ".ts": ("tsc --noEmit",),
    ".tsx": ("tsc --noEmit",),
    ".mts": ("tsc --noEmit",),
    ".cts": ("tsc --noEmit",),
    ".py": ("mypy .", "pyright"),
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


# ----------------------------------------------------------------------
# Verify commands, over one `commands` block
# ----------------------------------------------------------------------
#
# These were methods on `Config` and are now functions, because a `commands`
# block is no longer a property of the repository — it is a property of a
# workspace, and there can be several. The behaviour is unchanged; `Config`
# and `Workspace` both call through here so the two can never drift.


def _commands_for(commands: dict[str, Any], kind: str) -> dict[str, str]:
    """One verify step's commands, keyed by extension, plus `*`.

    A plain string means what it always did — every language, one command — so
    no config changes meaning by being read here.
    """
    raw = commands.get(kind, "")
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


def _command_for(commands: dict[str, Any], kind: str, path: str) -> str:
    """The command that verifies one file's language, or "" if none does."""
    resolved = _commands_for(commands, kind)
    suffix = Path(path).suffix.lower() if path else ""
    return resolved.get(suffix) or resolved.get(ANY_LANGUAGE, "")


def _exempt(commands: dict[str, Any], kind: str, suffix: str) -> bool:
    """Whether this language is declared as one nothing needs to run."""
    raw = commands.get(kind, "")
    if isinstance(raw, str):
        return False
    wanted = suffix.lower()
    for key, value in (raw or {}).items():
        if _is_exemption(value) and wanted in normalize_language(str(key)):
            return True
    return False


def _covering(commands: dict[str, Any], kind: str, suffix: str) -> tuple[str, str]:
    """`(command, how)` for one extension: 'exact', 'catch-all', or ''."""
    if _exempt(commands, kind, suffix):
        return "", "declared as needing none"
    resolved = _commands_for(commands, kind)
    suffix = suffix.lower()
    exact = resolved.get(suffix)
    if exact:
        return exact, "exact"
    fallback = resolved.get(ANY_LANGUAGE, "")
    if not fallback:
        return "", ""
    mismatch = _wrong_language(suffix, fallback)
    if mismatch:
        return "", f"runs {mismatch}"
    return fallback, "catch-all"


def _validate_commands(commands: dict[str, Any], where: str) -> None:
    """Refuse a `commands` block that cannot mean what it says.

    `where` names the block in the error, so a message about a workspace's
    commands says which workspace rather than pointing at a key that appears
    several times in the file.
    """
    for kind, raw in commands.items():
        if not isinstance(raw, (str, dict)):
            raise ConfigError(
                f"{where}.{kind} is {type(raw).__name__}; expected a "
                f'command string, or a map of language to command like '
                f'{{".rs": "cargo test", ".js": "node --test"}}.'
            )
        if isinstance(raw, dict):
            for key, value in raw.items():
                if _is_exemption(value):
                    continue
                if not isinstance(value, str):
                    raise ConfigError(
                        f"{where}.{kind}.{key} is {type(value).__name__}; "
                        f"expected a command string."
                    )
        for suffix, command in _commands_for(commands, kind).items():
            mismatch = _wrong_language(suffix, command)
            if mismatch:
                raise ConfigError(
                    f"{where}.{kind} runs {command!r} for {suffix} files, "
                    f"but that command runs {mismatch}. A command keyed to "
                    f"a language it cannot run fails every ticket in that "
                    f"language and reports it as the ticket's fault."
                )


# The root of a repository that declares no workspaces of its own.
REPO_ROOT = "."


def normalize_workspace_root(raw: str) -> str:
    """A workspace root as a repo-relative posix path, or `.` for the root.

    Normalised on the way in so `"./tools/path-forge"`, `"tools/path-forge/"`
    and `"tools\\path-forge"` are one workspace rather than three that each
    claim the same files.
    """
    text = str(raw or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    text = text.rstrip("/")
    return text or REPO_ROOT


@dataclass
class Workspace:
    """One build inside the repository: a root, its commands, what it disowns.

    Not a language and not a module. A workspace is a directory that owns a
    manifest and a set of commands that only work when run from inside it —
    `npm test` under a `package.json`, `cargo test` under a `Cargo.toml`. The
    distinction the loop needs is that its commands run with `cwd` set here,
    and that the files under it are verified by these commands and no others.

    A repository declaring no workspaces has exactly one, at `.`, holding the
    top-level `commands`. Every code path then resolves to what it did before
    workspaces existed, which is what makes this safe to land in one commit.
    """

    root: str = REPO_ROOT
    commands: dict[str, Any] = field(default_factory=dict)
    # Paths this workspace does not own despite sitting beneath it. A child
    # workspace's root is excluded implicitly; this is for the rest. Without
    # it a root workspace's `tests/` glob swallows a subproject's `tests/`,
    # which is how one repository's gdUnit4 command came to "collect" — and
    # silently ignore — 4,000 lines of TypeScript.
    excludes: list[str] = field(default_factory=list)

    @property
    def is_repo_root(self) -> bool:
        return self.root == REPO_ROOT

    @property
    def name(self) -> str:
        """Short label for a step name. `.` is the repository itself."""
        return self.root.rsplit("/", 1)[-1] if not self.is_repo_root else REPO_ROOT

    def path(self, root: Path) -> Path:
        """Absolute path of this workspace's root."""
        return root if self.is_repo_root else root / self.root

    @property
    def prefix(self) -> str:
        """What to prepend to a path this workspace reports about itself.

        Empty at the repository root, where a workspace-relative path already
        is a repo-relative one.
        """
        return "" if self.is_repo_root else f"{self.root}/"

    def contains(self, path: str) -> bool:
        """Whether `path` — repo-relative — sits inside this workspace's root.

        Says nothing about ownership: an ancestor workspace contains a child's
        files too. `Config.workspace_for` resolves that by longest prefix.
        """
        candidate = str(path or "").replace("\\", "/").lstrip("./")
        if self.is_repo_root:
            return bool(candidate)
        return candidate == self.root or candidate.startswith(f"{self.root}/")

    # -- verify commands, scoped to this workspace ---------------------

    def commands_for(self, kind: str) -> dict[str, str]:
        return _commands_for(self.commands, kind)

    def command_for(self, kind: str, path: str) -> str:
        return _command_for(self.commands, kind, path)

    def exempt(self, kind: str, suffix: str) -> bool:
        return _exempt(self.commands, kind, suffix)

    def covering(self, kind: str, suffix: str) -> tuple[str, str]:
        return _covering(self.commands, kind, suffix)

    def covers(self, kind: str, suffix: str) -> bool:
        return bool(self.covering(kind, suffix)[0])

    def unchecked(self, suffix: str) -> tuple[str, ...]:
        """The type checker this build has no command for, or `()`.

        Answers only for the languages in `TYPECHECKERS` — the ones whose test
        command does not compile the project, so that a missing entry is a hole
        rather than a redundancy. An exemption silences it: a project that has
        decided not to type-check its Python has decided, and the difference
        between a decision and an oversight is the one thing worth reporting.
        """
        suffix = suffix.lower()
        if suffix not in TYPECHECKERS:
            return ()
        if self.exempt("typecheck", suffix) or self.covers("typecheck", suffix):
            return ()
        # A language with no test command either has a bigger problem, already
        # reported, or is one nothing here runs at all. Saying "its test
        # command does not check the whole project" about a build with no test
        # command is a sentence that does not parse.
        if not self.covers("test", suffix):
            return ()
        return TYPECHECKERS[suffix]


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
    # Prove, rather than infer, that each build's test command reads each
    # language *this backlog will write*. Writes an unparseable file, runs the
    # command over it, requires the command to go red *and* to name the file,
    # deletes it.
    #
    # Scoped to the backlog rather than the tree because the tree is full of
    # languages nobody is asking about: a Godot repository with one Python
    # helper script has `.py` present, nothing that runs it, and no ticket that
    # cares. Blocking on that is `build.sh` all over again.
    #
    # Inference was the alternative and it is what shipped the defect: coverage
    # was read off the text of a command against a table of known runners, and a
    # runner the table has never heard of answers "covered" for every language
    # in the repository. One gdUnit4 launcher reported itself as the test
    # command for 4,000 lines of TypeScript and exited 0 fifteen times.
    #
    # Costs one command invocation per language per build, once per run, and
    # ends the run rather than the ticket when a build fails it. Off is for a
    # suite slow enough that paying it at startup is worse than finding out
    # later, or a language whose canary the runner legitimately ignores —
    # `forge toolchain --language X --skip` is the narrower way to say the
    # second, and says it on the record.
    preflight_canary: bool = True
    # Run the verify commands once before the first ticket and refuse to start
    # on a tree that is already red.
    #
    # The per-ticket baseline excuses a failure that pre-dates a ticket, which
    # is what stops one abandoned file from failing an entire backlog. On a
    # repository that is red *before the run*, that amnesty applies to every
    # ticket at once: each one is verified against a build that cannot succeed,
    # each one is excused for it, and the backlog reports green over a project
    # that does not compile. `_unverifiable` does not catch this — the red is
    # in files no ticket owns, so there is no exhausted owner to point at — and
    # `_finish` only finds it after every ticket has been spent.
    #
    # Only a failure naming files is worth blocking on. A command that fails
    # with nothing to attribute — `pytest` exiting 5 on a repository with no
    # tests yet — is a greenfield run, not a broken one, and is reported rather
    # than gated. Turn this off for a repository whose red is known and is what
    # the backlog is there to fix.
    require_green_baseline: bool = True
    # Take a ticket's work back out of the tree when it gives up, keeping a
    # copy under `.hybridforge/abandoned/`.
    #
    # Nothing used to revert a failed ticket, on the grounds that a human may
    # want to salvage what it wrote. The cost was paid by everything after it:
    # the file stays broken, whole-project verification reports it to every
    # later ticket, and because it is outside their scope they are excused for
    # it — so they pass having compiled nothing. One run ended with two files
    # importing a package that never existed and five tickets green against a
    # tree where `compileJava` failed on the first file it read.
    #
    # Quarantine keeps both: the tree returns to the state the ticket inherited
    # and the abandoned work is on disk to read. Turn it off to have a failed
    # ticket's files left in place.
    quarantine_failed: bool = True
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


def _workspace_from(block: Any, index: int) -> Workspace:
    """One `workspaces[]` entry, or a ConfigError naming which one is wrong."""
    where = f"workspaces[{index}]"
    if not isinstance(block, dict):
        raise ConfigError(
            f"{where} is {type(block).__name__}; expected an object with a "
            f'"root" and a "commands".'
        )
    raw_root = block.get("root", REPO_ROOT)
    if not isinstance(raw_root, str):
        raise ConfigError(f"{where}.root is {type(raw_root).__name__}; expected a path.")
    root = normalize_workspace_root(raw_root)
    if root.startswith("/") or root.startswith("..") or re.match(r"^[A-Za-z]:", root):
        # An absolute or escaping root would have the loop run a command
        # outside the repository it is verifying, and attribute the result to
        # a ticket in it.
        raise ConfigError(
            f"{where}.root is {raw_root!r}; expected a path inside the "
            f'repository, like "tools/path-forge" or ".".'
        )
    commands = block.get("commands", {}) or {}
    if not isinstance(commands, dict):
        raise ConfigError(
            f"{where}.commands is {type(commands).__name__}; expected an object."
        )
    excludes = block.get("excludes", []) or []
    if not isinstance(excludes, list) or any(not isinstance(e, str) for e in excludes):
        raise ConfigError(f"{where}.excludes must be a list of path patterns.")
    return Workspace(root=root, commands=commands, excludes=list(excludes))


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
    # What the config file declared, or `None` for a repository that declared
    # nothing. Read through `workspaces`, never directly.
    _declared_workspaces: list[Workspace] | None = None

    @property
    def workspaces(self) -> list[Workspace]:
        """The builds in this repository. Never empty.

        A repository that declares none has exactly one, at `.`, holding
        `commands` — and it is *derived* on each access rather than stored.
        Storing it aliased a dict, so `config.commands = {...}` left the
        workspace holding the block that was replaced, and the loop verified
        against commands nobody had configured any more. Deriving it means
        there is one copy of the truth and no way to update half of it.
        """
        if self._declared_workspaces is not None:
            return self._declared_workspaces
        return [Workspace(root=REPO_ROOT, commands=self.commands)]

    @property
    def _explicit_workspaces(self) -> bool:
        return self._declared_workspaces is not None

    def declare_workspaces(self, workspaces: list[Workspace]) -> None:
        """Say this repository holds these builds, and check that it can.

        The way a caller that is not `load` — the wizard, a test — declares
        them, so nobody has to reach past the property to do it. Validated on
        the spot: a root that resolves to nothing owns no files, and a config
        written with one looks entirely reasonable while a whole build goes
        unverified.

        Passing an empty list clears the declaration, which puts the
        repository back to the implicit single workspace over `commands`.
        """
        if not workspaces:
            self._declared_workspaces = None
            return
        self._declared_workspaces = list(workspaces)
        if self.commands:
            # Same refusal `load` makes, for the same reason: the top-level
            # block would be read by nothing and would look configured.
            raise ConfigError(
                "cannot declare workspaces while a top-level `commands` block "
                "is set — move it into the workspace whose root is '.'."
            )
        self._validate_workspaces()

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

        declared = data.get("workspaces")
        if declared is not None:
            if not isinstance(declared, list):
                raise ConfigError(
                    f"`workspaces` is {type(declared).__name__}; expected a list "
                    f'of {{"root": "...", "commands": {{...}}}} objects.'
                )
            if config.commands:
                # Both spellings present is not a merge, it is a question with
                # no answer: the top-level block would be read by nothing and
                # would look configured. The repository root is a workspace
                # like any other and should say so.
                raise ConfigError(
                    "config declares both `workspaces` and a top-level "
                    "`commands`. Move the top-level block into the workspace "
                    'whose root is ".", or delete `workspaces` to keep using '
                    "it as-is."
                )
            if not declared:
                raise ConfigError(
                    "`workspaces` is empty. Delete the key to use the "
                    "repository root, or declare at least one build."
                )
            config._declared_workspaces = [
                _workspace_from(block, index) for index, block in enumerate(declared)
            ]

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
            preflight_canary=bool(loop.get("preflightCanary", True)),
            require_green_baseline=bool(loop.get("requireGreenBaseline", True)),
            quarantine_failed=bool(loop.get("quarantineFailed", True)),
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
        _validate_commands(self.commands, "commands")
        self._validate_workspaces()

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

    def _validate_workspaces(self) -> None:
        """Refuse a workspace layout the loop would silently misread.

        A root that does not exist is the dangerous one. It resolves nothing,
        every file falls through to whichever workspace does match, and the
        config looks entirely reasonable while a whole build goes unverified —
        which is the failure workspaces exist to prevent, reintroduced by a
        typo.
        """
        seen: dict[str, int] = {}
        for index, workspace in enumerate(self.workspaces):
            if workspace.root in seen:
                raise ConfigError(
                    f"workspaces[{index}].root is {workspace.root!r}, which "
                    f"workspaces[{seen[workspace.root]}] already claims. Two "
                    f"workspaces cannot own the same files."
                )
            seen[workspace.root] = index
            if self._explicit_workspaces:
                _validate_commands(workspace.commands, f"workspaces[{index}].commands")
                path = workspace.path(self.root)
                if not path.is_dir():
                    raise ConfigError(
                        f"workspaces[{index}].root is {workspace.root!r}, which "
                        f"is not a directory in this repository. A root that "
                        f"resolves to nothing owns no files, so its build is "
                        f"never verified and nothing says so."
                    )

    # ------------------------------------------------------------------
    # Workspace resolution
    # ------------------------------------------------------------------

    def workspace_for(self, path: str) -> Workspace | None:
        """The workspace owning one repo-relative path, longest root first.

        `None` means no declared build claims the file. That is only reachable
        when workspaces are declared explicitly and none of them covers it —
        the implicit root workspace claims everything, so a repository that
        never heard of this feature never sees it.

        The empty answer is the point of the feature. Under the old model an
        unclaimed file was absorbed by whatever catch-all was configured, and
        absorption reads as coverage: a Godot launcher reported itself as the
        test command for 4,000 lines of TypeScript it could not see.
        """
        candidate = str(path or "").replace("\\", "/").lstrip("./")
        if not candidate:
            return None
        owner: Workspace | None = None
        for workspace in self.workspaces:
            if not workspace.contains(candidate):
                continue
            if workspace.excludes and matches_any(candidate, list(workspace.excludes)):
                continue
            # A child workspace's root is excluded from its ancestors
            # implicitly, which longest-prefix already expresses.
            if owner is None or len(workspace.root) > len(owner.root):
                owner = workspace
        return owner

    def workspaces_for(self, paths: Sequence[str]) -> tuple[list[Workspace], list[str]]:
        """`(workspaces these paths touch, paths no workspace owns)`.

        Both halves are answers a caller needs. A ticket whose files land in
        two workspaces is a scoping error; a ticket with unowned files is one
        nothing can verify. Phase 1 reports; the gates that refuse on either
        are phase 4.
        """
        found: list[Workspace] = []
        unowned: list[str] = []
        for path in paths:
            if any(character in str(path) for character in "*?["):
                # A glob in `allowed_files` names no particular file, so it
                # cannot be resolved to a build. Left to the caller.
                continue
            workspace = self.workspace_for(str(path))
            if workspace is None:
                unowned.append(str(path))
            elif workspace not in found:
                found.append(workspace)
        return found, unowned

    def workspace_for_ticket(self, paths: Sequence[str]) -> Workspace:
        """The single workspace a ticket's writable files belong to.

        Falls back to the repository root when the files resolve to nothing —
        a ticket writing only globs, or a backlog whose scope has not been
        checked yet. Phase 4 refuses those at ingest instead; until then the
        old behaviour (verify from the repository root) is the safe default,
        because it is what every run before workspaces did.
        """
        found, _ = self.workspaces_for(paths)
        if len(found) == 1:
            return found[0]
        return self.root_workspace

    @property
    def root_workspace(self) -> Workspace:
        """The workspace at `.`, or the first declared one if there is none.

        A repository whose only build lives in a subdirectory has no workspace
        at `.`, and the run-level sweeps still need somewhere to stand.
        """
        for workspace in self.workspaces:
            if workspace.is_repo_root:
                return workspace
        return self.workspaces[0]

    # ------------------------------------------------------------------
    # Verify commands
    # ------------------------------------------------------------------
    #
    # These answer across every workspace, which is what a caller asking "does
    # this project test anything at all" means. A caller holding a *path* has
    # a build to ask about and should ask that workspace directly —
    # `config.workspace_for(path).covers(...)`. The gates move onto that in
    # phase 4; today a single-workspace repository cannot tell the difference,
    # which is the property that makes phase 1 a no-op for existing configs.

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

        Across workspaces the maps are merged, first declaration winning, so
        "is anything configured for this step" stays answerable without a path.
        """
        merged: dict[str, str] = {}
        for workspace in self.workspaces:
            for suffix, command in workspace.commands_for(kind).items():
                merged.setdefault(suffix, command)
        return merged

    def command_for(self, kind: str, path: str) -> str:
        """The command that verifies one file's language, or "" if none does.

        Resolved through the file's own workspace, so a path in a subproject
        gets that subproject's command rather than the repository root's.
        """
        workspace = self.workspace_for(path)
        if workspace is None:
            return ""
        return workspace.command_for(kind, path)

    def exempt(self, kind: str, suffix: str) -> bool:
        """Whether this language is declared as one nothing needs to run.

        The third state, and it earns its place. A shell wrapper and a
        PowerShell build script have no behavior a unit test could assert, and
        the gate is meant to catch a language nobody thought about — not to
        stall a backlog over `build.sh`. Saying so in config is a decision on
        the record; leaving the key out is an oversight, and those are exactly
        what this feature exists to surface.

        Exempt anywhere is exempt: one workspace declaring `.sh` as needing no
        runner is a decision on the record about shell scripts.
        """
        return any(workspace.exempt(kind, suffix) for workspace in self.workspaces)

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

        The best answer any workspace gives, because the question has no path
        in it and so no single build to ask. `exact` beats `catch-all` beats
        nothing.
        """
        if self.exempt(kind, suffix):
            return "", "declared as needing none"
        best = ("", "")
        for workspace in self.workspaces:
            command, how = workspace.covering(kind, suffix)
            if how == "exact":
                return command, how
            if command and not best[0]:
                best = (command, how)
            elif not best[0] and not best[1] and how:
                best = ("", how)
        return best

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
                "preflightCanary": self.loop.preflight_canary,
                "requireGreenBaseline": self.loop.require_green_baseline,
                "quarantineFailed": self.loop.quarantine_failed,
                "pollSeconds": self.loop.poll_seconds,
                "maxRuntimeSeconds": self.loop.max_runtime_seconds,
                "baselineVerify": self.loop.baseline_verify,
                "executorTurns": self.loop.executor_turns,
                "bugHypotheses": self.loop.bug_hypotheses,
            },
            "ui": {"host": self.ui.host, "port": self.ui.port, "enabled": self.ui.enabled},
        }
        if self._explicit_workspaces:
            # Written only when the file said so. A repository that declares
            # no workspaces round-trips to the file it had, without acquiring
            # a key describing a feature it does not use.
            payload.pop("commands", None)
            payload["workspaces"] = [
                {
                    "root": workspace.root,
                    "commands": workspace.commands,
                    **({"excludes": workspace.excludes} if workspace.excludes else {}),
                }
                for workspace in self.workspaces
            ]
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
