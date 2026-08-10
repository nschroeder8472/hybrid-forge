"""Interactive `forge init` — connection strings, asked once.

The thing that actually stops people using this pipeline is not the loop, it is
the twenty minutes of reading before the first run: which adapter, which URL,
which of four roles gets which model. So this asks, in order, and **probes every
answer while the user is still sitting there** — a wrong endpoint discovered now
costs one retyped line, and the same wrong endpoint discovered by `forge go` at
2am costs the run.

Three properties it must keep:

**It never hangs.** No TTY means no questions: piped, redirected, or run from a
CI step, it takes the defaults and says so. A setup command that blocks forever
waiting on stdin nobody is watching is worse than one that guesses.

**It never invents an answer it could check.** Endpoints are probed, `claude` is
looked for on PATH, build commands are inferred from the files actually in the
repo. Where it guesses, the guess is the pre-filled default the user can see and
override.

**It writes nothing until the end.** Every question is answered, the result is
shown as the JSON it will become, and only then does anything land on disk.
Ctrl-C at any point leaves the repo exactly as it was found.
"""

from __future__ import annotations

import json
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import toolchain
from .config import CONFIG_DIR, CONFIG_FILE, ROLES, Config
from .memory import MemoryClient
from .profile import Profile
from .providers import build_provider
from .tokens import format_tokens

# How many times a failing probe may be retyped before the wizard moves on.
# Bounded so a wrong endpoint cannot trap someone in a question they cannot
# answer — `forge doctor` retests, and a config on disk is easier to fix than
# a loop with no exit.
MAX_RETRIES = 3


class Aborted(Exception):
    """The user pressed Ctrl-C or Ctrl-D. Nothing has been written."""


# ----------------------------------------------------------------------
# Prompting primitives
# ----------------------------------------------------------------------


def interactive() -> bool:
    """True when there is a human on the other end of stdin.

    Both ends are checked: stdout matters because a wizard whose questions are
    being piped to a file is asking nobody.
    """
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


@dataclass
class Prompter:
    """Question-asking with a non-interactive mode that answers with defaults."""

    enabled: bool = True
    # Injected in tests so the whole flow can be driven without a terminal.
    reader: Callable[[str], str] | None = None

    def _read(self, text: str) -> str:
        reader = self.reader or input
        try:
            return reader(text)
        except (EOFError, KeyboardInterrupt) as exc:
            raise Aborted() from exc

    def ask(self, question: str, default: str = "") -> str:
        if not self.enabled:
            return default
        suffix = f" [{default}]" if default else ""
        answer = self._read(f"{question}{suffix}: ").strip()
        return answer or default

    def confirm(self, question: str, default: bool = True) -> bool:
        if not self.enabled:
            return default
        suffix = "[Y/n]" if default else "[y/N]"
        while True:
            answer = self._read(f"{question} {suffix}: ").strip().lower()
            if not answer:
                return default
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no"):
                return False
            say("  answer y or n.")

    def choose(self, question: str, options: list[tuple[str, str]], default: int = 0) -> str:
        """Pick one of `options` as (key, description). Returns the key."""
        if not self.enabled:
            return options[default][0]
        say(f"\n{question}")
        for index, (_, description) in enumerate(options, start=1):
            marker = "*" if index - 1 == default else " "
            say(f"  {marker} {index}. {description}")
        while True:
            answer = self._read(f"choice [{default + 1}]: ").strip()
            if not answer:
                return options[default][0]
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                return options[int(answer) - 1][0]
            say(f"  enter a number from 1 to {len(options)}.")


def say(text: str = "") -> None:
    print(text)


def heading(step: int, total: int, title: str) -> None:
    say(f"\n\033[1m{step}/{total}  {title}\033[0m")


# ----------------------------------------------------------------------
# Probing
# ----------------------------------------------------------------------


