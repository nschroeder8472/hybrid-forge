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
from collections import Counter
from pathlib import Path

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
    # Gradle and Maven report a JUnit failure as the test's own name with the
    # verdict at the end of the line — `Bug001Test > jar_has_main_class()
    # FAILED` — and put the exception on the indented lines below it. None of
    # the patterns above start there, so a whole Java run parsed to zero
    # diagnostic blocks: no signatures, no blamed files, and `distill` falling
    # back to the head of the output, which on a passing-mostly suite is
    # several thousand characters of `PASSED`. The executor was shown that as
    # the failure it was being asked to fix, and every attribution the loop
    # makes — baseline amnesty, contradiction detection, scope blame — was
    # blind on the language for as long as it has supported it.
    r"|.*\s(?:FAILED|ERROR)\s*$"
    # `\s*` because a runner indents its verdict line. vitest prints
    # ` FAIL  tests/a.test.ts > suite > case` above the assertion, and that
    # header is the only place the failing *file* appears — the
    # `AssertionError:` block below it carries the message and no path at all.
    # Without it every vitest failure parsed to a diagnostic nothing could
    # attribute, on top of being invisible behind its own colour codes.
    r"|\s*(?:FAIL\b|✗|×)"                        # assorted test runners
    r")",
    re.IGNORECASE,
)

_WARNING = re.compile(r"^\s*warning\s*[:\[]", re.IGNORECASE)

# A file extension, for every pattern here that needs to recognise one. The
# lookahead requires a letter in it: an all-digit suffix is not an extension,
# and without that `'127.0.0.1:0'` is the file `127.0.0.1` at line 0. Godot
# prints that when it cannot reach its debugger — on every run — and it was
# recorded as one of the top failure classes of a ticket, 37 times over.
_EXTENSION = r"\.(?=[A-Za-z0-9]{0,4}[A-Za-z])[A-Za-z0-9]{1,5}"

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
    r"|^\s*[\w./\\+-]+" + _EXTENSION + r":\d+",
    re.IGNORECASE,
)

# A line that ends a block without starting a new one: blank, or a summary the
# tools print after the diagnostics are done.
_SUMMARY = re.compile(
    r"^\s*(?:warning|error)?:?\s*"
    r"(?:\d+ (?:warning|error)s? emitted"
    r"|could not compile"
    r"|test result:"
    r"|Compiling|Checking|Finished|Running)\b"
    # Gradle's task banner. `> Task :test FAILED` ends in FAILED and would
    # otherwise open a block under the rule above — a block whose head names no
    # test and whose body is every line until the first real failure.
    r"|^\s*>\s*Task\b"
    # Gradle's tally, `106 tests completed, 1 failed`. It ends in `failed` and
    # names no file, so as a block it is a signature that changes whenever the
    # suite grows — which would make every cycle's evidence look new.
    r"|^\s*\d+ tests? completed\b",
    re.IGNORECASE,
)


# What a tool writes when it thinks it is talking to a terminal: colour, bold,
# cursor moves. Every pattern in this module is anchored at `^` or matches on
# word boundaries, and an escape sequence sits in front of the first character
# of the line — so a runner that colours its output was invisible to all of it.
#
# That is not hypothetical. `vitest` colours every failure, and across an
# 18-hour run not one of its failures parsed: `signatures` returned the empty
# set, so the baseline amnesty compared nothing to nothing; `files_blamed`
# named no file, so nothing could be attributed; and `distill` fell through to
# the head of the output, which is the run banner. The executor was shown the
# banner as the failure it was being asked to fix, on the ticket that went on
# to spend 430 attempts. See docs/CONVERGENCE.md.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")

# Godot's own error format. `_err_print_error` prints the message and then one
# frame naming the file it was raised from:
#
#   ERROR: The remote port number must be between 1 and 65535 (inclusive).
#      at: connect_to_host (core/io/stream_peer_tcp.cpp:69)
#
# Which is the *engine's* C++ source, not the project's. Godot spells every
# project file with the `res://` scheme, so a frame carrying no scheme is the
# runtime talking about itself, and no toolchain this loop has met prints a
# symbol before a parenthesised location in any other context.
#
# It matters because the engine says these things on every run, green ones
# included: a debugger port it was not given, a D3D12 swapchain resize, pages
# still allocated at exit. On the run this comes from they were the top four
# failure classes of a ticket that spent 45 attempts —
# `core/io/stream_peer_tcp.cpp`, `drivers/d3d12/rendering_device_driver_d3d12
# .cpp`, `./core/templates/paged_allocator.h` and `127.0.0.1`, each 37 times —
# so convergence was measured against Godot's startup and the four real
# GDScript parse errors were ranked below it.
_ENGINE_FRAME = re.compile(r"^\s*at:\s+\S.*\([^()]+:\d+\)\s*$")
_PROJECT_SCHEME = re.compile(r"\bres://", re.IGNORECASE)


