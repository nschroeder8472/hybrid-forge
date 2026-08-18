"""Turn toolchain output into the part that says what went wrong.

A failing `cargo clippy --all-targets` on a mid-sized crate prints 14k–20k
characters, nearly all of it warnings about unrelated code and a trailing
summary. The one line that matters — the `error[E0603]` and its span — is
somewhere in the middle.

The loop used to pass `output[-4000:]` to the next attempt and to the ticket's
failure note. Tail-slicing compiler output is close to the worst possible
choice: compilers print errors first and warnings and summaries last, so the
tail reliably keeps the noise and drops the diagnosis. It also cut mid-token,
which is how a real run recorded its failure as ``s::game::Game` is private``
— the front of `tetris::game::Game` sliced off, leaving a symbol that appears
nowhere in the codebase for the next attempt to reason about.

So: keep the error blocks, drop the warnings, say how much was dropped, and
never cut inside a line.
"""

from __future__ import annotations

import re

# Start of a diagnostic block. Deliberately broad — this runs over cargo, tsc,
# pytest, eslint, go vet, and whatever a user configures, and a pattern that
# misses simply falls back to the tail rather than losing the output.
_ERROR = re.compile(
    r"^(?:"
    r"error(?:\[[A-Z]?\d+\])?\s*[:\[]"          # cargo / rustc
    r"|error\s+TS\d+"                            # tsc
    r"|E\s+\w|FAILED|ERROR\b"                    # pytest
    r"|.*\berror\b\s*(?:TS\d+)?\s*:"             # eslint, generic "path: error:"
    # Rust panics. Matched on `panicked at` wherever it appears in the line
    # rather than on the header's shape: current rustc prints the thread's pid
    # between the name and the verb — `thread 'x' (44792) panicked at ...` —
    # which defeated `thread '.*' panicked` and made every panic invisible.
    # `signatures` is built from these blocks, so the baseline amnesty was
    # comparing empty sets and could not tell a new panic from an old one.
    r"|.*\bpanicked at\b"
    # Python exception headers, which end a traceback and carry the message:
    # `ImportError: cannot import name 'locked'`. Previously only
    # `AssertionError` was recognised, so every other exception ended a run
    # with no diagnostic block parsed at all — and a caller reading blocks got
    # nothing back rather than the error.
    r"|\s*[A-Z]\w*(?:Error|Exception)\b\s*:"
    r"|FAIL\b|✗|×"                               # assorted test runners
    r")",
    re.IGNORECASE,
)

_WARNING = re.compile(r"^\s*warning\s*[:\[]", re.IGNORECASE)

# Continuation of a diagnostic that is not indented. rustc renders source
# snippets with the line number in column zero (`77 |     .board`), and tsc and
# pytest use a leading marker, so an "unindented means new block" rule alone
# throws away the span that says which code is wrong.
_CONTINUATION = re.compile(
    r"^\s*(?:\d+\s*)?\|"                    # rustc / cargo source spans
    r"|^\s*[-=]+>"                          # `-->` location lines
    r"|^\s*(?:help|note|expected|found|caused by|hint)\b"
    r"|^\s*\^+"                             # caret underlines
    # `tests/bug_001_test.py:1: in <module>` — pytest and friends put the
    # location on its own unindented line, which the "unindented ends the
    # block" rule threw away along with the only mention of the file. A new
    # diagnostic is still checked for first, so an eslint `path.js: error: …`
    # opens its own block rather than being swallowed here.
    r"|^\s*[\w./\\+-]+\.[A-Za-z0-9]{1,5}:\d+",
    re.IGNORECASE,
)

# A line that ends a block without starting a new one: blank, or a summary the
# tools print after the diagnostics are done.
_SUMMARY = re.compile(
    r"^\s*(?:warning|error)?:?\s*"
    r"(?:\d+ (?:warning|error)s? emitted"
    r"|could not compile"
    r"|test result:"
    r"|Compiling|Checking|Finished|Running)\b",
    re.IGNORECASE,
)