def check_url(url: str) -> str:
    """Complaint about an obviously malformed URL, or "" when it looks usable.

    Catches the mistake everyone makes — pasting `forge-host:8765/mcp` without
    a scheme — before it reaches urllib, whose `unknown url type` is true but
    unhelpful.
    """
    url = url.strip()
    if not url:
        return "empty"
    if "://" not in url:
        return f"missing a scheme — try http://{url}"
    if not url.startswith(("http://", "https://")):
        scheme = url.split("://", 1)[0]
        return f"{scheme}:// is not supported here; use http:// or https://"
    remainder = url.split("://", 1)[1]
    if not remainder or remainder.startswith("/"):
        return "no host in the URL"
    return ""


def probe_model(name: str, block: dict[str, Any]) -> tuple[bool, str]:
    """Send one real completion. Returns (ok, one-line report).

    A real call rather than a socket check on purpose: a reachable port that
    401s, or an endpoint serving a model name that does not exist, both look
    fine to anything cheaper and fail on the first ticket.

    Never raises. A probe that throws would take every answer the user has
    already typed with it, which is a worse outcome than any endpoint being
    down — so anything unexpected is reported as a failed probe.
    """
    complaint = check_url(str(block.get("baseUrl", ""))) if block.get("baseUrl") else ""
    if complaint:
        return False, complaint

    try:
        provider = build_provider(name, block)
    except Exception as exc:  # noqa: BLE001 - a config typo is a probe failure
        return False, f"config rejected: {exc}"

    try:
        report = provider.health()
    except Exception as exc:  # noqa: BLE001 - health() normalizes ProviderError only
        return False, f"{type(exc).__name__}: {exc}"

    if report.startswith("FAIL"):
        # The trailing `error=…` is the part worth showing; the rest restates
        # what the user just typed.
        _, _, detail = report.partition("error=")
        return False, detail or report

    try:
        caps = provider.capabilities()
        return True, f"answered — context {format_tokens(caps.context_window)}"
    except Exception:  # noqa: BLE001 - it answered; capability detail is a bonus
        return True, "answered"


def memory_block(answer: str, room: str = "") -> dict[str, Any]:
    """Read one answer as either transport.

    A URL is the only thing that can start with a scheme, so the two are
    distinguishable without asking a second question. Anything else is argv for
    a stdio server — which is what MemPalace ships, and the more common answer.
    """
    answer = answer.strip()
    if answer.startswith(("http://", "https://")) or "://" in answer:
        return {"url": answer, "room": room}
    return {"command": answer.split(), "room": room}


def probe_memory(answer: str, room: str) -> tuple[bool, str]:
    """Connect and list tools. Never raises, for the same reason as above."""
    block = memory_block(answer, room)
    if block.get("url"):
        complaint = check_url(block["url"])
        if complaint:
            return False, complaint

    try:
        client = MemoryClient.from_config(block, room=room)
        if client is None:
            return False, "no url or command"
        report = client.describe()
    except Exception as exc:  # noqa: BLE001 - describe() normalizes its own errors only
        return False, f"{type(exc).__name__}: {exc}"

    if report.startswith("FAIL"):
        _, _, detail = report.partition("error=")
        return False, detail or report
    return True, report[len("ok memory ") :] if report.startswith("ok memory ") else report


def _report(ok: bool, detail: str) -> None:
    say(f"  \033[32mok\033[0m  {detail}" if ok else f"  \033[31mFAIL\033[0m  {detail}")


# ----------------------------------------------------------------------
# Repo inspection
# ----------------------------------------------------------------------


def detect_commands(
    root: Path, models: dict[str, dict[str, Any]], roles: dict[str, str]
) -> toolchain.Detection:
    """Ask the planner model what this repo's verify commands are.

    Uses the planner because it is the reading-and-judgment role and is usually
    the strongest model configured. The repo root is passed through to a
    `claude-cli` planner as its `cwd` so the session is rooted in the project —
    the evidence still travels in the prompt, so this works the same for a
    provider with no filesystem at all.
    """
    name = roles.get("planner") or roles.get("reviewer") or ""
    block = dict(models.get(name) or {})
    if not block:
        return toolchain.Detection(error="no planner model configured yet")

    if block.get("kind") in ("claude-cli", "claude-code") and not block.get("cwd"):
        # Detection-only: never persisted, because cwd is a property of this
        # repository and the config it lands in may be copied to another.
        block["cwd"] = str(root)

    try:
        provider = build_provider(name, block)
    except Exception as exc:  # noqa: BLE001 - a bad block is a failed detection
        return toolchain.Detection(error=f"could not build the planner model: {exc}")

    return toolchain.detect(root, provider)


