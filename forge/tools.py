"""The read-only tools a role may call while it works.

A role with no filesystem has to be handed its context in advance, and what to
hand it can only be guessed at before anyone has read a line. Run 1 of
`HANDBACK-DASHBOARD.md` is what that guess costs: 83% of a 47k-token prompt was
a test suite nobody asked for, the one file the spec named was absent, and the
executor spent nine attempts asking for a shell it did not have. See
docs/CONTEXT-TOOLS.md.

So the role gets to look. Four tools, all read-only:

    read_file(path, start, end)   the contents, or a slice
    grep(pattern, glob)           where a symbol is defined or used
    list_dir(path)                what is in a directory
    outline(path)                 definitions and signatures, no bodies

**No write, no shell, no network.** The property this loop rests on is that a
model cannot change the tree except through a patch that was reviewed; that is
a fact about the write path and it is untouched here. *A model cannot look at
the tree* was never the safety property — it was an accident of the executor
having no filesystem.

Every path is resolved and confined to the repository root by
`patch.is_safe_path`, the same guard the write side uses, so `../../.ssh/id_rsa`
is refused here exactly as it is refused there. A refusal is content, not an
exception: the model is told what was wrong with the call and gets another
turn, because a role that cannot read one file usually can read the next.
"""

from __future__ import annotations

import ast
import fnmatch
import re
from pathlib import Path
from typing import Any

from .patch import is_safe_path, normalize_path
from .providers.base import ToolCall, ToolResult, ToolSpec

# What one tool result may return. Generous for a source file and small enough
# that four of them do not fill a window: the point of a tool is that the model
# reads what it needs, and a model that has just been handed 200k characters is
# back where the paste path left it.
MAX_RESULT_CHARS = 24_000
# Lines returned by `read_file` when the caller names no range. Enough for most
# files in one call; longer ones say how to ask for the rest.
DEFAULT_READ_LINES = 400
# Matches reported by one `grep`. A pattern with more hits than this is too
# broad to be answered usefully, and saying so beats truncating silently.
MAX_MATCHES = 60
# Entries listed by `list_dir`.
MAX_ENTRIES = 200
# Directories never walked, listed or searched. Not a security boundary —
# `is_safe_path` is that — but a model that greps `.git` gets pack files, and
# one that lists `__pycache__` learns nothing.
SKIP_DIRS = frozenset(
    {".git", "__pycache__", ".pytest_cache", "node_modules", ".venv", "venv",
     ".mypy_cache", ".ruff_cache", "dist", "build", ".hybridforge"}
)
# Read as text, or refused as binary. A model handed a `.png` decoded with
# `errors="replace"` sees several thousand replacement characters and believes
# them — the same failure `_IMAGE_TYPES` exists to prevent on the paste path.
BINARY_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".zip", ".gz", ".tar",
     ".exe", ".dll", ".so", ".dylib", ".gguf", ".bin", ".db", ".sqlite",
     ".ico", ".woff", ".woff2", ".ttf", ".mp4", ".mp3", ".wav"}
)


TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="read_file",
        description=(
            "Read a file from the repository. Returns the file's lines, each "
            "prefixed with its line number. Give `start` and `end` to read one "
            "range of a long file; omit them to read from the beginning."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Repository-relative path, e.g. forge/state.py",
                },
                "start": {
                    "type": "integer",
                    "description": "First line to return, 1-based. Defaults to 1.",
                },
                "end": {
                    "type": "integer",
                    "description": "Last line to return, inclusive.",
                },
            },
            "required": ["path"],
        },
    ),
    ToolSpec(
        name="grep",
        description=(
            "Search the repository for a regular expression. Returns matching "
            "lines as `path:line: text`. Use this to find where a name is "
            "defined or used before reading a file."
        ),
        parameters={
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Python regular expression, e.g. class Store\\b",
                },
                "glob": {
                    "type": "string",
                    "description": (
                        "Restrict the search to paths matching this glob, "
                        "e.g. forge/*.py. Optional."
                    ),
                },
            },
            "required": ["pattern"],
        },
    ),
    ToolSpec(
        name="list_dir",
        description=(
            "List the files and directories under one repository path. "
            "Directories are marked with a trailing slash."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Repository-relative directory. Omit for the "
                        "repository root."
                    ),
                },
            },
        },
    ),
    ToolSpec(
        name="outline",
        description=(
            "List the definitions in a source file — classes, functions, their "
            "signatures and line numbers — without their bodies. Cheaper than "
            "reading a large file when you only need to know what is in it."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Repository-relative path to a source file.",
                },
            },
            "required": ["path"],
        },
    ),
]

TOOL_NAMES = frozenset(spec.name for spec in TOOLS)