def strip_ansi(text: str) -> str:
    """Terminal control sequences removed, so the text is what it says.

    Applied where output is captured — see `Orchestrator._shell` — and again at
    the entry to every parser here, because a caller may hand over a detail
    string recorded before this existed.
    """
    return _ANSI.sub("", text or "")


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

    # Done here rather than at each reader, for the reason `_blocks` is shared
    # at all: `signatures`, `classify`, `files_blamed` and `distill` must not
    # disagree about what counts as a diagnostic.
    #
    # Only when something else survives. Noise is only noise when there is
    # signal, and a run whose *sole* evidence is an engine error has nothing to
    # gain from being told nothing failed.
    kept = [trimmed for block in errors if (trimmed := _trim_engine(block))]
    return (kept or errors), warnings


def _is_engine_frame(line: str) -> bool:
    """Whether this line is the runtime naming its own source. See `_ENGINE_FRAME`."""
    return bool(_ENGINE_FRAME.match(line)) and not _PROJECT_SCHEME.search(line)


def _trim_engine(block: list[str]) -> list[str]:
    """The block without the runtime's own frames, or [] if that is all it was.

    Removed line by line rather than block by block, because one Godot error
    is both: `ERROR: Failed to load script "res://tests/theme/x.gd"` names the
    project file that actually failed, and the frame under it names
    `modules/gdscript/gdscript_resource_format.cpp`, which is where Godot's
    loader gave up. Keeping the block and dropping the frame is the only
    reading that attributes it to the right file — with the frame in place the
    engine source was picked as the subject twenty times on one ticket.
    """
    trimmed = [line for line in block if not _is_engine_frame(line)]
    if len(trimmed) == len(block):
        return block
    # What is left of an engine error once its frame is gone: a message about
    # a debugger port, a swapchain, or pages still allocated at exit. Nothing
    # naming project code, on a line the engine prints every run including the
    # green ones.
    if any(_PROJECT_SCHEME.search(line) for line in trimmed) or any(
        _LOCATION.search(line) for line in trimmed
    ):
        return trimmed
    return []


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
    blocks, _ = _blocks(strip_ansi(output).splitlines())
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


# The identifier a toolchain gives its own diagnostics, most specific first.
# Extracted so a *class* can be named the way its tool names it — `TS2532`,
# `trailing-whitespace`, `E0603` — rather than by a paraphrase of the message,
# which changes with every symbol it happens to mention.
#
# Nothing here is required to match. A tool with no error codes falls back to
# its message with the numbers masked, which is less precise and still stable
# across the line numbers that make raw text useless for this.
_CODES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\berror\[([A-Z]?\d+)\]"),                    # rustc / cargo
    re.compile(r"\berror\s+(TS\d+)\b", re.IGNORECASE),        # tsc
    re.compile(r"\b(clippy::[a-z_][a-z0-9_:]*)"),             # clippy lints
    re.compile(r"\(([a-z][a-z0-9]*(?:-[a-z0-9]+)+)\)\s*$"),   # gdlint, eslint
    re.compile(r"\b([EWFCNDIS]\d{3})\b"),                     # flake8, ruff, pycodestyle
    re.compile(r"\b([A-Z]\w*(?:Error|Exception))\b"),         # Python exceptions
    re.compile(r"\b(CS\d+|SA\d+|error\s+C\d+)\b"),            # C#, C++
)

# A test runner's verdict line: the framework saying which case failed. It
# carries no error code and its message is the *test's own name*, so treating
# it as a message mints a class per test case and the dedupe this exists for
# never fires. What it does carry, and what nothing else in a vitest failure
# does, is the file.
# `\b` only after the words: it needs a word character beside it, and a verdict
# written as a symbol has a space there. Without the split, every `× suite >
# case` line fell through to `_message_of` and minted a class per test case —
# the opposite of what this exists for.
_VERDICT = re.compile(r"^\s*(?:(?:FAIL|FAILED|ERROR)\b|[✗×])", re.IGNORECASE)