# ----------------------------------------------------------------------
# The flow
# ----------------------------------------------------------------------


@dataclass
class Answers:
    models: dict[str, dict[str, Any]] = field(default_factory=dict)
    roles: dict[str, str] = field(default_factory=dict)
    commands: dict[str, str] = field(default_factory=dict)
    memory: dict[str, Any] = field(default_factory=dict)
    room: str = ""
    never_delegate: list[str] = field(default_factory=list)


TOTAL_STEPS = 5


def run(root: Path, profile: Profile, prompter: Prompter) -> tuple[Config, Profile] | None:
    """Ask everything, probe as we go, return (config, profile) or None if declined.

    Nothing is written here. The caller decides, so an abort at the last
    question leaves the repository untouched.
    """
    answers = Answers()

    say(f"\n\033[1mHybrid Forge setup\033[0m — {root.name}")
    if not profile.is_empty:
        say(f"reusing saved settings from {profile.path}")
        say(f"  {profile.describe()}")
    if not prompter.enabled:
        say("no terminal attached — taking defaults for every question.")

    _ask_executor(answers, profile, prompter)
    _ask_judgment_roles(answers, profile, prompter)
    _ask_memory(answers, profile, prompter)
    _ask_repo(answers, root, prompter)

    config = Config(
        root=root,
        room=answers.room,
        models=answers.models,
        roles=answers.roles,
        commands=answers.commands,
        never_delegate=answers.never_delegate,
        memory=answers.memory,
    )
    config.ui.port = profile.ui_port

    heading(5, TOTAL_STEPS, "Review")
    say(f"\nThis will write {root / CONFIG_DIR / CONFIG_FILE}:\n")
    say(_preview(config))

    if not prompter.confirm("\nWrite it", default=True):
        return None

    updated = Profile(
        models=answers.models,
        roles=answers.roles,
        memory=answers.memory,
        ui_port=config.ui.port,
        path=profile.path,
    )
    return config, updated


def _ask_executor(answers: Answers, profile: Profile, prompter: Prompter) -> None:
    heading(1, TOTAL_STEPS, "Executor — the model that writes the code")
    say("Any OpenAI-compatible server: Ollama, vLLM, LM Studio, llama.cpp, LiteLLM,")
    say("OpenRouter, DeepSeek, or OpenAI itself.")

    previous = profile.models.get("local", {})
    block: dict[str, Any] = {}

    # A loop rather than recursion: someone fixing a typo three times should
    # not be growing the stack, and re-printing the section blurb on every
    # retry buries the error message they are trying to read.
    for attempt in range(MAX_RETRIES):
        base_url = prompter.ask(
            "\nBase URL", previous.get("baseUrl", "http://localhost:11434/v1")
        )
        model = prompter.ask("Model name", previous.get("model", "qwen3.6:35b-a3b"))
        key_env = prompter.ask(
            "Env var holding the API key, if this endpoint needs one",
            previous.get("apiKeyEnv", ""),
        )

        block = {"kind": "openai", "baseUrl": base_url, "model": model}
        if key_env:
            block["apiKeyEnv"] = key_env
        if previous.get("contextWindow"):
            block["contextWindow"] = previous["contextWindow"]

        say("\nprobing…")
        ok, detail = probe_model("local", block)
        _report(ok, detail)
        if ok or not prompter.enabled:
            break

        # Retyping the same value is the common case, so the failed answer
        # becomes the next prompt's default.
        previous = dict(block)
        if attempt == MAX_RETRIES - 1:
            say("\nStill failing. Continuing anyway — `forge doctor` will retest.")
            break
        say("\nThe executor is the one endpoint nothing else can cover for.")
        if not prompter.confirm("Try again", default=True):
            say("Continuing with an unreachable executor — `forge doctor` will retest.")
            break

    answers.models["local"] = block
    for role in ROLES:
        answers.roles[role] = "local"


