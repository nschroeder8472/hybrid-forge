"""Ask a model what this repository's verify commands actually are.

`commands.lint` / `.typecheck` / `.test` is the highest-leverage field in the
config and the easiest to get wrong. The loop reads a failing check as a reason
to re-delegate, so a command that does not work does not fail once — it fails
`maxAttempts` times per ticket and parks the entire backlog, looking for all the
world like a bad executor model.

Inferring it from a marker file is guessing. `Cargo.toml` present does not mean
`cargo test` is the command: the repo may use `cargo nextest run`, need
`--workspace`, gate tests behind a feature flag, or run them through `just`. The
project's CI workflow already states the real answer, and so does its Makefile
and its contributing guide.

So this reads those documents and asks a model to extract what is written in
them. Two deliberate choices about how:

**The evidence is gathered here, not by the model.** The `claude-cli` adapter
could read the repo with its own tools, but only with `allowAllTools` — a real
grant of authority — and a headless session without it may stall on a
permission prompt or quietly return nothing. Collecting the files ourselves
works identically across every adapter, sends exactly what we can name, and
needs no permission at all.

**It reports what it did not find.** A repo with no CI, no Makefile, and no
contributing guide has nothing to extract, and the honest answer is an empty
command the user fills in — not a plausible default. An empty command is skipped
by the loop; a wrong one parks the backlog.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .providers import Message, Provider, ProviderError

# Per-file and total caps on gathered evidence. The prompt has to fit whatever
# model is playing planner, which may be a local 32k one, and a vendored
# lockfile or a 4000-line workflow would crowd out everything useful.
MAX_FILE_CHARS = 6_000
MAX_TOTAL_CHARS = 24_000

# Where projects actually write their build commands, most authoritative first.
# CI is first because it is the one that has to be correct — a stale Makefile
# target survives indefinitely, a stale CI step turns the build red.
EVIDENCE_GLOBS: tuple[str, ...] = (
    ".github/workflows/*.yml",
    ".github/workflows/*.yaml",
    ".gitlab-ci.yml",
    ".circleci/config.yml",
    "azure-pipelines.yml",
    "Makefile",
    "makefile",
    "Justfile",
    "justfile",
    "Taskfile.yml",
    "package.json",
    "pyproject.toml",
    "tox.ini",
    "noxfile.py",
    ".pre-commit-config.yaml",
    "Cargo.toml",
    "go.mod",
    "Package.swift",
    "build.gradle",
    "build.gradle.kts",
    "pom.xml",
    "Gemfile",
    "composer.json",
    "CONTRIBUTING.md",
    "DEVELOPMENT.md",
    "DEVELOPING.md",
    "docs/CONTRIBUTING.md",
    "docs/DEVELOPMENT.md",
    "README.md",
)

# Files that mark a **build root** — a directory with its own dependency tree
# and its own commands, which have to run from inside it. Narrower than
# `EVIDENCE_GLOBS` on purpose: a README states commands and is not a build, and
# proposing a workspace around every markdown file would bury the two that
# matter.
#
# `Makefile` is deliberately absent. It marks a build often enough to tempt and
# sits at the root of repositories with no subprojects at all, so it proposes
# the workspace that already exists and nothing else.
BUILD_MANIFESTS: tuple[str, ...] = (
    "package.json",
    "deno.json",
    "Cargo.toml",
    "pyproject.toml",
    "setup.py",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "Gemfile",
    "composer.json",
    "Package.swift",
    "project.godot",
    "mix.exs",
    "pubspec.yaml",
    "CMakeLists.txt",
)

# What each language needs on disk before any of it can be built, and where a
# build root would name it. Narrower than `BUILD_MANIFESTS`, which answers
# "is there a build here" — this answers "can this language be built here at
# all", and the two are different questions with different costs for being
# wrong.
#
# Only the ecosystems where the answer is unambiguous. Python is the instructive
# omission: a directory of standalone `.py` files with no `pyproject.toml` is
# ordinary and runs perfectly, so listing it would report a hole in half the
# repositories that exist. Same for Ruby, PHP, C and shell. A language belongs
# here only when its code genuinely cannot be built without the file.
LANGUAGE_MANIFESTS: dict[str, tuple[str, ...]] = {
    ".ts": ("package.json", "deno.json"),
    ".tsx": ("package.json", "deno.json"),
    ".mts": ("package.json", "deno.json"),
    ".cts": ("package.json", "deno.json"),
    ".rs": ("Cargo.toml",),
    ".go": ("go.mod",),
    ".java": ("pom.xml", "build.gradle", "build.gradle.kts"),
    ".kt": ("pom.xml", "build.gradle", "build.gradle.kts"),
    ".swift": ("Package.swift",),
    ".ex": ("mix.exs",),
    ".exs": ("mix.exs",),
    ".dart": ("pubspec.yaml",),
    ".scala": ("build.sbt", "pom.xml", "build.gradle"),
}


def manifests_for(suffix: str) -> tuple[str, ...]:
    """The build files this language cannot be built without, or `()`.

    Empty is the common answer and means "no opinion" — not "no manifest
    needed", but "this cannot tell, so it will not say".
    """
    return LANGUAGE_MANIFESTS.get(suffix.lower(), ())


# Directories whose contents are generated, vendored, or private to a tool.
# `node_modules` alone holds thousands of `package.json` files, every one of
# which would otherwise be proposed as a build in this repository.
_SKIP_DIRS = frozenset(
    {
        "node_modules", "target", "build", "dist", "out", "vendor", "bin", "obj",
        ".git", ".hg", ".svn", ".hybridforge", ".venv", "venv", "__pycache__",
        ".tox", ".mypy_cache", ".pytest_cache", ".next", ".gradle", ".idea",
        "site-packages", "third_party", "Pods",
    }
)

# How deep a build root can sit before it stops being one worth proposing. A
# monorepo nests packages three or four deep and a person configuring one will
# say so by hand; the case this exists for is `tools/path-forge` beside a game.
MAX_WORKSPACE_DEPTH = 3


def manifest_gaps(config: Any, tickets: Sequence[Any]) -> list[str]:
    """Languages a backlog writes that its build cannot build yet.

    A backlog creating a whole new language tree in a repository with nothing
    to build it is the third root cause of the run this exists for: fifteen
    tickets wrote 4,000 lines of TypeScript into a repository with no
    `package.json`, no `tsconfig.json`, and no ticket owning either — so
    nothing could compile, type-check or test a line of it, and every gate
    downstream read the absence of complaints as the absence of a problem.

    Reported, never refused, and the reason is that a refusal here has no
    escape hatch. `commands` has an exemption spelling for a language nothing
    runs; there is none for "this project builds its TypeScript with a Makefile
    and no `package.json`", which is unusual and not wrong. The gates that do
    refuse — an unowned file at ingest, a canary that stays green over a file
    that cannot parse — catch the *consequences* of this on their own. What
    this adds is the cause, named at the one moment fixing it is free.

    A manifest some ticket in the backlog creates is not a gap: writing the
    build file and the first module it builds is an ordinary way to start.
    """
    created = {
        _normal(path)
        for ticket in tickets
        for path in ticket.allowed_files
        if not any(character in path for character in "*?[")
    }
    # Keyed by the *manifest*, not the extension. `.ts` and `.tsx` are one
    # ecosystem with one `package.json`, and reporting them separately says the
    # same sentence twice about the same missing file.
    found: dict[tuple[str, tuple[str, ...]], tuple[Any, set[str]]] = {}
    for ticket in tickets:
        for path in ticket.allowed_files:
            if any(character in path for character in "*?["):
                continue
            suffix = Path(path).suffix.lower()
            wanted = manifests_for(suffix)
            if not wanted:
                continue
            workspace = config.workspace_for(path)
            if workspace is None:
                continue  # refused outright by the unowned-file gate
            entry = found.setdefault((workspace.root, wanted), (workspace, set()))
            entry[1].add(suffix)

    gaps: list[str] = []
    for (_root, wanted), (workspace, suffixes) in sorted(
        found.items(), key=lambda item: item[0]
    ):
        if any(
            (config.root / f"{workspace.prefix}{name}").is_file()
            or _normal(f"{workspace.prefix}{name}") in created
            for name in wanted
        ):
            continue
        where = "" if workspace.is_repo_root else f" in {workspace.root}"
        languages = ", ".join(sorted(suffixes))
        gaps.append(
            f"{languages}{where}: nothing here builds it. There is no "
            f"{' or '.join(wanted)}{where or ' in this repository'}, and no "
            f"ticket creates one, so nothing can compile, type-check or "
            f"test the {languages} this backlog writes."
        )
    return gaps


def _normal(path: str) -> str:
    return str(path).replace("\\", "/").lstrip("./").lower()


def discover_workspaces(root: Path, *, max_depth: int = MAX_WORKSPACE_DEPTH) -> list[str]:
    """Directories in this repository that look like their own build.

    Returned as repo-relative posix paths with `.` first when the root itself
    holds a manifest, which is the order they should be proposed in.

    This proposes; it never decides. A directory holding a `package.json` is
    strong evidence of a build and no evidence at all about whether the person
    wants it verified separately — a repository with one `package.json` at its
    root is the ordinary single-build case and must keep behaving like one.
    """
    found: list[str] = []
    root = Path(root)

    def walk(directory: Path, depth: int) -> None:
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            return
        if any(entry.name in BUILD_MANIFESTS and entry.is_file() for entry in entries):
            relative = directory.relative_to(root).as_posix()
            found.append(relative if relative != "." else ".")
        if depth >= max_depth:
            return
        for entry in entries:
            if not entry.is_dir() or entry.is_symlink():
                continue
            if entry.name in _SKIP_DIRS or entry.name.startswith("."):
                continue
            walk(entry, depth + 1)

    walk(root, 0)
    return found


DETECT_SYSTEM = """You identify a repository's verify commands.

