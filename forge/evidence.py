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
from collections import Counter
from pathlib import Path
from typing import Sequence

from .patch import normalize_path

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


# Directories and extensions a walk must skip when git is not there to say so.
# Everything here is either generated, vendored, or not text.
_SKIP_DIRS = frozenset(
    """.git .hg .svn node_modules target dist build out vendor .venv venv
    __pycache__ .mypy_cache .pytest_cache .ruff_cache .tox .next .idea
    .gradle .terraform coverage htmlcov""".split()
)
_SKIP_SUFFIXES = frozenset(
    """.pyc .pyo .so .dylib .dll .exe .bin .o .a .class .jar .zip .gz .tar .rar
    .7z .png .jpg .jpeg .gif .bmp .ico .webp .pdf .mp3 .mp4 .mov .wav .ttf
    .woff .woff2 .eot .wasm .lock .db .sqlite""".split()
)
# Big enough for any source file; past it, it is data.
MAX_SCAN_BYTES = 512_000


def repo_files(root: Path, limit: int = MAX_FILES) -> list[str]:
    """Every source file in the project, tracked or not.

    Untracked matters more here than anywhere else in this codebase. A project
    the loop has just built is *entirely* untracked — `autoCommit` is off by
    default, which is the right default and means the work sits in the tree
    uncommitted. `git ls-files` alone reports nothing about it, so the first
    bug report filed against fresh work reached the planner with an empty file
    list and came back "no repository evidence was provided". The report was
    fine; the search never looked at the code.

    So: tracked files, plus untracked ones git does not ignore, and a plain
    walk when there is no git at all. The walk skips the directories a
    `.gitignore` would have — a listing of `node_modules` is not evidence, and
    it would crowd out everything that is.
    """
    found = _git(root, "ls-files")
    found += _git(root, "ls-files", "--others", "--exclude-standard")
    if found:
        return sorted(dict.fromkeys(found))[:limit]
    return _walk(root, limit)


def _walk(root: Path, limit: int = MAX_FILES) -> list[str]:
    """The project's files without git's help. Never raises."""
    found: list[str] = []
    try:
        for path in sorted(root.rglob("*")):
            if len(found) >= limit:
                break
            if not path.is_file() or path.suffix.lower() in _SKIP_SUFFIXES:
                continue
            relative = path.relative_to(root)
            if any(part in _SKIP_DIRS for part in relative.parts):
                continue
            found.append(relative.as_posix())
    except OSError:
        return found
    return found


def _read(root: Path, path: str) -> str:
    """One file's text, or "" if it is not text this can search."""
    try:
        candidate = root / path
        if candidate.stat().st_size > MAX_SCAN_BYTES:
            return ""
        return candidate.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _scan(root: Path, files: list[str], match, limit: int) -> list[str]:
    """Search the project without git, as `path:line: text`.

    The fallback under both searches below. Slower than `git grep`, and
    unbothered by whether a file has ever been committed — which is the case
    that matters, because a bug is usually reported against work still sitting
    in the tree.
    """
    found: list[str] = []
    for path in files:
        for number, line in enumerate(_read(root, path).splitlines(), start=1):
            if len(found) >= limit:
                return found
            if match(line):
                found.append(f"{path}:{number}: {line.strip()[:LINE_LIMIT]}")
    return found


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


# Words that carry no location. A report is mostly these, and grepping them
# returns every file in the project, which is the same as returning none.
_STOPWORDS = frozenset(
    """a about after again all also always am an and any are as at back be
    because been before being between both but by can cannot could did do does
    doing done down during each either else even ever every few first for from
    get gets getting go goes going got had happen happens has have having he
    her here hers him his how i if in into is it its just keep like little look
    looks made make makes many may maybe me more most much must my never new no
    nor not now of off often on once one only or other our out over put
    really same saw say says see seems seen she should show shows since so some
    something sometimes still such sure take than that the their them then
    there these they thing things this those though through time to too try
    trying two under until up us use used using very want was way we well were
    what when where whether which while who why will with without work works
    would you your""".split()
)

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]{2,}")