def _blocks(lines: list[str]) -> tuple[list[list[str]], int]:
    """Split output into diagnostic blocks, returning (error blocks, warnings).

    A block runs from its opening line until the next top-level diagnostic or
    a blank line at column zero — which is how rustc, tsc, and pytest all
    delimit them, so indented continuation lines (spans, notes, help) stay
    attached to the diagnostic they explain.
    """
    errors: list[list[str]] = []
    warnings = 0
    current: list[str] | None = None
    keeping = False

    for line in lines:
        starts_error = bool(_ERROR.match(line)) and not _SUMMARY.match(line)
        starts_warning = bool(_WARNING.match(line))

        if starts_error or starts_warning:
            if keeping and current:
                errors.append(current)
            keeping = starts_error
            current = [line] if starts_error else None
            if starts_warning:
                warnings += 1
            continue

        if not keeping:
            continue
        # A block ends at an unindented line that is not one of the compiler's
        # own continuation renderings.
        if line.strip() and not line[:1].isspace() and not _CONTINUATION.match(line):
            if current:
                errors.append(current)
            current, keeping = None, False
            continue
        if current is not None:
            current.append(line)

    if keeping and current:
        errors.append(current)
    return errors, warnings


# Fragments that differ between two runs of the *same* failing build: cargo's
# per-target hashes, process ids in panic headers, addresses, timings.
_VOLATILE = re.compile(
    r"-[0-9a-f]{8,}\b"                 # cargo target hashes
    r"|\b0x[0-9a-f]+\b"                # addresses
    r"|\b[0-9a-f]{16,}\b"              # bare hashes
    r"|\(\d+\)"                        # pids in rust panic headers
    r"|\b\d+(?:\.\d+)?m?s\b",          # durations
    re.IGNORECASE,
)


def signatures(output: str) -> set[str]:
    """Stable identifiers for the distinct errors in one tool's output.

    The loop compares the errors a ticket's verify step produced against the
    errors that were already failing before it started, so it can tell "you
    broke this" from "this was broken when you got here". That comparison has
    to survive a rebuild: cargo renames `wasm_layer-bd0673e6c1e4e95f` on every
    invocation and rust stamps a pid into every panic header, so comparing raw
    text would call every pre-existing failure new and blame the current ticket
    for a file it is not even allowed to open.

    An empty result means nothing parsed as a diagnostic. Callers must treat
    that as "cannot attribute" rather than "no errors" — a set difference
    against an unparseable failure would silently forgive it.
    """
    found: set[str] = set()
    blocks, _ = _blocks((output or "").splitlines())
    for block in blocks:
        key = _block_key(block)
        if key:
            found.add(key)
    return found


def _block_key(block: list[str]) -> str:
    """One diagnostic block reduced to a stable identifier.

    The opening line says what went wrong; the `-->` span says where. One
    without the other collapses distinct errors together: rustc emits the same
    `unresolved import` head once per test target.

    Shared by `signatures` and `files_blamed` so the two agree on what counts
    as the same error. They must: `files_blamed` is asked which files a
    ticket's *own* failures name, and it answers that by excluding the
    signatures the baseline already had.
    """
    head = block[0].strip()
    where = next(
        (line.strip() for line in block[1:] if line.strip().startswith("-->")), ""
    )
    # Tools that do not draw rustc's arrow still put the location somewhere: on
    # the head line (javac, tsc, go), or on a line of its own below it (pytest's
    # `tests/test_scan.py:12: in <module>`). Taking the first line that carries
    # one keeps the key distinct per file and — because `_signature_scope` reads
    # the key, not the block — is what lets the location be found at all. Two
    # pytest failures reading `AssertionError: assert 1 == 2` in different files
    # collapsed into one signature otherwise, and neither could be attributed.
    if not where and not locations(head):
        where = next(
            (line.strip() for line in block[1:] if locations(line)), ""
        )
    return re.sub(r"\s+", " ", _VOLATILE.sub("#", f"{head} {where}")).strip().lower()


