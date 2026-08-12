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
#
# The closing fence must be at least as long as the opening one, which is the
# CommonMark rule and the only way a file whose own contents contain fences can
# be transported at all. A README wrapped in three backticks ends at the first
# fence *inside* the README: the file is written truncated, and everything
# after it is re-parsed as though it were more files. That is not theoretical —
# it silently replaced a working `build.sh` with a fragment of markdown and
# failed the ticket three times for a defect the executor never made.
_BLOCK = re.compile(
    r"^[ \t]*(?:(?:File|Path)\s*:\s*)?[`'\"]?(?P<path>[\w./\\+-]+\.[\w+]+)[`'\"]?[ \t]*:?[ \t]*\n"
    r"(?P<fence>`{3,})[^\n]*\n"
    r"(?P<body>.*?)"
    r"^(?P=fence)`*[ \t]*$",
    re.MULTILINE | re.DOTALL,
)

BLOCKED_PREFIX = "BLOCKED:"

# A fence line on its own — an opener with an optional language, or a closer.
_FENCE_LINE = re.compile(r"^[ \t]*```[^\n]*$")


def _unwrap_double_fence(body: str) -> str:
    """Drop an inner fence the outer match swallowed.

    Models sometimes wrap the whole reply in a fence, or emit the path line and
    then two fences (`\\n```\\n```rust\\n`). `_BLOCK` binds to the *first*
    opener, so the second one lands inside the captured body and gets written
    into the file. That produced three `.rs` files in a real run whose second
    line was ```` ```rust ````, which broke `cargo clippy --all-targets` for
    every ticket afterwards — the ticket that caused it had long since passed.

    Only triggers when the body's first non-blank line is itself a fence, which
    is not something a source file does.
    """
    lines = body.split("\n")
    first = next((i for i, line in enumerate(lines) if line.strip()), None)
    if first is None or not _FENCE_LINE.match(lines[first]):
        return body

    del lines[first]
    # The matching closer, if the model emitted one, is now trailing.
    last = next(
        (i for i in range(len(lines) - 1, -1, -1) if lines[i].strip()), None
    )
    if last is not None and _FENCE_LINE.match(lines[last]):
        del lines[last]
    return "\n".join(lines)


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
        FileEdit(
            path=match.group("path").replace("\\", "/"),
            content=_unwrap_double_fence(match.group("body")),
        )
        for match in _BLOCK.finditer(text)
    ]
    return ParsedOutput(edits=edits)


def duplicate_paths(parsed: ParsedOutput) -> list[str]:
    """Paths the response wrote more than once, in first-seen order.

    The executor is told to emit each changed file exactly once, whole. Two
    blocks for one path therefore means something went wrong upstream of the
    write, and the usual cause is a fence: a file containing its own fenced
    block closes the outer fence early, and the remainder of that file gets
    re-parsed into extra blocks whose paths came out of its prose.

    Worth its own check because of the order `apply_edits` works in. The
    spurious block is always the later one, so it wins, and the result is a
    file replaced by a fragment of a different file — with a successful apply
    step, no rejected paths, and nothing in the log to connect the two. Loud
    and retryable beats silent and wrong.
    """
    seen: set[str] = set()
    repeated: list[str] = []
    for edit in parsed.edits:
        path = normalize_path(edit.path)
        if path in seen and path not in repeated:
            repeated.append(path)
        seen.add(path)
    return repeated


def normalize_path(path: str) -> str:
    """Canonical repo-relative form, for comparing a model's path to a pattern.

    Models write the same file several ways — `./build.sh`, `build.sh`,
    `.\\build.sh` — and a scope check that treats those as different rejects a
    ticket's *own* allowed file. That happened for real: TT-006 listed
    `build.sh` and had `./build.sh` rejected as out of scope on every attempt,
    so the ticket could never finish no matter what the executor wrote.

    Only a leading `./` is stripped. An absolute path is left alone on purpose:
    turning `/etc/passwd` into `etc/passwd` could make it match a repo-relative
    pattern, and a scope check should never widen what it accepts.
    """
    normalized = path.replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def matches_any(path: str, patterns: list[str]) -> bool:
    normalized = normalize_path(path)
    for pattern in patterns:
        pattern = normalize_path(pattern)
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


# Ways a test file can re-declare the code under test instead of referencing
# it. Every one of these introduces a symbol the linker or loader must resolve
# from somewhere else at build time — and in a test target built from the same
# crate, there is nowhere else. The result is not a failing assertion but a
# failing *link*, which takes down every other test in the target with it.
_FOREIGN_BINDINGS: tuple[tuple[str, str], ...] = (
    # Rust / C / C++: `extern "C" { fn game_new(); }`. The block form only —
    # `extern crate serde` and `pub extern "C" fn` are declarations of a
    # different kind and are fine.
    (r'extern\s*(?:"[A-Za-z0-9_-]+"\s*)?\{', 'extern block'),
    # Python ctypes and cffi.
    (r"\bctypes\s*\.\s*(?:CDLL|cdll|WinDLL|windll|PyDLL)\b", "ctypes"),
    (r"\bCDLL\s*\(", "ctypes"),
    (r"\bcdll\s*\.\s*LoadLibrary\b", "ctypes"),
    (r"\bffi\s*\.\s*dlopen\s*\(", "cffi"),
    # cgo. Only the import itself; the word "C" is too common otherwise.
    (r'^\s*import\s+"C"\s*$', "cgo import"),
    # .NET P/Invoke.
    (r"\[\s*DllImport\b", "DllImport"),
    # JVM.
    (r"\bSystem\s*\.\s*load(?:Library)?\s*\(", "System.loadLibrary"),
    # JavaScript / Deno FFI.
    (r"\bDeno\s*\.\s*dlopen\s*\(", "Deno.dlopen"),
    (r"\bffi\s*\.\s*Library\s*\(", "node-ffi"),
    (r"\bkoffi\s*\.\s*load\s*\(", "koffi"),
    # The bare C call, whatever the language wrapping it.
    (r"\bdlopen\s*\(", "dlopen"),
)

_FOREIGN_BINDING_RES = tuple(
    (re.compile(pattern, re.MULTILINE), label) for pattern, label in _FOREIGN_BINDINGS
)


def foreign_bindings(text: str) -> list[str]:
    """Lines in `text` that re-declare code under test as a foreign binding.

    Returned as `label: line` so the tester can be shown exactly what was
    rejected. Empty when the file references its subject normally, which is
    what a host-run test of an exported function is supposed to do.

    A comment mentioning one of these is not a declaration, so obvious comment
    lines are skipped. The check is a net, not a parser: it exists because a
    prohibition in the prompt was ignored by a small local model, and its
    failure mode is asking for one rewrite that was not needed.
    """
    found: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("//", "#", "*", "--")) or line.startswith("/*"):
            continue
        for pattern, label in _FOREIGN_BINDING_RES:
            if pattern.search(line):
                found.append(f"{label}: {line[:120]}")
                break
    return found
