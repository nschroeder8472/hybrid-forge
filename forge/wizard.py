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
from collections.abc import Sequence
from typing import Any, Callable

from . import toolchain
from .config import CONFIG_DIR, CONFIG_FILE, REPO_ROOT, ROLES, Config, Workspace
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
    root: Path,
    models: dict[str, dict[str, Any]],
    roles: dict[str, str],
    where: Path | None = None,
    language: str = "",
) -> toolchain.Detection:
    """Ask the planner model what this repo's verify commands are.

    `where` reads a subdirectory instead: a build with its own manifest states
    its own commands in its own files, and the repository root's answer for
    them is the answer for a different project. The model still runs with the
    repository as its `cwd`, because that is where the checkout is.

    `language` narrows the question the same way, and for a sharper reason: a
    polyglot repository states a command for each of its languages, and the
    answer for the wrong one passes without running a line of the right one.

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

    return toolchain.detect(where or root, provider, language=language)


# ----------------------------------------------------------------------
# The flow
# ----------------------------------------------------------------------


@dataclass
class Answers:
    models: dict[str, dict[str, Any]] = field(default_factory=dict)
    roles: dict[str, str] = field(default_factory=dict)
    # One command per kind, or — when the build holds more than one language —
    # a map of extension to command per kind. Both spellings are what `config`
    # reads; see `_commands_for`.
    commands: dict[str, Any] = field(default_factory=dict)
    memory: dict[str, Any] = field(default_factory=dict)
    room: str = ""
    never_delegate: list[str] = field(default_factory=list)
    # Empty for the ordinary single-build repository, which is most of them.
    # Filled only when the tree holds more than one manifest and the person
    # says yes to configuring them separately — at which point `commands`
    # stays empty, because the two spellings are exclusive.
    workspaces: list[Workspace] = field(default_factory=list)


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
    if answers.workspaces:
        config.declare_workspaces(answers.workspaces)
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
    say("A local model, served by llama.cpp's router:")
    say("")
    say('  llama-server --models-preset <preset> --models-max 1')
    say("")
    say("The model name below is the router's id for a checkpoint, which is the")
    say("section name in the preset. `forge models` writes that preset from this")
    say("config once you are through here.")

    previous = profile.models.get("local", {})
    block: dict[str, Any] = {}

    # A loop rather than recursion: someone fixing a typo three times should
    # not be growing the stack, and re-printing the section blurb on every
    # retry buries the error message they are trying to read.
    for attempt in range(MAX_RETRIES):
        base_url = prompter.ask(
            "\nRouter URL", previous.get("baseUrl", "http://127.0.0.1:8080/v1")
        )
        model = prompter.ask(
            "Model id (the preset's section name)",
            previous.get("model", "qwen3.8"),
        )
        model_path = prompter.ask(
            "Path to its .gguf, if you want `forge models` to write the preset",
            previous.get("modelPath", ""),
        )

        block = {"kind": "llamacpp", "baseUrl": base_url, "model": model}
        if model_path:
            block["modelPath"] = model_path
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

    # Write-back mutates a store every future session reads, with no undo, so
    # it is asked rather than assumed. The default is yes because the answer it
    # enables is dry-run: nothing is written, and the log shows what would have
    # been. Defaulting to no produced a run with 262 memory retrievals and zero
    # writes, which rediscovered the same three project conventions eleven
    # times across two tickets that never exchanged a word.
    if prompter.confirm(
        "\nLet the loop write durable decisions back to memory (starts in dry-run)",
        default=True,
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

    builds = _ask_builds(root, prompter)
    if builds:
        answers.workspaces = [
            Workspace(
                root=build,
                commands=_ask_commands(answers, root, prompter, build, builds),
            )
            for build in builds
        ]
        answers.commands = {}
    else:
        answers.commands = _ask_commands(answers, root, prompter, REPO_ROOT)

    say("\nPaths the executor must never touch, comma-separated.")
    say("Auth, migrations, and crypto belong here.")
    raw = prompter.ask("neverDelegate", "")
    answers.never_delegate = [p.strip() for p in raw.split(",") if p.strip()]


def _ask_builds(root: Path, prompter: Prompter) -> list[str]:
    """Which directories to configure as separate builds. Empty means one.

    Discovery proposes; the person decides. A directory holding a
    `package.json` is strong evidence of a build and no evidence at all about
    whether they want it verified separately — a repository with one manifest
    at its root is the ordinary case and has to keep behaving like one, so the
    question is not even asked unless the tree holds two.

    Saying no is a real answer and the default is yes only because the
    alternative is what shipped the defect: one command set claiming authority
    over a subproject with a different toolchain, which reads as coverage for
    files it cannot see.
    """
    found = toolchain.discover_workspaces(root)
    if len(found) < 2:
        return []

    say("\nThis repository looks like more than one build:")
    for build in found:
        say(f"  {build}")
    say("Each has its own manifest, so each needs its own commands, run from")
    say("its own directory. Configured as one build, whichever command is set")
    say("at the root would claim the others' files and report as covering them.")

    if not prompter.confirm("\nConfigure them separately", default=True):
        say("  keeping one set of commands for the whole repository.")
        return []
    return found


def _ask_commands(
    answers: Answers,
    root: Path,
    prompter: Prompter,
    build: str,
    builds: Sequence[str] = (),
) -> dict[str, Any]:
    """One build's commands, detected from inside it and confirmed.

    Asked per language when the build holds more than one, because one command
    for all of them is the configuration this repository's own gates refuse:
    `forge doctor` reports it as a catch-all, the preflight canary blocks a run
    over a language the command does not read, and a ticket in that language is
    otherwise graded by reading its diff. Somebody answering these questions
    anyway is the cheapest place to close that, and the only one where the
    answer is not already costing a run.
    """
    where = root if build == REPO_ROOT else root / build
    if build != REPO_ROOT:
        say(f"\n\033[1m{build}\033[0m")

    languages = toolchain.census(root, build, others=builds)
    if len(languages) > 1:
        return _ask_per_language(answers, root, prompter, where, languages)

    suggested = _detect_or_ask(answers, root, prompter, where=where)
    commands: dict[str, Any] = {
        "lint": prompter.ask("\nlint", suggested.get("lint", "")),
        "typecheck": prompter.ask("typecheck", suggested.get("typecheck", "")),
        "test": prompter.ask("test", suggested.get("test", "")),
    }
    _say_format_note()
    commands["format"] = prompter.ask("format", suggested.get("format", ""))
    return commands


def _say_format_note() -> None:
    """Why `format` is asked apart from the three that verify.

    It is the one command here that is not a whole invocation: the loop appends
    the files it just wrote. It runs before verification and its own failure
    never parks a ticket, so a ticket whose only defect is whitespace costs
    nothing instead of an attempt. One run spent 117 of a ticket's 160 lint
    failures on exactly that. Blank is a supported answer and the safe one.
    """
    say("\nA formatter is optional, and pays for itself the first time a lint")
    say("failure is only whitespace. The loop appends the files it wrote, so")
    say("give the command *without* a target:  gdformat  /  prettier --write")
    say("/  ruff format  /  rustfmt  /  gofmt -w.  Blank for none.")


def _ask_per_language(
    answers: Answers,
    root: Path,
    prompter: Prompter,
    where: Path,
    languages: dict[str, int],
) -> dict[str, Any]:
    """The same four questions, once per language this build actually holds.

    Ranked by file count, so the language the build is mostly made of is
    answered first and a stray file last — which is also the order in which
    somebody gives up, and the one where giving up costs least.

    A blank answer is a real answer: that language has no runner, `doctor`
    reports it, and the canary refuses to start a run that would grade it by
    reading. `skip` says it needs none, which `config` stores as a declared
    exemption rather than a gap. Telling those two apart is the whole reason
    the question is asked per language.
    """
    ranked = sorted(languages.items(), key=lambda item: (-item[1], item[0]))
    say("\nThis build holds more than one language:")
    for suffix, count in ranked:
        say(f"  {suffix:<6} {count:>5} file(s)")
    say("Each is asked for separately. One command covering all of them reads")
    say("as coverage for files it never runs — the run then stops at the")
    say("preflight canary, or worse, passes a ticket nothing compiled.")
    say("Blank means this language is not verified; `skip` means it needs no")
    say("runner. Either can be changed later with `forge toolchain`.")

    detected = _detect_per_language(
        answers, root, prompter, where, [suffix for suffix, _ in ranked]
    )
    _say_format_note()

    commands: dict[str, dict[str, str]] = {}
    for suffix, _count in ranked:
        say(f"\n\033[1m{suffix}\033[0m")
        suggested = detected.get(suffix, {})
        for kind in ("lint", "typecheck", "test", "format"):
            answer = prompter.ask(f"{kind} [{suffix}]", suggested.get(kind, ""))
            if answer:
                commands.setdefault(kind, {})[suffix] = answer
    # A kind nobody answered for any language is left out rather than written
    # as an empty map: `commands_for` reads a missing key and an empty one the
    # same way, and the file should say only what was decided.
    return dict(commands)


def _detect_per_language(
    answers: Answers,
    root: Path,
    prompter: Prompter,
    where: Path,
    suffixes: Sequence[str],
) -> dict[str, dict[str, str]]:
    """Detection defaults per language: one question, then one call each.

    Consent is asked once. Asking per language would be the same decision put
    four times over, and what is being consented to — reading files already in
    this repository — does not change with the language being asked about.

    A failure ends the sweep rather than being retried per language. Every
    failure here is a property of the planner or of the repository — no model
    configured, nothing to read, the endpoint down — so the next call fails
    exactly as the first did, having cost another wait.
    """
    subject = "this repo" if where == root else where.name
    if not prompter.confirm(
        f"\nRead {subject}'s CI config and docs to find them", default=True
    ):
        return {}

    found: dict[str, dict[str, str]] = {}
    for suffix in suffixes:
        say(f"  reading for {suffix}…")
        detection = detect_commands(
            root, answers.models, answers.roles, where=where, language=suffix
        )
        if not detection.ok:
            say(f"  \033[33mskipped\033[0m  {detection.error}")
            say("  Enter them yourself, or leave blank and fill them in later.")
            break
        if not detection.found_anything:
            say(f"  nothing here states a verify command for {suffix}.")
            continue
        if detection.confidence != "high":
            say("  \033[33mlow confidence\033[0m — check these before accepting.")
        found[suffix] = detection.commands
    return found


def _detect_or_ask(
    answers: Answers, root: Path, prompter: Prompter, where: Path | None = None
) -> dict[str, str]:
    """Offer model-backed detection, returning defaults for the command prompts.

    Declining, failing, or finding nothing all produce the same thing: empty
    defaults. Nothing here guesses from a file name — a repo whose commands are
    written down nowhere is a repo only its author can answer for.
    """
    blank = {"lint": "", "typecheck": "", "test": ""}

    subject = "this repo" if where is None or where == root else where.name
    if not prompter.confirm(
        f"\nRead {subject}'s CI config and docs to find them", default=True
    ):
        return blank

    say("  reading…")
    detection = detect_commands(root, answers.models, answers.roles, where=where)

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
    payload: dict[str, Any] = {
        "room": config.room,
        "models": config.models,
        "roles": config.roles,
    }
    if config._explicit_workspaces:
        payload["workspaces"] = [
            {"root": w.root, "commands": w.commands} for w in config.workspaces
        ]
    else:
        payload["commands"] = config.commands
    payload["neverDelegate"] = config.never_delegate
    payload["memory"] = config.memory
    return "\n".join("  " + line for line in json.dumps(payload, indent=2).splitlines())