def prose_terms(report: str, limit: int = MAX_TERMS) -> list[str]:
    """Content words from a report that named nothing specific.

    The report a person actually files is "the score sometimes stops updating",
    with no identifier, no path and nothing in backticks. `terms` finds nothing
    in it, and a planner handed only a file tree is picking by filename.

    Domain words survive into code — a score is usually near something called
    `score` — so the words themselves are worth grepping even though they are
    prose. Ordered by how often the report repeats them, because a word the
    reporter used three times is what the report is about.
    """
    counts: Counter[str] = Counter()
    for match in _WORD.finditer(report):
        word = match.group(0).lower()
        if word not in _STOPWORDS:
            counts[word] += 1
    return [word for word, _ in counts.most_common(limit)]


# What a definition looks like in the languages this loop is likely to meet,
# written as POSIX ERE because `git grep -E` is what runs it. Deliberately
# shallow: this is an index for a model to read, not a parser, and a wrong
# index is worse than a thin one.
_DEFINITION = (
    r"^[[:space:]]*(pub |export |public |private |protected |static |async )*"
    r"(def|fn|func|function|class|struct|enum|trait|interface|impl|type)"
    r"[[:space:]]+[A-Za-z_]"
)

# The same rule in Python spelling, for the scan that runs when git cannot.
_PY_DEFINITION = re.compile(
    r"^\s*(?:pub\s+|export\s+|public\s+|private\s+|protected\s+|static\s+|async\s+)*"
    r"(?:def|fn|func|function|class|struct|enum|trait|interface|impl|type)\s+[A-Za-z_]"
)

MAX_SYMBOLS = 300

# Extensions where a line matching the definition pattern is prose or markup
# rather than a definition.
_NOT_SOURCE = frozenset(
    """.md .markdown .txt .rst .json .yaml .yml .toml .ini .cfg .lock .csv
    .html .xml .svg .css .scss""".split()
)


def symbol_index(root: Path, limit: int = MAX_SYMBOLS) -> list[str]:
    """Every definition line in tracked source, as `path:line: text`.

    The bridge between a report's words and the code's names. "The score stops
    updating" does not match `fn commit_lines`, but a reader — including a
    model — can see that `commit_lines` is where lines and score meet. Grepped
    rather than parsed, because a wrong index is worse than a shallow one.
    """
    # `--untracked` for the same reason `repo_files` asks for it: the work a
    # bug is reported against has usually not been committed yet.
    lines = _git(root, "grep", "-n", "-I", "-E", "--untracked", "--", _DEFINITION)
    if not lines:
        lines = _scan(root, repo_files(root), _PY_DEFINITION.match, limit * 4)

    found = []
    for line in lines:
        path = line.split(":", 1)[0]
        # `class` and `type` are ordinary words in prose and markup. Filtered
        # here rather than by pathspec so the same call works whatever the
        # project is written in.
        if Path(path).suffix.lower() in _NOT_SOURCE:
            continue
        found.append(line[:LINE_LIMIT])
        if len(found) >= limit:
            break
    return found


# Files that exist to re-export other files. Naming one as a ticket's scope is
# almost always a mis-scope: there is no behavior in it to fix, and the code
# the report is about is in a sibling.
_MODULE_LIST_NAMES = frozenset(
    """lib.rs mod.rs __init__.py index.js index.ts index.mjs mod.ts""".split()
)

# A line that re-exports rather than implements, or is not code at all.
_REEXPORT = re.compile(
    r"^\s*(?:(?:pub(?:\s*\([^)]*\))?\s+)?(?:mod|use|export|import|from|require)\b"
    r"|[#}\]);]|//|/\*|\*)"
)

# How many files a widened read scope may add. Enough to cover a module and its
# neighbours; not so many that the real scope is lost in a directory listing.
MAX_READING = 12


def is_module_list(text: str, name: str) -> bool:
    """Whether this file only re-exports, so there is nothing in it to fix.

    Judged on the contents rather than the name alone. A `__init__.py` that
    holds real code is a legitimate thing to scope a ticket to; one holding
    four import lines is not, whatever it is called.

    This is why a run spent seven retry cycles going nowhere. A bug ticket was
    scoped to `src/lib.rs`, which in that crate is four `pub mod` lines and 62
    bytes, and both the executor and the tester were shown that file and
    nothing else. The executor's final answer was that the struct it had been
    told to fix "is likely defined in `src/game.rs` ... outside the allowed
    scope I'm permitted to modify", which was exactly right.
    """
    if name.lower() not in _MODULE_LIST_NAMES:
        return False
    lines = [line for line in text.splitlines() if line.strip()]
    return bool(lines) and all(_REEXPORT.match(line) for line in lines)