# A path with no line number after it, which `_LOCATION` deliberately does not
# match. Required to contain a separator: without that, `Object.is` and
# `assert.ok` are paths, and every assertion message names a file.
_BARE_PATH = re.compile(
    r"(?<![\w])((?:[A-Za-z]:)?[\w.+-]*[/\\][\w./\\+-]*" + _EXTENSION + r")\b"
)

# Every number that is not part of an error code. Line numbers, column numbers,
# counts, and the literal values an assertion compared — all of which change
# between two instances of the same mistake, and all of which made the raw text
# useless as an identity.
_NUMBERS = re.compile(r"(?<![A-Za-z])\d+(?:\.\d+)?")

# How much of a message survives into a class when there is no code to name it
# by. Long enough to distinguish two complaints, short enough that a compiler
# quoting a whole type signature does not make every instance unique.
_MESSAGE_CHARS = 60


def _code_of(head: str) -> str:
    """The tool's own name for this diagnostic, or "" if it does not give one."""
    for pattern in _CODES:
        found = pattern.search(head)
        if found:
            return found.group(1).strip()
    # After the codes, not before: a verdict line that also carries a code —
    # `FAILED ... error[E0603]` — should be classed by the code.
    if _VERDICT.match(head):
        return "test failed"
    return ""


def _message_of(head: str) -> str:
    """A head line reduced to the complaint, with everything variable removed.

    The fallback identity for a tool that does not number its diagnostics. Both
    the location and every number go: `a.py:14: undefined name 'x'` and
    `a.py:98: undefined name 'y'` are one mistake made twice, and a key that
    keeps the line number says they are two.
    """
    text = head
    for path in locations(head):
        text = text.replace(path, " ")
    text = _LOCATION.sub(" ", text)
    text = _VOLATILE.sub(" ", text)
    text = _NUMBERS.sub("#", text)
    # Quoted symbols are the other thing that varies between two instances of
    # one mistake: `'foo' is possibly undefined` and `'bar' is possibly
    # undefined` are the same misunderstanding about the same rule.
    text = re.sub(r"[`'\"][^`'\"]{1,80}[`'\"]", "@", text)
    text = re.sub(r"[^\w@#:\-. ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()[:_MESSAGE_CHARS]


def classify(step: str, output: str) -> set[str]:
    """The distinct *kinds* of failure in one step's output.

    `signatures` answers "is this the same error as the one the baseline had",
    and to answer it keeps the location — line and column included — because
    attribution needs to know which line broke. This answers a different
    question: "have I failed this way before". `TS2532` at line 40 and `TS2532`
    at line 51 are one misunderstanding about one compiler flag, and a key that
    separates them is why an executor could fail the same way 512 times without
    anything noticing.

    A class is `(step, code, file)`. The code is the tool's own — `TS2532`,
    `trailing-whitespace`, `E0603` — falling back to the message with its
    numbers, paths and quoted symbols masked. The file is kept because the same
    rule broken in two files is two pieces of work; the line is not, because
    the same rule broken twice in one file is one thing to learn.

    Shares `_blocks` and the head/location parsing with `signatures`, so the
    two never disagree about what counts as one diagnostic. They must not be
    collapsed into one function: attribution needs the line and convergence
    needs it gone.

    Returns readable strings on purpose. These are counted into the executor's
    prompt, and a class it cannot read is a fact it cannot use.
    """
    found: set[str] = set()
    # The same class seen *with* a file somewhere in this output.
    named: set[str] = set()
    blocks, _ = _blocks(strip_ansi(output).splitlines())
    for block in blocks:
        head = block[0].strip()
        name = _code_of(head) or _message_of(head)
        if not name:
            continue
        target = _file_of(block)
        found.add(f"{step} {name} in {target}" if target else f"{step} {name}")
        if target:
            named.add(f"{step} {name}")
    # A runner prints the same verdict twice — once with the file, once as a
    # bare case line — and the fileless copy says nothing the located one does
    # not. Dropped here rather than at the caller, so the count the executor is
    # shown is a count of distinct mistakes.
    found -= named
    if found:
        return found

    # Nothing parsed as a diagnostic, and the step still failed. Two kinds of
    # step reach here and both need an identity: a reviewer's `REJECT:`, which
    # is the loop's own protocol rather than a toolchain's, and any tool whose
    # output none of the patterns above recognise.
    #
    # Returning nothing instead is what a caller cannot survive. The retry
    # brake compares one cycle's classes to the last, and a ticket that
    # produces none is a ticket whose evidence is always new — which is exactly
    # the state that let one run repeat itself 86 times.
    first = next(
        (line.strip() for line in strip_ansi(output).splitlines() if line.strip()),
        "",
    )
    message = _message_of(first)
    return {f"{step} {message}"} if message else set()


def _file_of(block: list[str]) -> str:
    """The file this diagnostic is about, or "".

    Three places, in the order a tool is likely to put it: on the head with a
    line number, on the head with none — which `_LOCATION` does not match by
    design, and which is the only path a vitest failure prints — and only then
    on a later line.

    The head is asked both ways before anything below it is asked at all,
    because a tool states its subject first and then explains how it got
    there. Godot's is the case that settles the order: `ERROR: Failed to load
    script "res://tests/theme/x.gd" with error "Parse error"` names the file on
    the head without a line number and follows it with a ten-frame backtrace
    through gdUnit4's scanner. Reading a location below the head first blamed
    the scanner, which is in `addons/` and which no ticket may touch.
    """
    head = _URI_SCHEME.sub("", block[0].strip())
    where = locations(head)
    if where:
        return where[0]
    bare = _BARE_PATH.search(head)
    if bare:
        return bare.group(1).replace("\\", "/")
    below = next((locations(line) for line in block[1:] if locations(line)), [])
    return below[0] if below else ""


def distill(output: str, *, limit: int = 6000) -> str:
    """Reduce toolchain output to its diagnostics, newest information first.

    Falls back to the *head* of the output when nothing parses as a
    diagnostic: an unrecognized tool still tends to lead with its complaint,
    and the tail is where the noise lives.
    """
    text = strip_ansi(output).strip()
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


# Room reserved for `clip`'s marker, which cannot be measured until the size of
# what it is reporting is known. Generous: the message is fixed and the number
# in it cannot exceed a step's output.
_CLIP_MARKER = 120


def clip(text: str, limit: int) -> str:
    """`text` cut to `limit` characters, keeping both ends.

    Which end carries the verdict depends on the tool, and taking a side is
    wrong for half of them. A compiler leads with its diagnostics — tsc prints
    nothing else, and Godot reports a parse error nine lines in, right under
    its banner. A test runner ends with them: it logs a line per case as it
    goes and states what failed at the bottom.

    Head-only truncation lost the second kind completely. A gdUnit4 suite
    prints `STARTED` and `PASSED` for every case, so a project with a few
    hundred tests overruns any sane cap long before the summary — and on the
    run this comes from, 17 of one ticket's 37 recorded test failures stored
    not one line of failure text. All 17 were exactly at the cap, and every one
    of them was a run where discovery had *succeeded*, so there was nothing at
    the head either: what was kept was the engine banner and several hundred
    passing tests, filed as the evidence for a red step.

    The tail gets the larger share. A tool that leads with its diagnostics has
    said what it has to say within a few KB; one that ends with them has its
    per-case log running right up to the summary.
    """
    text = text or ""
    if len(text) <= limit:
        return text

    text = _cap_repeats(text)
    if len(text) <= limit:
        return text

    room = max(limit - _CLIP_MARKER, 0)
    head, tail = room * 2 // 5, room - room * 2 // 5
    # Whole lines, for the reason `_clip_lines` does it: a diagnostic cut in
    # half reads as a claim about a symbol that is not in the source.
    front = text[:head].rpartition("\n")[0] or text[:head]
    back = text[len(text) - tail :].partition("\n")[2] or text[len(text) - tail :]
    dropped = len(text) - len(front) - len(back)
    return f"{front}\n[… {dropped} characters not stored …]\n{back}"


# How many times one line may appear before the rest are counted instead of
# kept. Two rather than one, so a genuinely repeated diagnostic still reads as
# repeated.
_REPEAT_LIMIT = 2


def _cap_repeats(text: str) -> str:
    """The same line over and over, replaced by a count of how often.

    Position is the wrong thing to select on when most of the output is one
    sentence. A green gdUnit4 run of a 400-test suite is 738,000 characters, of
    which 633,000 — 87% — is two lines alternating 3,475 times apiece:

        ERROR: Condition "!((HRESULT)(res) >= 0)" is true. Returning: …
           at: swap_chain_resize (drivers/d3d12/rendering_device_driver_d3d12…)

    Godot writes them while shutting down its renderer, *after* the run's
    verdict. So the summary sits 29% of the way in, with half a megabyte of
    that couplet behind it: a head cut misses it and so does a tail cut. With
    the repeats counted the same run is 84,000 characters, the verdict is near
    the end of it where a tail cut finds it, and exactly two distinct lines
    were affected.

    Only reached when the output is already over the limit, so nothing that
    fits is ever altered — and the count is kept, because "this happened 3,475
    times" is itself a fact about the run.
    """
    seen: Counter[str] = Counter()
    dropped: Counter[str] = Counter()
    kept: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            # Runs of blank lines are what removing thousands of lines leaves
            # behind, and they cost the room this is trying to save.
            if kept and kept[-1].strip():
                kept.append(line)
            continue
        seen[stripped] += 1
        if seen[stripped] <= _REPEAT_LIMIT:
            kept.append(line)
        else:
            dropped[stripped] += 1

    if not dropped:
        return text
    return "\n".join(kept) + (
        f"\n[{sum(dropped.values())} further line(s) identical to "
        f"{len(dropped)} shown above, not stored]"
    )


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
    r"((?<![\w])(?:[A-Za-z]:)?[\w./\\+-]*(?:" + _EXTENSION + r"|[/\\][\w+-]+))"
    r"(?::(\d+)(?::\d+)?|\((\d+)(?:,\d+)?\))"
)

