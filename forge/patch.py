"""Turn model output into file writes, and refuse the ones out of scope.

The executor is told to emit complete file contents in fenced blocks preceded
by a path. This module parses that and — more importantly — enforces the
allowed-file list before anything touches disk.

Scope enforcement is the load-bearing part. An executor that quietly edits a
file outside its ticket is the failure mode that makes autonomous runs
untrustworthy, and it is not caught by tests: the tests pass, the diff looks
plausible, and something unrelated broke. Checking here means the loop can run
unattended without a human diffing every step.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path

# A path line followed by a fenced block. The path may be bare, backticked, or
# a `File: x` / `path/to/x:` label — models vary, and rejecting a correct
# implementation over label punctuation is a bad trade.
_BLOCK = re.compile(
    r"^[ \t]*(?:(?:File|Path)\s*:\s*)?[`'\"]?(?P<path>[\w./\\+-]+\.[\w+]+)[`'\"]?[ \t]*:?[ \t]*\n"
    r"```[^\n]*\n"
    r"(?P<body>.*?)"
    r"^```[ \t]*$",
    re.MULTILINE | re.DOTALL,
)

BLOCKED_PREFIX = "BLOCKED:"


@dataclass
class FileEdit:
    path: str
    content: str


@dataclass
class ParsedOutput:
    edits: list[FileEdit] = field(default_factory=list)
    blocked_reason: str = ""
    # Paths the model tried to write that its ticket did not allow.
    rejected: list[str] = field(default_factory=list)

    @property
    def is_blocked(self) -> bool:
        return bool(self.blocked_reason)

    @property
    def is_empty(self) -> bool:
        return not self.edits and not self.blocked_reason


def parse_output(text: str) -> ParsedOutput:
    """Extract file edits, or the executor's refusal to guess."""
    stripped = text.strip()
    if stripped.startswith(BLOCKED_PREFIX):
        return ParsedOutput(blocked_reason=stripped[len(BLOCKED_PREFIX) :].strip())

    # A BLOCKED marker anywhere in the response counts, since models often lead
    # with a sentence of preamble before the marker.
    marker = re.search(rf"^{BLOCKED_PREFIX}(?P<reason>.*)", text, re.MULTILINE)
    if marker and not _BLOCK.search(text):
        return ParsedOutput(blocked_reason=marker.group("reason").strip())

    edits = [
        FileEdit(path=match.group("path").replace("\\", "/"), content=match.group("body"))
        for match in _BLOCK.finditer(text)
    ]
    return ParsedOutput(edits=edits)


def matches_any(path: str, patterns: list[str]) -> bool:
    normalized = path.replace("\\", "/")
    for pattern in patterns:
        pattern = pattern.replace("\\", "/")
        if fnmatch.fnmatch(normalized, pattern):
            return True
        # `src/auth/**` should match `src/auth/x.py` under fnmatch too, which
        # treats `**` as a single `*` — check the directory prefix explicitly.
        if pattern.endswith("/**") and normalized.startswith(pattern[:-3] + "/"):
            return True
    return False


def enforce_scope(
    parsed: ParsedOutput,
    allowed: list[str],
    never_delegate: list[str],
) -> ParsedOutput:
    """Drop edits the ticket did not authorize.

    An empty `allowed` list means the ticket named no files, which is a spec
    defect — every edit is rejected rather than defaulting to permissive.
    """
    kept: list[FileEdit] = []
    rejected: list[str] = list(parsed.rejected)

    for edit in parsed.edits:
        if not matches_any(edit.path, allowed):
            rejected.append(f"{edit.path} (outside the ticket's allowed files)")
            continue
        if matches_any(edit.path, never_delegate):
            rejected.append(f"{edit.path} (matches a neverDelegate pattern)")
            continue
        kept.append(edit)

    return ParsedOutput(edits=kept, blocked_reason=parsed.blocked_reason, rejected=rejected)


def is_safe_path(root: Path, candidate: str) -> bool:
    """True when `candidate` resolves inside `root`.

    Model output is untrusted input. A path like `../../.ssh/authorized_keys`
    would otherwise pass an allowed-files glob written with `..` in it, or
    escape via a symlink, so the resolved path is checked against the project
    root before any write.
    """
    try:
        resolved = (root / candidate).resolve()
    except (OSError, ValueError):
        return False
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        return False
    return True


def apply_edits(root: Path, edits: list[FileEdit]) -> list[str]:
    """Write the edits to disk, returning the paths written.

    Raises on a path that escapes the project root — that is not a scope
    disagreement to log and continue past, it is an attempt to write outside
    the repository.
    """
    written: list[str] = []
    for edit in edits:
        if not is_safe_path(root, edit.path):
            raise ValueError(f"refusing to write outside the project root: {edit.path}")
        target = root / edit.path
        target.parent.mkdir(parents=True, exist_ok=True)
        body = edit.content
        if not body.endswith("\n"):
            body += "\n"
        target.write_text(body, encoding="utf-8")
        written.append(edit.path)
    return written