def reading_scope(
    root: Path,
    allowed: Sequence[str],
    reference: Sequence[str] = (),
    extra: Sequence[str] = (),
    limit: int = MAX_READING,
) -> list[str]:
    """What a ticket should be allowed to read, given what it may write.

    Reading and writing are not the same permission and were being granted as
    though they were. A ticket that may write one file was shown that one file,
    so the role holding it could not check a call against the function it
    calls, could not see the type it has to stay consistent with, and could not
    tell whether the cause it was given was even the right cause. Widening what
    may be *read* costs a prompt some tokens; widening what may be *written* is
    the thing worth being strict about, and is untouched here.

    Three sources, in descending order of how much they are worth:

    - The modules a module-list file declares. `src/lib.rs` naming `pub mod
      game` is a direct pointer at `src/game.rs`, and following it is the
      difference between showing a role 62 bytes and showing it the code.
    - `extra`, which the caller already has reason to believe is relevant —
      for a bug ticket, the files the report's own words grepped to.
    - Source siblings in the same directory. A fix almost always has to stay
      consistent with the module next to it.

    Everything is filtered to files that exist and are not already writable,
    and the whole thing is capped: a read scope of forty files is a directory
    listing, and the role stops reading any of it carefully.
    """
    writable = {normalize_path(path) for path in allowed}
    picked: list[str] = []
    seen: set[str] = set()

    def take(path: str) -> None:
        key = normalize_path(path)
        if key in writable or key in seen or len(picked) >= limit:
            return
        if not (root / path).is_file():
            return
        seen.add(key)
        picked.append(path)

    for path in reference:
        take(path)

    # Declared modules first — the one pointer in this function that is a fact
    # about the code rather than a guess from the directory.
    for path in allowed:
        if any(character in path for character in "*?["):
            continue
        candidate = root / path
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not is_module_list(text, candidate.name):
            continue
        for name in sorted(set(_WORD.findall(text))):
            for sibling in candidate.parent.glob(f"{name}.*"):
                if sibling.is_file() and sibling.suffix.lower() not in _NOT_SOURCE:
                    take(str(sibling.relative_to(root)).replace("\\", "/"))
            nested = candidate.parent / name / candidate.name
            if nested.is_file():
                take(str(nested.relative_to(root)).replace("\\", "/"))

    for path in extra:
        take(path)

    for path in allowed:
        if any(character in path for character in "*?["):
            continue
        parent = (root / path).parent
        suffix = Path(path).suffix.lower()
        if not suffix or not parent.is_dir():
            continue
        for sibling in sorted(parent.iterdir()):
            if sibling.is_file() and sibling.suffix.lower() == suffix:
                take(str(sibling.relative_to(root)).replace("\\", "/"))

    return picked


def paths_named(root: Path, text: str, limit: int = 4) -> list[str]:
    """Repo-relative paths mentioned in prose that actually exist on disk.

    For reading a role's own words back. When the executor gives up with
    `BLOCKED: the Game struct is likely defined in src/game.rs ... outside the
    allowed scope I'm permitted to modify`, it has said precisely what it
    needs, in a sentence nothing was reading.

    Existence is the filter that makes this safe to act on. A model naming a
    file it invented gets nothing; a model naming a file in the repository is
    pointing at something checkable.
    """
    found: list[str] = []
    seen: set[str] = set()
    for match in _PATHLIKE.findall(text or ""):
        path = normalize_path(match.strip("`'\"(),;"))
        if path in seen or path.startswith(("/", "\\")) or ".." in path:
            continue
        seen.add(path)
        candidate = root / path
        try:
            if not candidate.is_file() or root.resolve() not in candidate.resolve().parents:
                continue
        except OSError:
            continue
        found.append(path)
        if len(found) >= limit:
            break
    return found


