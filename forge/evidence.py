"""What a bug report can be checked against, gathered from the repository.

A plan states which files a ticket may write. A bug report does not — the file
that needs changing is the thing being looked for, and the person reporting
"pieces sometimes drop three at once after I switch tabs" is describing a
symptom, not a location. So the scope has to be discovered before a ticket can
be written at all.

**The harness gathers it, not the model.** An adapter with tools could search
the repository itself, but only some adapters have tools, only with a real
grant of authority, and a headless session without it may stall on a permission
prompt or quietly return nothing. Collecting the evidence here works
identically behind every adapter, sends exactly what we can name, and needs no
permission at all. Same reasoning as `toolchain.py`, for the same reason.

Nothing here interprets. It answers "what files exist" and "where do the words
in this report appear", and the planner decides what that means.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

# Enough of a tree to locate work in a normal repository, and few enough paths
# that the report and the grep hits still fit beside it.
MAX_FILES = 400
# Per-term, so one common word cannot crowd out every other term's hits.
MAX_HITS = 12
MAX_TERMS = 12
LINE_LIMIT = 200


def _git(root: Path, *args: str) -> list[str]:
    """Run a git command, returning its lines. Never raises.

    Every caller here is gathering context. A repository without git, a git
    that is not installed, a command that times out — each costs the planner
    some evidence and none of them is worth failing a bug report over.
    """
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode not in (0, 1):  # 1 is grep's "no matches"
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def tracked_files(root: Path, limit: int = MAX_FILES) -> list[str]:
    """The repository's tracked files, which is the only list worth showing.

    `git ls-files` rather than a walk: it already honours `.gitignore`, so
    `node_modules`, `target/` and a virtualenv never reach the prompt. A repo
    that is not a git checkout returns nothing rather than a directory listing
    of every build artifact it happens to contain.
    """
    return sorted(_git(root, "ls-files"))[:limit]


# Words in a report worth searching for: a backticked span, something that
# looks like a path, a CamelCase or snake_case identifier, a `Type::method`.
# Prose words are deliberately not searched — "sometimes" matches everything
# and locates nothing.
_QUOTED = re.compile(r"`([^`]{2,80})`")
_PATHLIKE = re.compile(r"\b[\w./\\-]+\.[A-Za-z0-9]{1,5}\b")
_IDENTIFIER = re.compile(
    r"\b(?:[A-Za-z_][A-Za-z0-9_]*::[A-Za-z_][A-Za-z0-9_]*"
    r"|[a-z][a-z0-9]*_[a-z0-9_]+"
    r"|[A-Z][a-z0-9]+[A-Z][A-Za-z0-9]*)\b"
)


def terms(report: str, limit: int = MAX_TERMS) -> list[str]:
    """Searchable terms out of a prose report, most specific first.

    Ordered by how much a hit would tell you: something the reporter put in
    backticks was named deliberately, a path names a file outright, and an
    identifier shape is a name the code plausibly uses. Duplicates collapse
    case-insensitively so `SoftDrop` and `softdrop` do not spend two searches
    saying the same thing.
    """
    found: list[str] = []
    seen: set[str] = set()
    for pattern in (_QUOTED, _PATHLIKE, _IDENTIFIER):
        for match in pattern.finditer(report):
            term = match.group(1) if pattern is _QUOTED else match.group(0)
            term = term.strip()
            key = term.lower()
            if len(term) < 3 or key in seen:
                continue
            seen.add(key)
            found.append(term)
    return found[:limit]


def hits(root: Path, term: str, limit: int = MAX_HITS) -> list[str]:
    """Where a term appears in tracked source, as `path:line: text`.

    Fixed-string, case-insensitive, and capped. The planner needs enough to
    tell `src/game.rs` from `src/board.rs`, not a concordance.
    """
    lines = _git(root, "grep", "-n", "-I", "-i", "-F", "--", term)
    trimmed = []
    for line in lines[:limit]:
        trimmed.append(line[:LINE_LIMIT] + ("…" if len(line) > LINE_LIMIT else ""))
    return trimmed


def gather(root: Path, report: str) -> str:
    """Everything the planner gets to look at, as one block of text.

    Empty when the repository yields nothing — no git, no matches — and an
    empty block is the honest answer. A planner told "here is the evidence"
    over an invented file tree writes a ticket scoped to files that do not
    exist, which fails at the first apply and reads like a model problem.
    """
    sections: list[str] = []

    files = tracked_files(root)
    if files:
        listing = "\n".join(files)
        note = (
            f"\n[{MAX_FILES} of them; the repository has more]"
            if len(files) >= MAX_FILES
            else ""
        )
        sections.append(f"### Files in this repository\n{listing}{note}")

    matched: list[str] = []
    for term in terms(report):
        found = hits(root, term)
        if found:
            matched.append(f"#### `{term}`\n" + "\n".join(found))
    if matched:
        sections.append(
            "### Where the report's own words appear in the code\n"
            + "\n\n".join(matched)
        )

    return "\n\n".join(sections)