class Toolbox:
    """Runs the read-only tools against one repository root.

    Holds the root and a call ledger. The ledger is what a step log gets: which
    files a role actually looked at, in order, which is the record that says
    whether a ticket failed for want of context or in spite of having it.
    """

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        # Every call made through this box: `(name, argument summary, ok)`.
        self.ledger: list[tuple[str, str, bool]] = []

    # -- dispatch ------------------------------------------------------

    def run(self, call: ToolCall) -> ToolResult:
        """Answer one tool call. Never raises.

        A tool that fails answers with the reason, because the model is going
        to get another turn either way and a turn spent on a refusal it can
        read is worth more than one spent on an exception it cannot.
        """
        handlers = {
            "read_file": self._read_file,
            "grep": self._grep,
            "list_dir": self._list_dir,
            "outline": self._outline,
        }
        handler = handlers.get(call.name)
        if handler is None:
            return self._done(
                call,
                f"no tool named `{call.name}`. Available: "
                + ", ".join(sorted(TOOL_NAMES)),
                ok=False,
                summary=call.name,
            )
        try:
            content, ok, summary = handler(call.arguments)
        except Exception as exc:  # noqa: BLE001 - reported to the model as text
            content, ok, summary = f"{call.name} failed: {exc}", False, call.name
        return self._done(call, content, ok=ok, summary=summary)

    def _done(
        self, call: ToolCall, content: str, *, ok: bool, summary: str
    ) -> ToolResult:
        self.ledger.append((call.name, summary, ok))
        if len(content) > MAX_RESULT_CHARS:
            content = (
                content[:MAX_RESULT_CHARS]
                + f"\n... result cut at {MAX_RESULT_CHARS} characters. "
                "Ask for a narrower range or a more specific pattern."
            )
        return ToolResult(call_id=call.call_id, name=call.name, content=content, ok=ok)

    # -- paths ---------------------------------------------------------

    def _resolve(self, raw: Any) -> tuple[Path | None, str]:
        """The path a call names, or `(None, why not)`.

        Refusals are worded as instructions rather than as errors: a model told
        "paths are relative to the repository root" retries correctly, and one
        told "invalid path" tries the same absolute path again.
        """
        if not isinstance(raw, str) or not raw.strip():
            return None, "`path` is required and must be a string."
        path = normalize_path(raw.strip())
        # `PurePath.is_absolute` is False for `/etc/passwd` on Windows — no
        # drive letter — so a model writing a POSIX absolute path there would
        # fall through to the containment check and be told the path is
        # outside the repository. True, and useless: what it needs to hear is
        # the form that works. Checked by shape rather than by platform.
        if path.startswith(("/", "\\")) or Path(path).is_absolute():
            return None, (
                f"`{raw}` is absolute. Paths are relative to the repository "
                "root, e.g. forge/state.py"
            )
        if not is_safe_path(self.root, path):
            return None, f"`{raw}` is outside the repository."
        return (self.root / path), ""

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def _walk(self) -> list[Path]:
        """Every readable file in the repository, skipping what is not source."""
        found: list[Path] = []
        for path in sorted(self.root.rglob("*")):
            if not path.is_file():
                continue
            if any(part in SKIP_DIRS for part in path.relative_to(self.root).parts):
                continue
            found.append(path)
        return found

    # -- the tools -----------------------------------------------------

    def _read_file(self, arguments: dict) -> tuple[str, bool, str]:
        path, why = self._resolve(arguments.get("path"))
        if path is None:
            return why, False, str(arguments.get("path", ""))
        name = arguments.get("path", "")
        if not path.exists():
            return (
                f"`{name}` does not exist. Use `list_dir` or `grep` to find it.",
                False,
                name,
            )
        if path.is_dir():
            return f"`{name}` is a directory — use `list_dir`.", False, name
        if path.suffix.lower() in BINARY_SUFFIXES:
            return (
                f"`{name}` is a binary file ({path.stat().st_size} bytes) and "
                "cannot be read as text.",
                False,
                name,
            )

        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        start = _positive(arguments.get("start"), 1)
        end = _positive(arguments.get("end"), start + DEFAULT_READ_LINES - 1)
        if start > len(lines):
            return (
                f"`{name}` has {len(lines)} lines; line {start} is past the end.",
                False,
                name,
            )
        end = min(end, len(lines))
        body = "\n".join(
            f"{number:>6}\t{text}"
            for number, text in enumerate(lines[start - 1 : end], start=start)
        )
        header = f"{name} lines {start}-{end} of {len(lines)}"
        if end < len(lines):
            header += (
                f" — {len(lines) - end} more. Call read_file again with "
                f"start={end + 1} for the rest."
            )
        return f"{header}\n{body}", True, f"{name}:{start}-{end}"

    def _grep(self, arguments: dict) -> tuple[str, bool, str]:
        raw = arguments.get("pattern")
        if not isinstance(raw, str) or not raw.strip():
            return "`pattern` is required and must be a string.", False, ""
        try:
            pattern = re.compile(raw)
        except re.error as exc:
            return f"`{raw}` is not a valid regular expression: {exc}", False, raw
        glob = arguments.get("glob") or ""
        if glob and not isinstance(glob, str):
            return "`glob` must be a string, e.g. forge/*.py", False, raw

        hits: list[str] = []
        for path in self._walk():
            relative = self._relative(path)
            if glob and not _matches_glob(relative, glob):
                continue
            if path.suffix.lower() in BINARY_SUFFIXES:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                if pattern.search(line):
                    hits.append(f"{relative}:{number}: {line.strip()[:200]}")
                    if len(hits) > MAX_MATCHES:
                        break
            if len(hits) > MAX_MATCHES:
                break

        summary = f"{raw}{' in ' + glob if glob else ''}"
        if not hits:
            scope = f" under `{glob}`" if glob else ""
            return (
                f"no line matches `{raw}`{scope}. The pattern is a Python "
                "regular expression; try a shorter one, or `list_dir` to see "
                "what is there.",
                True,
                summary,
            )
        if len(hits) > MAX_MATCHES:
            return (
                f"more than {MAX_MATCHES} matches for `{raw}` — narrow it with "
                f"`glob`, or search for something more specific. First "
                f"{MAX_MATCHES}:\n" + "\n".join(hits[:MAX_MATCHES]),
                True,
                summary,
            )
        return "\n".join(hits), True, summary

    def _list_dir(self, arguments: dict) -> tuple[str, bool, str]:
        raw = arguments.get("path") or "."
        path, why = self._resolve(raw)
        if path is None:
            return why, False, str(raw)
        if not path.exists():
            return f"`{raw}` does not exist.", False, str(raw)
        if not path.is_dir():
            return f"`{raw}` is a file — use `read_file`.", False, str(raw)

        entries = []
        for child in sorted(path.iterdir()):
            if child.name in SKIP_DIRS:
                continue
            if child.is_dir():
                entries.append(f"{child.name}/")
            else:
                entries.append(f"{child.name}  ({child.stat().st_size} bytes)")
        if not entries:
            return f"`{raw}` is empty.", True, str(raw)
        listing = "\n".join(entries[:MAX_ENTRIES])
        if len(entries) > MAX_ENTRIES:
            listing += f"\n... {len(entries) - MAX_ENTRIES} more entries"
        return f"{self._relative(path) or '.'}/\n{listing}", True, str(raw)

    def _outline(self, arguments: dict) -> tuple[str, bool, str]:
        path, why = self._resolve(arguments.get("path"))
        if path is None:
            return why, False, str(arguments.get("path", ""))
        name = str(arguments.get("path", ""))
        if not path.exists() or path.is_dir():
            return f"`{name}` is not a readable file.", False, name
        if path.suffix.lower() != ".py":
            return (
                f"`{name}` is not Python; `outline` reads Python only. Use "
                "`read_file` or `grep`.",
                False,
                name,
            )
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = outline_python(text)
        if not lines:
            return f"`{name}` declares nothing at module level.", True, name
        return f"{name}: {len(text.splitlines())} lines\n" + "\n".join(lines), True, name