# A scheme no repository path has, dropped before matching so the location
# underneath is an ordinary one. `file:///repo/src/Main.kt` is kotlinc and any
# tool reporting URIs; `res://tests/x.gd` is Godot naming a file relative to
# the project root, which is repository-relative already.
#
# Without the second, every GDScript location parsed to `//tests/x.gd` — a path
# with a leading double slash that matches nothing on disk and nothing in a
# ticket's scope. For the whole Godot half of a project, attribution, baseline
# amnesty and contradiction detection were answering about a file that does not
# exist.
#
# `user://` is deliberately absent. It names the engine's user-data directory,
# which is outside the repository, and rewriting it to a bare relative path
# would invent a repository file that was never there.
_URI_SCHEME = re.compile(r"\b(?:file|res)://", re.IGNORECASE)


# How each runner says how many tests it ran. The largest number any of these
# finds is taken, because several of them print more than one — pytest reports
# `collected 5 items` and then `5 passed`, and a suite that grew shows the
# growth in whichever number is biggest.
#
# A runner missing from this table yields no count, which is not a failure: the
# caller treats "cannot tell" as its own answer and says so rather than
# guessing. `go test` is the honest example — it prints `ok  pkg  0.01s` and no
# count at all.
_TEST_COUNTS = (
    re.compile(r"\b(\d+)\s+passed\b", re.IGNORECASE),          # pytest, jest, vitest
    re.compile(r"\bcollected\s+(\d+)\s+items?\b", re.IGNORECASE),  # pytest
    re.compile(r"\bRan\s+(\d+)\s+tests?\b", re.IGNORECASE),   # unittest
    re.compile(r"\b(\d+)\s+passing\b", re.IGNORECASE),         # mocha
    re.compile(r"^#\s*pass\s+(\d+)\b", re.IGNORECASE | re.MULTILINE),  # node:test
    re.compile(r"\bTests\s+run:\s*(\d+)", re.IGNORECASE),      # JUnit / maven
    re.compile(r"\b(\d+)\s+tests?\s+completed\b", re.IGNORECASE),  # gradle
    re.compile(r"\b(\d+)\s+examples?\b", re.IGNORECASE),       # rspec
    re.compile(r"\b(\d+)\s+tests?,\s*\d+\s+assertions?\b", re.IGNORECASE),  # phpunit
)


