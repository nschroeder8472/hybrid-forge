"""A map of the repository: what is where, without the bodies.

The read tools in `tools.py` answer *show me this*. This answers *what is
there* — the question a role has before it knows what to ask for, and the one
the paste path used to answer by shipping whole files chosen by a guess.

The map is an index, not a payload. For every source file it carries the path,
the definitions in it and their signatures, and nothing else. On this
repository that is roughly 30k characters against the 1.9M a full paste would
be, and it is what makes a read tool cheap: a model that can see

    forge/state.py
      1404: RETRYABLE

reads one file. A model that cannot see it greps four times first, or invents
the API and is rejected for it.

Two properties matter more than completeness:

**It is stable.** The same repository produces the same map, byte for byte,
across every ticket and every role in a run. That is what lets it sit in the
cached prefix — see `prompts.stable_prefix` — where it is paid for once rather
than on all 44 calls of a run.

**It never lies by omission without saying so.** A map cut for budget says
which directories it dropped. A model told the map is complete when it is not
will conclude a file does not exist rather than grepping for it.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .tools import BINARY_SUFFIXES, SKIP_DIRS, outline_python

# What the whole map may cost. Roughly 12k tokens — large for a prompt block
# and small against the alternative, which is a role guessing which of 59 files
# it needs and being handed eleven of the wrong ones.
MAX_MAP_CHARS = 48_000
# Definitions listed per file before the rest are summarised as a count. A file
# with 90 methods is a file the model should open, not one the map should
# transcribe.
MAX_DEFS_PER_FILE = 24
# Files listed per directory before the rest are summarised.
MAX_FILES_PER_DIR = 40
# Extensions whose definitions are worth extracting. Everything else is listed
# by name only — knowing `forge/ui/index.html` exists is the useful part, and
# nothing here parses HTML.
OUTLINED_SUFFIXES = frozenset({".py"})


def repo_map(root: Path, *, limit: int = MAX_MAP_CHARS) -> str:
    """The repository as a map, ready to paste into a prompt.

    Deterministic: directories sorted, files sorted, definitions in source
    order. Two calls on an unchanged tree return identical text, which is the
    property the cached prefix depends on.
    """
    root = Path(root).resolve()
    directories = _by_directory(root)

    blocks: list[str] = []
    used = 0
    dropped: list[str] = []
    for directory, paths in directories:
        block = _render(root, directory, paths)
        if used + len(block) > limit:
            dropped.append(directory)
            continue
        blocks.append(block)
        used += len(block)

    # A map that fitted nothing still says what it left out. Returning an empty
    # string would tell the role this repository has no files in it, which is
    # worse than telling it the map did not fit and to go and look.
    if not blocks and not dropped:
        return ""
    text = "\n".join(blocks)
    if dropped:
        text += (
            "\n[map truncated: no listing for "
            + ", ".join(sorted(dropped)[:12])
            + (" and others" if len(dropped) > 12 else "")
            + ". Those directories exist — use list_dir and grep to read them.]"
        )
    return text


def map_digest(root: Path, *, limit: int = MAX_MAP_CHARS) -> str:
    """A stable identity for one map, for a cache key or a log line."""
    return hashlib.sha256(repo_map(root, limit=limit).encode("utf-8")).hexdigest()[:16]


def _by_directory(root: Path) -> list[tuple[str, list[Path]]]:
    """Every source file, grouped by the directory holding it."""
    grouped: dict[str, list[Path]] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        if path.suffix.lower() in BINARY_SUFFIXES:
            continue
        if path.name.startswith("."):
            continue
        directory = relative.parent.as_posix()
        grouped.setdefault(directory or ".", []).append(path)
    return sorted(grouped.items())


def _render(root: Path, directory: str, paths: list[Path]) -> str:
    """One directory's block: its name, then a line per file."""
    lines = [f"{directory}/" if directory != "." else "./"]
    for path in paths[:MAX_FILES_PER_DIR]:
        lines.append(f"  {path.name}")
        lines.extend(_definitions(root, path))
    if len(paths) > MAX_FILES_PER_DIR:
        lines.append(f"  ... {len(paths) - MAX_FILES_PER_DIR} more files")
    return "\n".join(lines) + "\n"


def _definitions(root: Path, path: Path) -> list[str]:
    """A file's definitions, indented under it, or nothing.

    Unreadable is not an error here. A file that cannot be decoded, or cannot
    be parsed, still belongs on the map by name — the model can open it, and
    the map saying it exists is most of the value.
    """
    if path.suffix.lower() not in OUTLINED_SUFFIXES:
        return []
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found = outline_python(source)
    if not found:
        return []
    kept = [f"  {line}" for line in found[:MAX_DEFS_PER_FILE]]
    if len(found) > MAX_DEFS_PER_FILE:
        kept.append(f"      ... {len(found) - MAX_DEFS_PER_FILE} more definitions")
    return kept
