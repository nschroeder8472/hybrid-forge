"""What a build manifest declared before an attempt rewrote it.

The executor returns whole files. That is what makes its output parseable
without a diff format, and it is also the one shape of edit that can delete
something by omission: a file the model reproduces from memory comes back
missing whatever it did not think to copy, and every other check in the loop
passes because the tree it runs against still has the answer.

One run lost this out of a `package.json`:

    "devDependencies": {
      "@types/node": "^22.10.2", "eslint": "^9.17.0", "prettier": "^3.4.2",
      "typescript": "^5.6.3", "typescript-eslint": "^8.19.0", "vitest": "^3.2.7"
    }

Every command still passed. `node_modules/` was already on disk, so lint,
typecheck and the suite all ran exactly as before, the reviewer read a diff
that added two scripts, and the ticket was recorded done. On a clean checkout
`npm ci` installed one package and every one of those commands failed — which
is to say the acceptance criterion "all four exit 0" was false everywhere
except the machine that verified it.

Nothing else in the loop can see this. Verification runs where the dependencies
are already installed, by construction: they are what the commands need to run
at all. So the check has to be on the text of the manifest rather than on any
consequence of it.

Only names are compared, never versions. A ticket that bumps or loosens a
constraint is doing ordinary work; a ticket that drops the entry is not.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:  # `tomllib` is stdlib from 3.11; this package supports 3.10.
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - depends on the interpreter
    tomllib = None  # type: ignore[assignment]

# Where each JSON manifest keeps the things it depends on. Every one of these
# is a mapping of name to constraint, so the names are the keys.
_JSON_SECTIONS: dict[str, tuple[str, ...]] = {
    "package.json": (
        "dependencies",
        "devDependencies",
        "peerDependencies",
        "optionalDependencies",
    ),
    "deno.json": ("imports",),
    "deno.jsonc": ("imports",),
    "composer.json": ("require", "require-dev"),
}

# Where each TOML manifest keeps them, as a path of nested tables. Read with
# `tomllib`, so this costs no dependency. A section may hold a mapping of name
# to constraint or a list of requirement strings, and both are handled.
_TOML_SECTIONS: dict[str, tuple[tuple[str, ...], ...]] = {
    "Cargo.toml": (
        ("dependencies",),
        ("dev-dependencies",),
        ("build-dependencies",),
    ),
    "pyproject.toml": (
        ("project", "dependencies"),
        ("project", "optional-dependencies"),
        ("build-system", "requires"),
        ("tool", "poetry", "dependencies"),
        ("tool", "poetry", "dev-dependencies"),
    ),
}

MANIFESTS: frozenset[str] = frozenset(_JSON_SECTIONS) | frozenset(_TOML_SECTIONS)


def is_manifest(path: str) -> bool:
    """Whether this path is a manifest whose declarations can be compared."""
    return Path(path).name in MANIFESTS


def snapshot(root: Path, paths: list[str]) -> dict[str, str]:
    """The current text of every manifest among `paths` that exists.

    Taken before the edits land, because afterwards the previous declarations
    are gone and git is not a reliable source: a manifest an earlier attempt in
    this same ticket already rewrote has no clean version to diff against.
    """
    found: dict[str, str] = {}
    for path in paths:
        if not is_manifest(path):
            continue
        target = root / path
        try:
            found[path] = target.read_text(encoding="utf-8")
        except OSError:
            continue
    return found


def _names(value: Any) -> set[str]:
    """The dependency names in one section, whichever shape it takes.

    A mapping of name to constraint gives its keys. A list of requirement
    strings — `["vitest>=3", "ruff"]` — gives each entry up to the first
    character that starts a constraint, so a version bump is not read as a
    different dependency.
    """
    if isinstance(value, dict):
        return {str(key) for key in value}
    if isinstance(value, list):
        names = set()
        for entry in value:
            if not isinstance(entry, str):
                continue
            head = entry.strip()
            for stop in ("=", "<", ">", "!", "~", "^", "[", ";", " "):
                head = head.split(stop, 1)[0]
            if head:
                names.add(head)
        return names
    return set()


def _dig(document: Any, path: tuple[str, ...]) -> Any:
    for key in path:
        if not isinstance(document, dict):
            return None
        document = document.get(key)
    return document


def _declared(text: str, filename: str) -> set[str] | None:
    """Every dependency name the manifest declares, or None if unreadable.

    None rather than an empty set, and the caller treats it as "no opinion".
    A manifest that does not parse is a defect the language's own tooling
    reports far better than this can, and guessing that an unparseable file
    declares nothing would turn every syntax error into a dropped-dependency
    complaint pointing at the wrong thing.
    """
    if filename in _JSON_SECTIONS:
        try:
            document = json.loads(text)
        except (ValueError, TypeError):
            return None
        if not isinstance(document, dict):
            return None
        names: set[str] = set()
        for section in _JSON_SECTIONS[filename]:
            names |= _names(document.get(section))
        return names
    if filename in _TOML_SECTIONS:
        # On 3.10 there is no TOML reader in the standard library and this
        # package takes no dependencies, so a Cargo or pyproject manifest reads
        # as "no opinion" there — the same answer an unparseable one gives, and
        # the same consequence: the guard says nothing rather than guessing.
        if tomllib is None:
            return None
        try:
            document = tomllib.loads(text)
        except (tomllib.TOMLDecodeError, ValueError, TypeError):
            return None
        names = set()
        for path in _TOML_SECTIONS[filename]:
            section = _dig(document, path)
            if isinstance(section, dict) and all(
                isinstance(inner, (dict, list)) for inner in section.values()
            ):
                # `optional-dependencies` and poetry's groups nest one level
                # deeper: a mapping of group name to that group's own section.
                for inner in section.values():
                    names |= _names(inner)
                continue
            names |= _names(section)
        return names
    return None


def dropped(before: str, after: str, filename: str) -> list[str]:
    """Dependency names `before` declared that `after` no longer does.

    Empty when nothing was lost, when either version is unreadable, or when
    the file is not a manifest this module knows.
    """
    was = _declared(before, filename)
    now = _declared(after, filename)
    if was is None or now is None:
        return []
    return sorted(was - now)


def losses(root: Path, before: dict[str, str]) -> list[tuple[str, list[str]]]:
    """Every manifest in `before` that now declares fewer dependencies.

    `before` comes from `snapshot`, taken before the edits were applied; the
    current text is read from disk. Returns `(path, dropped names)` pairs, in
    the order the paths were snapshotted, and only for paths that lost
    something.
    """
    found: list[tuple[str, list[str]]] = []
    for path, was in before.items():
        try:
            now = (root / path).read_text(encoding="utf-8")
        except OSError:
            continue
        gone = dropped(was, now, Path(path).name)
        if gone:
            found.append((path, gone))
    return found
