"""Find imports that point at nothing.

A model writing one file of a larger design will import the rest of it. That is
correct behaviour and usually harmless — the module it names is the one the next
ticket writes. It stops being harmless when *no* ticket writes it, because
nothing else in the loop notices: the file lands, the apply step succeeds, the
verify commands do not run that language or do not exist yet, the reviewer reads
a diff that looks right, and the ticket goes green over code that cannot be
loaded.

One run did that fifteen times. Sixteen imports across eight invented module
paths — `../types`, `../geometry`, `../model/rect`, `../models/level_model` —
each ticket reaching for a shared module it had been told to use and was not
allowed to create, and no ticket anywhere owning it. The backlog finished
`done`. Nothing in it had ever been compiled.

The check is a regex and a `stat`. It needs no toolchain, no model, and no
ecosystem knowledge beyond one pattern per language family, which is what makes
it worth running before anything expensive: it would have failed that run's
first ticket on its first attempt.

**Only relative imports.** A bare specifier is a package (`react`, `serde`) or
resolves through configuration this cannot see — a `tsconfig` path alias, a
`PYTHONPATH`, an include directory. Guessing about those produces false
failures, and a false failure here costs an attempt and tells the executor to
fix code that was never broken.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Sequence
from pathlib import Path

# Extensions a missing module can be spelled with, per language family. Tried
# in order against a target that names no extension of its own, which is the
# normal way TypeScript and JavaScript are written.
_CANDIDATE_SUFFIXES: dict[str, tuple[str, ...]] = {
    ".ts": (".ts", ".tsx", ".d.ts", ".js", ".jsx", ".mjs", ".cjs", ".json"),
    ".tsx": (".tsx", ".ts", ".d.ts", ".js", ".jsx", ".mjs", ".cjs", ".json"),
    ".mts": (".mts", ".ts", ".d.ts", ".mjs", ".js"),
    ".cts": (".cts", ".ts", ".d.ts", ".cjs", ".js"),
    ".js": (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".json"),
    ".jsx": (".jsx", ".js", ".tsx", ".ts", ".mjs", ".cjs", ".json"),
    ".mjs": (".mjs", ".js", ".ts", ".mts"),
    ".cjs": (".cjs", ".js", ".ts", ".cts"),
}

# Where a directory import lands. `index` for the JavaScript family, and the
# reason a bare directory target is not automatically a miss.
_INDEX_STEMS = ("index",)

_JS_FAMILY = frozenset(_CANDIDATE_SUFFIXES)
_C_FAMILY = frozenset({".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".hh"})

# `from '…'`, `require('…')`, `import('…')`, `export … from '…'`. One pattern,
# because every spelling ends in a quoted specifier and the quoting is the part
# that matters.
_JS_IMPORT = re.compile(
    r"""(?:\bfrom\s*|\brequire\s*\(\s*|\bimport\s*\(\s*|\bimport\s+)
        (['"])(?P<target>\.{1,2}/[^'"]*)\1""",
    re.VERBOSE,
)

# `mod x;` — with the semicolon. `mod x { … }` is an inline module and names no
# file at all, so a pattern without the semicolon reports every one of them.
_RUST_MOD = re.compile(r"^\s*(?:pub(?:\s*\([^)]*\))?\s+)?mod\s+([A-Za-z_]\w*)\s*;", re.MULTILINE)

# `import "./x"` — legal in Go and rare enough that the cost of the pattern is
# the pattern.
_GO_IMPORT = re.compile(r'"(?P<target>\.{1,2}/[^"]*)"')

# `#include "x.h"`. The angle-bracket form searches the include path and is not
# this module's business.
_C_INCLUDE = re.compile(r'^\s*#\s*include\s*"(?P<target>[^"]+)"', re.MULTILINE)

_LINE_COMMENT = re.compile(r"(?m)//.*$")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def strip_comments(text: str) -> str:
    """Remove the comments a C-family language writes.

    A commented-out import names nothing and must not be reported: the whole
    value of this check is that a failure it produces is worth acting on.

    Deliberately crude. It will strip a `//` inside a string literal, which
    loses an import that was never going to be found in a string anyway, and
    that is the safe direction — this can only ever miss a real miss, never
    invent one.
    """
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", text))


def targets(path: str, text: str) -> list[str]:
    """Every relative import in one file, as written.

    Python is read with `ast` rather than a pattern, because the standard
    library will parse it exactly and a regex will find `from .` inside a
    docstring. A file that does not parse yields nothing: it has a worse
    problem than an unresolved import, and the toolchain will say so.
    """
    suffix = Path(path).suffix.lower()
    if suffix == ".py":
        return _python_targets(text)
    if suffix in _JS_FAMILY:
        return [m.group("target") for m in _JS_IMPORT.finditer(strip_comments(text))]
    if suffix == ".rs":
        return [f"mod:{name}" for name in _RUST_MOD.findall(strip_comments(text))]
    if suffix == ".go":
        return [m.group("target") for m in _GO_IMPORT.finditer(strip_comments(text))]
    if suffix in _C_FAMILY:
        return [m.group("target") for m in _C_INCLUDE.finditer(strip_comments(text))]
    return []


def _python_targets(text: str) -> list[str]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:
            # `from . import x` has no module, and each imported name is its own
            # candidate module — `x.py` or `x/__init__.py`.
            if node.module:
                found.append("." * node.level + node.module)
            else:
                found.extend("." * node.level + alias.name for alias in node.names)
    return found


def candidates(path: str, target: str) -> list[str]:
    """Every repo-relative path `target` could legitimately mean.

    Empty when the target is not this module's business — a bare package
    specifier, or a language with no rule here. An empty list is never a miss.
    """
    directory = Path(path).parent
    suffix = Path(path).suffix.lower()

    if target.startswith("mod:"):
        name = target[4:]
        # A `mod` declaration resolves beside the file, or into a directory
        # named for it. `lib.rs` and `main.rs` root a crate, and `mod.rs` roots
        # a directory, but all three sit in the directory the child belongs to,
        # so one rule covers them.
        stem = Path(path).stem
        base = directory if stem in ("lib", "main", "mod") else directory / stem
        return [
            _posix(base / f"{name}.rs"),
            _posix(base / name / "mod.rs"),
            # Rust 2015 laid children beside their parent. Accepted rather than
            # argued with: this is a check for files nobody wrote, not a
            # linter for edition style.
            _posix(directory / f"{name}.rs"),
            _posix(directory / name / "mod.rs"),
        ]

    if suffix == ".py":
        level = len(target) - len(target.lstrip("."))
        module = target[level:]
        base = directory
        for _ in range(level - 1):
            base = base.parent
        parts = module.split(".") if module else []
        stem = base.joinpath(*parts) if parts else base
        return [
            _posix(stem.with_suffix(".py")) if parts else "",
            _posix(stem / "__init__.py"),
            # A relative `from .x import y` where `y` is the module and `x` the
            # package is spelled `from .x import y` too, so the parent package
            # existing is enough to say this names something real.
            _posix(base / "__init__.py") if parts else "",
        ]

    if suffix in _JS_FAMILY:
        base = _normalize(directory / target)
        found = [_posix(base)]
        if not Path(target).suffix:
            found += [f"{_posix(base)}{ext}" for ext in _CANDIDATE_SUFFIXES[suffix]]
        found += [
            f"{_posix(base)}/{stem}{ext}"
            for stem in _INDEX_STEMS
            for ext in _CANDIDATE_SUFFIXES[suffix]
        ]
        return found

    if suffix == ".go":
        # A Go import names a package directory, and any file in it will do.
        return [_posix(_normalize(directory / target))]

    if suffix in _C_FAMILY:
        return [_posix(_normalize(directory / target))]

    return []


def _normalize(path: Path) -> Path:
    """Collapse `..` without touching the filesystem."""
    parts: list[str] = []
    for part in path.as_posix().split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return Path(*parts) if parts else Path(".")


def _posix(path: Path) -> str:
    return path.as_posix()


def unresolved(
    root: Path,
    written: Sequence[str],
    known: Iterable[str] = (),
    read: Sequence[str] = (),
) -> list[tuple[str, str]]:
    """`(file, import)` for every relative import that names nothing.

    A target counts as resolved when any of its candidate spellings is a file
    on disk, or is a path some ticket in this backlog is allowed to write.
    The second is why `known` exists: a module the next ticket creates is a
    declared future file, not a mistake, and failing an attempt over it would
    make a correct plan unrunnable.

    `read` names files whose content this call should take from disk anyway —
    used for nothing today, and present so a caller can check a file it did not
    just write without loading it twice.

    A directory that exists satisfies a target too. Go imports name a package
    directory rather than a file, and a JavaScript target resolving to a folder
    is an `index` import whose exact spelling this does not need to guess.
    """
    owned = {str(path).replace("\\", "/").lstrip("./") for path in known}
    misses: list[tuple[str, str]] = []
    for path in list(written) + list(read):
        relative = str(path).replace("\\", "/")
        absolute = root / relative
        try:
            text = absolute.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for target in targets(relative, text):
            options = [option for option in candidates(relative, target) if option]
            if not options:
                continue
            if any(
                option in owned or (root / option).exists() for option in options
            ):
                continue
            shown = target[4:] if target.startswith("mod:") else target
            if (relative, shown) not in misses:
                misses.append((relative, shown))
    return misses