def test_count(output: str) -> int | None:
    """How many tests a runner said it ran, or `None` when it did not say.

    `None` is a real answer and the caller must treat it as one. Reading "no
    number printed" as "no tests ran" would fail every `go test` in existence,
    and reading it as "fine" is what this exists to stop — so the only correct
    handling is to say the question could not be answered.
    """
    found = [
        int(match.group(1))
        for pattern in _TEST_COUNTS
        for match in pattern.finditer(output or "")
    ]
    return max(found) if found else None


def reroot(output: str, prefix: str, root: Path) -> str:
    """Rewrite a workspace's own paths so they are relative to the repository.

    Every attribution in the loop matches **repo-relative** paths against the
    text a command printed: `signatures` keys a diagnostic by its location,
    `errors_naming` asks whether a failure names the tester's file,
    `files_blamed` decides which ticket owns a red tree, and the executor is
    handed the output and told to fix the files in it.

    A command run with `cwd` set to a workspace prints paths relative to *that*
    directory. `tsc` under `tools/path-forge` reports `src/parser/level.ts`
    while the ticket owns `tools/path-forge/src/parser/level.ts`, and every one
    of those questions then answers "no file here": the diagnostic attributes
    to nothing, the baseline excuses it as unattributable, and the attempt goes
    green over a build that does not compile. That is the exact failure
    workspaces exist to remove, reintroduced by the fix for it — which is why
    this lands in the same commit as the `cwd` change and not after it.

    Conservative by construction. A path is rewritten only when
    `root/prefix/path` is a file that exists, so a runner's internal module
    names (`node:internal/modules/cjs/loader`), a URL, a version string, and a
    path already relative to the repository are all left exactly as they were.
    The cost of missing one is the behaviour we had; the cost of inventing one
    is blaming a ticket for a file in another build.
    """
    if not prefix or not output:
        return output
    prefix = prefix.replace("\\", "/").rstrip("/")
    if not prefix or prefix == ".":
        return output

    def rewrite(match: re.Match[str]) -> str:
        whole = match.group(0)
        path = match.group(1)
        normalized = path.replace("\\", "/")
        if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
            return whole  # already absolute
        if normalized == prefix or normalized.startswith(f"{prefix}/"):
            return whole  # already repo-relative
        try:
            if not (root / prefix / normalized).is_file():
                return whole
        except OSError:
            return whole
        # Keep the separator the tool used, so a Windows toolchain's output
        # still reads as its own and a regex someone wrote against it still
        # matches.
        joiner = "\\" if "\\" in path and "/" not in path else "/"
        replacement = prefix.replace("/", joiner) + joiner + path
        return replacement + whole[len(path) :]

    return _LOCATION.sub(rewrite, output)


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
    for match in _LOCATION.finditer(_URI_SCHEME.sub("", strip_ansi(text))):
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
            # Through `locations` rather than `_LOCATION` directly: it is this
            # module's one answer to "which file is this about", and a second
            # copy of the pattern here is how this came to be the only reader
            # that never stripped a URI scheme.
            for path in locations(line):
                entry = line.strip()
                lines = blamed.setdefault(path, [])
                if entry not in lines:
                    lines.append(entry)
    return blamed