def _ask_judgment_roles(answers: Answers, profile: Profile, prompter: Prompter) -> None:
    heading(2, TOTAL_STEPS, "Planner & reviewer — the judgment roles")
    say("The review step is what keeps a cheap executor honest, so it should not")
    say("be the executor. A model reviewing its own diff accepts it.")

    has_claude = shutil.which("claude") is not None
    options = [
        ("claude-cli", "Claude Code CLI — uses the subscription you already sign into"
                       + ("" if has_claude else "  (not found on PATH)")),
        ("anthropic", "Anthropic API — needs ANTHROPIC_API_KEY in the environment"),
        ("gemini", "Google Gemini — needs GEMINI_API_KEY in the environment"),
        ("local", "Same model as the executor — no second endpoint, weaker review"),
    ]
    # Default to what the profile already chose, else the CLI when it is
    # installed, else the API. Never silently default to self-review.
    previous_kind = profile.models.get("claude", {}).get("kind", "")
    default = 0 if has_claude else 1
    for index, (key, _) in enumerate(options):
        if key == previous_kind:
            default = index
            break

    previous = profile.models.get("claude", {})
    block: dict[str, Any] = {}

    for attempt in range(MAX_RETRIES):
        choice = prompter.choose("Who plans and reviews?", options, default=default)

        if choice == "local":
            say("\n  Note: executor and reviewer are the same model. Review will be weak.")
            answers.roles.update({"planner": "local", "reviewer": "local"})
            return

        if choice == "claude-cli":
            block = {"kind": "claude-cli"}
            model = prompter.ask("\nModel (blank = whatever the CLI defaults to)",
                                 previous.get("model", "opus"))
            if model:
                block["model"] = model
        elif choice == "anthropic":
            block = {
                "kind": "anthropic",
                "model": prompter.ask("\nModel", previous.get("model", "claude-opus-5")),
                "apiKeyEnv": prompter.ask("Env var holding the key",
                                          previous.get("apiKeyEnv", "ANTHROPIC_API_KEY")),
            }
        else:
            block = {
                "kind": "gemini",
                "model": prompter.ask("\nModel", previous.get("model", "gemini-2.5-pro")),
                "apiKeyEnv": prompter.ask("Env var holding the key",
                                          previous.get("apiKeyEnv", "GEMINI_API_KEY")),
            }

        say("\nprobing…")
        ok, detail = probe_model("claude", block)
        _report(ok, detail)
        if ok or not prompter.enabled:
            break

        previous = dict(block)
        if attempt == MAX_RETRIES - 1:
            say("\nStill failing. Continuing anyway — `forge doctor` will retest.")
            break
        if not prompter.confirm("Try a different answer", default=True):
            say("Continuing — `forge doctor` will retest.")
            break

    answers.models["claude"] = block
    answers.roles.update({"planner": "claude", "reviewer": "claude"})


def _ask_memory(answers: Answers, profile: Profile, prompter: Prompter) -> None:
    heading(3, TOTAL_STEPS, "Project memory (optional)")
    say("An MCP server holding decisions from past sessions. Without it the")
    say("executor sees only what each ticket carries. Blank to skip.")
    say("")
    say("A command runs the server here, as a child process — MemPalace speaks")
    say("stdio, so this is the usual answer:  mempalace-mcp")
    say("A URL reaches one already running elsewhere:  http://host:8765/mcp")

    previous = profile.memory or {}
    default_answer = previous.get("url", "") or " ".join(previous.get("command", []) or [])

    for attempt in range(MAX_RETRIES):
        answer = prompter.ask("\nMCP command or URL", default_answer)
        if not answer:
            answers.memory = {"url": "", "room": ""}
            say("  skipped — the loop will run without project context.")
            return

        answers.memory = memory_block(answer)

        say("\nprobing…")
        ok, detail = probe_memory(answer, room="")
        _report(ok, detail)
        if ok or not prompter.enabled:
            break

        default_answer = answer
        if attempt == MAX_RETRIES - 1:
            say("\nContinuing — memory failures never end a run, they only remove context.")
            break
        if not prompter.confirm("Try a different command or URL", default=True):
            say("Continuing — memory failures never end a run, they only remove context.")
            break

    # Write-back stays off. It mutates a store every future session reads with
    # no undo, and that is not a decision to make inside a setup flow the user
    # is trying to get through.
    if prompter.confirm(
        "\nLet the loop write durable decisions back to memory (starts in dry-run)",
        default=False,
    ):
        answers.memory["write"] = True
        answers.memory["dryRun"] = True
        say("  enabled with dryRun: true — it will log what it would write, and")
        say("  write nothing. Set memory.dryRun to false once you have watched it.")