You are given excerpts from a repository's CI configuration, build files, and
contributor documentation. Report the exact shell commands this project uses to
lint, type-check, and run its tests.

Rules:
- Report only what the evidence supports. If nothing states a command, return an
  empty string for it. An empty command is skipped; a guessed one is run against
  model-authored code and its failure parks the work.
- Prefer what CI runs over what documentation suggests. CI has to be correct.
- A repository with no CI still counts as evidence when its README or docs state
  how to run the tests — including in a file-layout listing or a one-line
  "run it with X" aside. Reporting a command the project documents is right;
  inventing one it never mentions is not.
- Distinguish a command this project runs on itself from an example it shows its
  users. A tool whose docs demonstrate configuring `pytest` is not thereby a
  project that runs `pytest`.
- Copy the command as written, including flags, workspace selectors, and any
  runner prefix (`make`, `just`, `npm run`, `uv run`, `poetry run`).
- Do not combine two commands with `&&`. Each field is one command.
- Do not include `cd`, environment variable prefixes, or CI-only wrappers
  (`actions/checkout`, matrix expressions, `${{ ... }}` interpolation). If a
  command's real form depends on a CI variable, leave it empty and say so.
- Some projects have no separate type-check step. That is normal — return empty
  rather than inventing one.

Reply with a single JSON object and nothing else:

{"lint": "...", "typecheck": "...", "test": "...",
 "source": "which file each command came from, one short line",
 "confidence": "high" | "low"}
"""


@dataclass
class Detection:
    """What a detection attempt produced, successful or not."""

    commands: dict[str, str] = field(default_factory=lambda: {"lint": "", "typecheck": "", "test": ""})
    source: str = ""
    confidence: str = "low"
    # Populated when detection could not run or could not be trusted. The
    # wizard shows this instead of pretending it found something.
    error: str = ""
    # Files actually read, for showing the user what the answer was based on.
    evidence: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def found_anything(self) -> bool:
        return any(self.commands.values())


# Lines worth keeping from a long document once its head has been taken. A
# README states how to run the tests near the bottom at least as often as the
# top, so head-truncating one throws away the answer.
_COMMAND_HINT = re.compile(
    r"\b(test|tests|lint|clippy|check|typecheck|type-check|mypy|pyright|ruff|"
    r"flake8|eslint|pytest|unittest|nox|tox|cargo|gradle|mvn|make|just|npm|pnpm|"
    r"yarn|go\s+(test|vet|build)|swift\s+(test|build)|dotnet|rake|bundle)\b",
    re.IGNORECASE,
)


def excerpt(text: str, limit: int) -> str:
    """Trim `text` to `limit`, keeping the parts that name commands.

    Takes the head, then sweeps the remainder for fenced code blocks and lines
    mentioning a build tool, so a command documented at the end of a long README
    survives truncation. Falls back to a plain head cut when nothing matches.
    """
    if len(text) <= limit:
        return text

    head_size = max(limit // 3, 1)
    head = text[:head_size]
    rest = text[head_size:]

    kept: list[str] = []
    budget = limit - head_size
    in_fence = False

    for line in rest.splitlines():
        if budget <= 0:
            break
        fence = line.lstrip().startswith("```")
        if fence:
            in_fence = not in_fence
        # Fenced blocks are where commands live; hint-matching lines catch the
        # prose forms ("run `make test` before opening a PR").
        if in_fence or fence or _COMMAND_HINT.search(line):
            candidate = line[:budget]
            kept.append(candidate)
            budget -= len(candidate) + 1

    if not kept:
        return text[:limit] + "\n… (truncated)"
    return head + "\n… (truncated; command-bearing lines below)\n" + "\n".join(kept)


def gather_evidence(root: Path) -> list[tuple[str, str]]:
    """Collect (relative path, excerpt) for files that state build commands.

    Ordered by `EVIDENCE_GLOBS`, truncated per file and in total, so the prompt
    stays bounded regardless of what the repository contains.
    """
    collected: list[tuple[str, str]] = []
    seen: set[Path] = set()
    budget = MAX_TOTAL_CHARS

    for pattern in EVIDENCE_GLOBS:
        if budget <= 0:
            break
        for path in sorted(root.glob(pattern)):
            if budget <= 0:
                break
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if not text.strip():
                continue

            trimmed = excerpt(text, MAX_FILE_CHARS)[:budget]
            budget -= len(trimmed)
            collected.append((path.relative_to(root).as_posix(), trimmed))

    return collected


def build_prompt(
    root: Path, evidence: list[tuple[str, str]], language: str = ""
) -> list[Message]:
    blocks = [f"### {name}\n```\n{text}\n```" for name, text in evidence]
    asked = (
        f"What are this project's lint, type-check, and test commands "
        f"**for its {language} files specifically**? A polyglot repository runs "
        f"each language its own way, and the command for another language is "
        f"worse than none — it passes without running a line of {language} and "
        f"reports that as verified. Answer only for {language}, and leave a "
        f"field empty when this project states nothing for it."
        if language
        else "What are this project's lint, type-check, and test commands?"
    )
    body = f"Repository: {root.name}\n\n" + "\n\n".join(blocks) + f"\n\n{asked}"
    return [
        Message(role="system", content=DETECT_SYSTEM),
        Message(role="user", content=body),
    ]


def _json_object(text: str) -> dict[str, Any]:
    """Pull a JSON object out of a reply that may be fenced or prefaced.

    Tolerant for the same reason the planner's parser is: a strict reader here
    discards a correct answer over a code fence.
    """
    candidate = text.strip()
    fence = re.search(r"```(?:json)?\s*\n(.*?)\n```", candidate, re.DOTALL)
    if fence:
        candidate = fence.group(1)
    else:
        first, last = candidate.find("{"), candidate.rfind("}")
        if first != -1 and last > first:
            candidate = candidate[first : last + 1]

    data = json.loads(candidate)
    if not isinstance(data, dict):
        raise ValueError("reply was not a JSON object")
    return data


# A command the model returned that we refuse to accept. These are the shapes
# that look like an answer and are not one — running them would fail every
# ticket, which is the exact outcome this module exists to prevent.
_REJECT = (
    re.compile(r"\$\{\{"),           # unexpanded CI interpolation
    re.compile(r"^\s*cd\s"),         # a directory change, not a verify command
    re.compile(r"<[a-zA-Z_ -]+>"),   # a placeholder the model invented
    re.compile(r"^\s*#"),            # a comment
)


def clean_command(value: Any) -> str:
    """Normalize one command, or return "" when it is not usable as-is."""
    if not isinstance(value, str):
        return ""
    command = value.strip().strip("`").strip()
    if not command or command.lower() in ("none", "n/a", "null", "-"):
        return ""
    if any(pattern.search(command) for pattern in _REJECT):
        return ""
    # Multi-line replies mean the model described a procedure, not a command.
    if "\n" in command:
        return ""
    return command


def parse_detection(text: str) -> Detection:
    """Turn a model reply into a Detection, rejecting anything unusable."""
    try:
        data = _json_object(text)
    except (json.JSONDecodeError, ValueError) as exc:
        return Detection(error=f"could not read the reply as JSON ({exc})")

    return Detection(
        commands={name: clean_command(data.get(name)) for name in ("lint", "typecheck", "test")},
        source=str(data.get("source", ""))[:300],
        confidence="high" if str(data.get("confidence", "")).lower() == "high" else "low",
    )


def detect(
    root: Path, provider: Provider, *, max_tokens: int = 1024, language: str = ""
) -> Detection:
    """Read the repo's own documents and ask `provider` what they say.

    Never raises. Detection is a convenience on top of a question the user can
    always answer themselves, so every failure path returns a Detection whose
    `error` explains what happened and whose commands are empty.

    `language` narrows the question to one of them. A repository that builds
    Rust and serves a JavaScript page states both, and the answer for the wrong
    one is worse than no answer: it passes without running a line of the
    language it claims to cover.
    """
    evidence = gather_evidence(root)
    if not evidence:
        return Detection(
            error="no CI config, build file, or contributing guide to read in this repo"
        )

    try:
        completion = provider.complete(
            build_prompt(root, evidence, language), max_tokens=max_tokens, temperature=0.0
        )
    except ProviderError as exc:
        return Detection(error=str(exc))
    except Exception as exc:  # noqa: BLE001 - setup convenience must not crash setup
        return Detection(error=f"{type(exc).__name__}: {exc}")

    detection = parse_detection(completion.text)
    detection.evidence = [name for name, _ in evidence]
    return detection