def blocks_naming(text: str, path: str) -> str:
    """The diagnostic blocks that name `path`, joined. "" when none do.

    Between `errors_naming`, which returns only the lines carrying the name,
    and the raw output, which carries everybody's. Both extremes are wrong for
    the one question this answers — *why* did the run fail on this file — and
    they are wrong in opposite directions.

    The lines alone lose the diagnosis. Python reports

        ImportError: cannot import name 'locked'
        tests/bug_001_test.py:1: in <module>

    with the cause on one line and the file on the next, so a caller matching
    "does this look like a build error" against the matched line alone sees a
    location and nothing else.

    The raw output borrows somebody else's. A reproduction failing on a
    perfectly good assertion was condemned as unbuildable because a different
    test, further up the same run, had reported `no such file` — the file was
    named, an unbuildable-looking phrase existed somewhere, and the two were
    never required to be about the same thing.

    The block is the unit that gets that right: it is exactly one diagnostic,
    with its own continuation lines and nobody else's.
    """
    if not path:
        return ""
    blocks, _ = _blocks((text or "").splitlines())
    wanted = {path.replace("\\", "/").lower(), path.replace("/", "\\").lower()}
    base = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
    bare = re.compile(rf"(?<![\w./\\-]){re.escape(base)}") if base else None

    def about(block: list[str]) -> bool:
        joined = "\n".join(block).lower()
        if any(name in joined for name in wanted):
            return True
        # Same bare-name fallback as `errors_naming`, and for the same reason:
        # JUnit prints stack frames with no directory at all.
        return bool(bare and bare.search(joined))

    return "\n".join("\n".join(block) for block in blocks if about(block))


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

    def names_it(line: str) -> bool:
        return any(name in line for name in wanted)

    # JUnit prints a stack frame as the bare file name — `java.io.IOException
    # at bug_001_test.java:17` — with no directory anywhere in the output. The
    # full-path match then finds nothing, so a reproduction that died on its
    # own first line read as unimplicated and was accepted as proof of the bug.
    #
    # Two things keep the fallback from over-matching. It is tried only when
    # the full path found nothing, so a toolchain that prints paths keeps the
    # stricter answer. And the bare name has to appear *bare*: a name preceded
    # by a path separator belongs to some other directory's file, which is how
    # `a/shared_test.java` would otherwise implicate `b/shared_test.java`.
    if not any(names_it(line.lower()) for line in lines):
        base = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
        if base:
            bare = re.compile(rf"(?<![\w./\\-]){re.escape(base)}")

            def names_it(line: str) -> bool:  # noqa: F811
                return bool(bare.search(line))

    found: list[str] = []
    for index, raw in enumerate(lines):
        lowered = raw.lower()
        if not names_it(lowered):
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