def _ask_repo(answers: Answers, root: Path, prompter: Prompter) -> None:
    heading(4, TOTAL_STEPS, "This repository")

    answers.room = prompter.ask(
        "Memory room — scopes retrieval to this project", root.name
    )
    if answers.memory.get("url") or answers.memory.get("command"):
        # A palace that scopes by project under a different parameter name gets
        # the same answer. MemPalace calls it a wing and refuses to write
        # without one; a server with no such parameter drops it, so sending it
        # unconditionally costs nothing and saves a silent write failure on the
        # first ticket that produces a decision.
        answers.memory.setdefault("arguments", {})["wing"] = answers.room

    say("\nVerify commands. These run before any model reviews, and an empty one")
    say("is skipped — which is better than a command that does not work, because")
    say("a failing check re-delegates the ticket rather than reporting itself.")

    suggested = _detect_or_ask(answers, root, prompter)

    answers.commands = {
        "lint": prompter.ask("\nlint", suggested.get("lint", "")),
        "typecheck": prompter.ask("typecheck", suggested.get("typecheck", "")),
        "test": prompter.ask("test", suggested.get("test", "")),
    }

    say("\nPaths the executor must never touch, comma-separated.")
    say("Auth, migrations, and crypto belong here.")
    raw = prompter.ask("neverDelegate", "")
    answers.never_delegate = [p.strip() for p in raw.split(",") if p.strip()]


def _detect_or_ask(answers: Answers, root: Path, prompter: Prompter) -> dict[str, str]:
    """Offer model-backed detection, returning defaults for the command prompts.

    Declining, failing, or finding nothing all produce the same thing: empty
    defaults. Nothing here guesses from a file name — a repo whose commands are
    written down nowhere is a repo only its author can answer for.
    """
    blank = {"lint": "", "typecheck": "", "test": ""}

    if not prompter.confirm(
        "\nRead this repo's CI config and docs to find them", default=True
    ):
        return blank

    say("  reading…")
    detection = detect_commands(root, answers.models, answers.roles)

    if not detection.ok:
        say(f"  \033[33mskipped\033[0m  {detection.error}")
        say("  Enter them yourself, or leave blank and fill them in later.")
        return blank

    if not detection.found_anything:
        say(f"  read {len(detection.evidence)} file(s); none of them state a verify command.")
        return blank

    say(f"  read: {', '.join(detection.evidence[:6])}"
        + (f" (+{len(detection.evidence) - 6} more)" if len(detection.evidence) > 6 else ""))
    if detection.source:
        say(f"  {detection.source}")
    if detection.confidence != "high":
        # Said plainly rather than silently downgraded: a low-confidence answer
        # is still a better starting point than a blank, but it is the user's
        # call whether to trust it.
        say("  \033[33mlow confidence\033[0m — check these before accepting them.")

    return detection.commands


def _preview(config: Config) -> str:
    """Render the config the way it will land on disk, indented for reading."""
    payload = {
        "room": config.room,
        "models": config.models,
        "roles": config.roles,
        "commands": config.commands,
        "neverDelegate": config.never_delegate,
        "memory": config.memory,
    }
    return "\n".join("  " + line for line in json.dumps(payload, indent=2).splitlines())