def distill(output: str, *, limit: int = 6000) -> str:
    """Reduce toolchain output to its diagnostics, newest information first.

    Falls back to the *head* of the output when nothing parses as a
    diagnostic: an unrecognized tool still tends to lead with its complaint,
    and the tail is where the noise lives.
    """
    text = (output or "").strip()
    if not text:
        return ""
    if len(text) <= limit:
        return text

    lines = text.splitlines()
    errors, warnings = _blocks(lines)

    if not errors:
        return _clip_lines(lines, limit, note="output truncated")

    kept: list[str] = []
    shown = 0
    for block in errors:
        candidate = "\n".join(block)
        if kept and sum(len(k) + 1 for k in kept) + len(candidate) > limit:
            break
        kept.append(candidate)
        shown += 1

    body = "\n\n".join(kept)
    if len(body) > limit:
        body = _clip_lines(body.splitlines(), limit, note="first error truncated")

    notes = []
    if shown < len(errors):
        notes.append(f"{len(errors) - shown} further error(s) not shown")
    if warnings:
        notes.append(f"{warnings} warning(s) suppressed")
    if notes:
        body += f"\n\n[{'; '.join(notes)}]"
    return body


def _clip_lines(lines: list[str], limit: int, *, note: str) -> str:
    """Take whole lines up to `limit`. Never splits a line.

    Cutting mid-line is what produced a failure note about a symbol
    (`s::game::Game`) that does not exist in the source.
    """
    kept: list[str] = []
    size = 0
    for line in lines:
        if size + len(line) + 1 > limit:
            break
        kept.append(line)
        size += len(line) + 1
    if len(kept) < len(lines):
        kept.append(f"[{note}: {len(lines) - len(kept)} more line(s)]")
    return "\n".join(kept)


# A file location inside a diagnostic. Deliberately loose about the path shape,
# because this runs over every toolchain a user can configure; the caller checks
# the path against the repository before trusting it.
#
# Two spellings, because compilers do not agree on one:
#
#     rustc / javac / gcc / go / pytest    path/File.java:33:7
#     tsc / msvc                           path/File.ts(33,7)
#
# An optional drive letter is part of the path. Without it `d:\src\a.java` is
# captured as `\src\a.java`, which no longer shares a prefix with the repository
# root and so can never be recognised as a file inside it.
#
# The extension is what keeps prose out: `error: expected 2, found 1` holds no
# `word.ext:line`. A path carrying a separator is accepted without one too, so
# `build/Makefile:12` is a location while a bare `Makefile:12` is not -- the
# conservative direction, since a false location blames a ticket for a file it
# may have no authority to touch.
#
# The drive letter may not follow a word character. Kotlin opens its
# diagnostics `e: file:///repo/src/Main.kt:9:5`, and without the guard the `e`
# ending `file` is read as a drive, yielding `e:///repo/src/Main.kt` — a path
# matching nothing, which silently excuses every Kotlin error.
_LOCATION = re.compile(
    r"((?<![\w])(?:[A-Za-z]:)?[\w./\\+-]*(?:\.[A-Za-z0-9]{1,5}|[/\\][\w+-]+))"
    r"(?::(\d+)(?::\d+)?|\((\d+)(?:,\d+)?\))"
)

# `file:///repo/src/Main.kt` (kotlinc, and any tool reporting URIs) carries a
# scheme that no repository path has. Dropped before matching so the location
# underneath is an ordinary absolute path.
_FILE_URI = re.compile(r"\bfile://", re.IGNORECASE)