def outline_python(source: str) -> list[str]:
    """Every top-level and class-level definition, with its signature.

    Shared with the repository map, which wants exactly this and wants it for
    every file at once. A file that does not parse produces nothing rather than
    raising: half a repository map is worth more than none, and a syntax error
    is the ticket's problem rather than the map's.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    lines: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            lines.append(f"  {node.lineno}: def {node.name}({_signature(node)})")
        elif isinstance(node, ast.ClassDef):
            bases = ", ".join(ast.unparse(base) for base in node.bases)
            lines.append(f"  {node.lineno}: class {node.name}" + (f"({bases})" if bases else ""))
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    lines.append(
                        f"    {child.lineno}: def {child.name}({_signature(child)})"
                    )
                elif isinstance(child, ast.Assign):
                    for target in child.targets:
                        if isinstance(target, ast.Name) and target.id.isupper():
                            lines.append(f"    {child.lineno}: {target.id}")
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    lines.append(f"  {node.lineno}: {target.id}")
    return lines


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """A definition's parameters, as written, without defaults' values."""
    args = node.args
    names = [arg.arg for arg in (*args.posonlyargs, *args.args)]
    if args.vararg:
        names.append(f"*{args.vararg.arg}")
    elif args.kwonlyargs:
        names.append("*")
    names += [arg.arg for arg in args.kwonlyargs]
    if args.kwarg:
        names.append(f"**{args.kwarg.arg}")
    return ", ".join(names)


def _positive(value: Any, fallback: int) -> int:
    """A line number from a model, or the fallback. Never zero or negative."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return number if number > 0 else fallback


def _matches_glob(path: str, glob: str) -> bool:
    """Whether a repo-relative path matches a glob a model wrote.

    Models write `forge/*.py` meaning "Python files under forge", which
    `fnmatch` reads as one path segment, and `*.py` meaning "anywhere". Both
    are answered, because refusing on the difference teaches nothing: the
    pattern is matched against the whole path and against the basename.
    """
    return (
        fnmatch.fnmatch(path, glob)
        or fnmatch.fnmatch(path, f"{glob.rstrip('/')}/*")
        or fnmatch.fnmatch(Path(path).name, glob)
        or fnmatch.fnmatch(path, f"**/{glob}")
    )
