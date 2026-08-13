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
    found = []
    for line in _git(root, "grep", "-n", "-I", "-E", "--", _DEFINITION):
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