def locations(text: str) -> list[str]:
    """Every file path this diagnostic text names, in forward-slash form.

    Every attribution in the loop reduces to one question -- which file is this
    complaint about -- and it has to be answered the same way for every
    toolchain. It was not: scope matching read locations out of rustc's `-->`
    marker alone, so cargo was attributed correctly while javac, tsc, go, and
    pytest, none of which emit `-->`, parsed to nothing at all.

    A signature that parses to nothing is unattributable and therefore
    excusable. That is the safe direction for one diagnostic and a catastrophe
    for a whole language: every failure a Java run produced was excused as
    somebody else's, tickets passed their verify step on a tree that did not
    compile, and the errors accumulated across seven tickets -- 3, then 7, then
    13, then 20 -- each cycle's baseline laundering the previous cycle's
    breakage into "pre-existing".

    Order is preserved and duplicates dropped, so the first location in a
    diagnostic -- the one compilers put the error at -- is checked first.
    """
    found: list[str] = []
    for match in _LOCATION.finditer(_FILE_URI.sub("", text or "")):
        path = match.group(1).replace("\\", "/")
        if path and path not in found:
            found.append(path)
    return found


def files_blamed(output: str, exclude: set[str] | None = None) -> dict[str, list[str]]:
    """Files the failures in this output point at, with the lines naming them.

    `signatures` answers "is this the same error as before"; this answers "whose
    code is it in". A bug ticket needs the second question: its fix can be
    correct and complete and still fail the suite, because an *older test*
    asserts the behavior the report calls a bug. Distinguishing that from an
    ordinary regression is the difference between a ticket a human can settle in
    a minute and five attempts spent oscillating.

    `exclude` holds the signatures that were already failing before the ticket
    started, and dropping them is what makes the answer mean anything. Without
    it every red file in a repository looks like it is about the ticket in
    hand: a run asked whether a *level* bug was contradicted by an assertion
    and was told yes by `tests/tt_001_test.rs`, which is about piece geometry
    and had been failing since before that ticket was filed.

    Only lines inside a diagnostic block count, so a passing test that happens
    to mention a filename does not implicate it.
    """
    blamed: dict[str, list[str]] = {}
    blocks, _ = _blocks((output or "").splitlines())
    for block in blocks:
        if exclude and _block_key(block) in exclude:
            continue
        for line in block:
            for match in _LOCATION.finditer(line):
                path = match.group(1).replace("\\", "/")
                entry = line.strip()
                lines = blamed.setdefault(path, [])
                if entry not in lines:
                    lines.append(entry)
    return blamed


def errors_naming(text: str, path: str) -> list[str]:
    """Lines of a verify failure that name `path`, with their message.

    Compilers report the message on one line and the location on the next
    (`error: unused variable: \x60x\x60` / `  --> tests\tt_001_test.rs:67:10`),
    so a location line is returned together with whatever introduced it.

    The tester's file is the only one it can change, and it is outside every
    other role's scope. A style error there fails the ticket for as long as the
    tester keeps reproducing it — one run spent twelve retry cycles on a single
    unused variable, because the failure it was shown read as evidence about
    the implementation rather than about its own file.

    Read out of the diagnostic blocks rather than the whole output, because
    every test runner announces the targets it is about to run and cargo does
    it by path: `Running tests\\bug_001_test.rs (target\\debug\\deps\\...)`. That
    line appears whether the target passed or failed, so scanning the raw text
    reported a file as implicated in its own success banner. A bug ticket was
    failed fifteen times over it — its own reproduction was passing, another
    ticket's was red, and the amnesty was told the reproduction itself had
    failed and refused to excuse anything.
    """
    if not path:
        return []
    wanted = {path.replace("\\", "/").lower(), path.replace("/", "\\").lower()}
    blocks, _ = _blocks(text.splitlines())
    lines = [line for block in blocks for line in block]
    found: list[str] = []
    for index, raw in enumerate(lines):
        lowered = raw.lower()
        if not any(name in lowered for name in wanted):
            continue
        # The message this location belongs to, if the line above is one.
        preceding = lines[index - 1].strip() if index else ""
        if preceding and re.match(r"^(error|warning)\b", preceding, re.IGNORECASE):
            entry = f"{preceding}\n  {raw.strip()}"
        else:
            entry = raw.strip()
        if entry not in found:
            found.append(entry)
    return found
