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
    r"|panicked at|thread '.*' panicked"         # rust panics
    r"|\s*(?:Assertion|assert)\w*Error"          # python
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
    r"|^\s*\^+",                            # caret underlines
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
        # The opening line says what went wrong; the `-->` span says where. One
        # without the other collapses distinct errors together: rustc emits the
        # same `unresolved import` head once per test target.
        head = block[0].strip()
        where = next(
            (line.strip() for line in block[1:] if line.strip().startswith("-->")), ""
        )
        key = re.sub(r"\s+", " ", _VOLATILE.sub("#", f"{head} {where}")).strip().lower()
        if key:
            found.add(key)
    return found


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