# Output that says the command never ran the code, in the spellings the shells
# and launchers use. Each phrase is one only a broken invocation prints — a
# missing binary, an interpreter that will not start, a runtime the launcher
# cannot find. None of them is something a compiler or a test runner says about
# source code.
#
# The point of naming them is that the loop's whole diagnosis machinery assumes
# the failing command reached the code. Handed `Gradle requires JVM 17 or later
# to run. Your build is currently configured to use JVM 8`, it distils it,
# files it as the ticket's failure, and asks a model to fix it. The model
# answers — correctly — that this is an environment problem and writes no
# files, which is then recorded as a reply that did not parse. One run spent
# ten minutes, thirty model calls and 131k tokens on that exchange before any
# code was written, and the executor was right every time.
_ENVIRONMENT = (
    re.compile(r"^.*: command not found", re.MULTILINE),
    re.compile(r"is not recognized as an internal or external command", re.IGNORECASE),
    re.compile(r"The term '[^']+' is not recognized as", re.IGNORECASE),
    re.compile(r"^(?:/bin/)?s?h: \d*:? ?\S+: not found", re.MULTILINE),
    re.compile(r"\brequires JVM \d+", re.IGNORECASE),
    re.compile(r"\bJAVA_HOME\b[^\n]*(?:not set|invalid|does not)", re.IGNORECASE),
    re.compile(r"Unable to locate a Java Runtime", re.IGNORECASE),
    re.compile(r"No matching (?:Java )?toolchains found", re.IGNORECASE),
    re.compile(r"Could not find or load main class", re.IGNORECASE),
    re.compile(r"^\S*python\S*: No module named \w+", re.MULTILINE),
    re.compile(r"no such command: `?\w+", re.IGNORECASE),
    re.compile(r"could not find `Cargo\.toml`", re.IGNORECASE),
    re.compile(r"^\S+: Permission denied", re.MULTILINE),
)


def environment_failure(output: str) -> str:
    """The line saying this command never ran the code, or "".

    Answers a question the rest of this module takes for granted: whether the
    output is *about* the project at all. `signatures` and `files_blamed` both
    assume a tool that started, read the source, and complained about it. A
    launcher that cannot find a runtime produces neither a diagnostic nor a
    location, and everything downstream reads that as "unparseable output",
    which is excusable — so the failure survives every check the loop has and
    lands in an executor prompt as work.

    Deliberately not a judgment about severity. It answers "did the toolchain
    run", and the caller decides what that means; a run is stopped on it,
    because no ticket can fix a machine.

    Callers must check `signatures(output)` is empty as well. A test suite
    asserting on the text of a shell error is real output about real code and
    prints a diagnostic block beside it; a launcher that never started prints
    nothing else at all.
    """
    for pattern in _ENVIRONMENT:
        found = pattern.search(output or "")
        if not found:
            continue
        # The whole line, not the match: `requires JVM 17` is the fingerprint,
        # and `Gradle requires JVM 17 or later to run. Your build is currently
        # configured to use JVM 8` is what a person needs to read.
        line = (output or "")[: found.start()].rsplit("\n", 1)[-1]
        line += (output or "")[found.start() :].split("\n", 1)[0]
        return line.strip()
    return ""