def read_files(root: Path, paths: list[str], per_file: int = 12_000) -> dict[str, str]:
    """Contents of the files a survey pass asked to see. Never raises.

    Bounded per file, because this goes into a prompt beside the report and the
    tree. A file too long to show whole is shown from the top: the definitions
    a reader is looking for are rarely at the bottom.
    """
    found: dict[str, str] = {}
    for path in paths:
        candidate = (root / path).resolve()
        try:
            if not candidate.is_file() or root.resolve() not in candidate.parents:
                continue
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if len(text) > per_file:
            text = text[:per_file] + f"\n[truncated at {per_file} characters]\n"
        found[path] = text
    return found


def hits(root: Path, term: str, limit: int = MAX_HITS) -> list[str]:
    """Where a term appears in tracked source, as `path:line: text`.

    Fixed-string, case-insensitive, and capped. The planner needs enough to
    tell `src/game.rs` from `src/board.rs`, not a concordance.
    """
    lines = _git(root, "grep", "-n", "-I", "-i", "-F", "--untracked", "--", term)
    if not lines:
        wanted = term.lower()
        lines = _scan(root, repo_files(root), lambda line: wanted in line.lower(), limit)
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

    files = repo_files(root)
    if files:
        listing = "\n".join(files)
        note = (
            f"\n[{MAX_FILES} of them; the repository has more]"
            if len(files) >= MAX_FILES
            else ""
        )
        sections.append(f"### Files in this repository\n{listing}{note}")

    named = terms(report)
    matched = _matches(root, named)

    # A report that named nothing specific is the ordinary case — "the score
    # sometimes stops updating" has no identifier, no path, nothing in
    # backticks. Falling back to its content words finds the domain: a score is
    # usually near something called `score`. Only when the specific terms found
    # nothing, because a report that *did* name a symbol has already told us
    # more than any word frequency will.
    if not matched:
        matched = _matches(root, prose_terms(report))

    if matched:
        sections.append(
            "### Where the report's own words appear in the code\n"
            + "\n\n".join(matched)
        )

    # The bridge from a report's words to the code's names, and worth its space
    # exactly when the words themselves did not land: "the score stops
    # updating" matches no line in a codebase that calls it `commit_lines`, but
    # a reader can see where lines and score meet.
    if not named:
        symbols = symbol_index(root)
        if symbols:
            sections.append(
                "### Every definition in this repository\n" + "\n".join(symbols)
            )

    return "\n\n".join(sections)


def _matches(root: Path, wanted: list[str]) -> list[str]:
    found = []
    for term in wanted:
        lines = hits(root, term)
        if lines:
            found.append(f"#### `{term}`\n" + "\n".join(lines))
    return found


# How much of a repository is searched for a file named somewhere it is not.
# Larger than `MAX_FILES`, which caps what goes into a prompt — this only has
# to answer "is there exactly one file with that name", and a listing that
# stops early answers it wrongly.
MAX_LOCATE = 20_000


def locate_named(root: Path, path: str, files: Sequence[str] | None = None) -> str:
    """Where a file that does not exist at `path` actually lives, if anywhere.

    A planner rewriting a ticket names the files the executor has to read, and
    it names them from memory: it is shown their contents, not the tree. So it
    writes `src/main/java/com/plexnamer/DirectoryScanner.java` for a class that
    sits in `.../plexnamer/domain/`, and the path is silently unreadable —
    `sources_for` skips what it cannot open, the executor is shown nothing, and
    it guesses the package. One run spent five attempts and a whole retry
    budget importing `com.plexnamer.DirectoryScanner`, a symbol that has never
    existed, because the only correction available to it was the compiler
    saying so again.

    Matched on the filename alone, and only when exactly one file in the
    repository carries it. That is the case where the intent is not in doubt:
    the planner named a real file and put it in the wrong directory. Two
    matches is a guess about which one was meant, and the caller is better off
    told the path is wrong than pointed at a coin flip.
    """
    name = Path(normalize_path(path)).name
    if not name:
        return ""
    pool = repo_files(root, limit=MAX_LOCATE) if files is None else files
    found = [candidate for candidate in pool if candidate.rsplit("/", 1)[-1] == name]
    return found[0] if len(found) == 1 else ""
