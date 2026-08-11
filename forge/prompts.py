"""Prompts for each role.

Kept in one file because they are a contract, not implementation detail: the
executor prompt's rules about scope and `BLOCKED:` are what `patch.py` parses,
and the reviewer's `REJECT` verdict is what the loop branches on. Changing the
wording without changing the parser is how an autonomous loop starts silently
accepting work it should have rejected.
"""

from __future__ import annotations

import json
import re
from typing import Any, Sequence

from .failures import distill
from .providers import Message
from .state import Ticket

# The prefix that marks a message as droppable to the budget gate. Memory and
# ticket context live behind it; the spec never does.
CONTEXT_HEADING = "## Established project context"

EXECUTOR_SYSTEM = """You are the executor in a plan-and-execute pipeline.

A senior engineer has already made the design decisions. Implement the spec
exactly as written. Do not redesign it.

Rules:
- Only modify files in the allowed scope. Anything else is out of bounds and
  will be rejected before it reaches disk.
- Implement every acceptance criterion. Add nothing that was not requested.
- Use the libraries and signatures the spec names.
- Established project context describes decisions already made on this project.
  Follow it. If it contradicts the spec, say so with BLOCKED: rather than
  silently picking one.
- If the spec is ambiguous or you believe it is wrong, DO NOT GUESS. Reply with
  a line starting `BLOCKED:` explaining precisely what is unclear, and nothing
  else. A blocked ticket is a useful outcome; a plausible guess is not.
- Output the COMPLETE contents of every file you change. For each one, put the
  file path on its own line, then a fenced code block containing the whole
  file. No partial files, no diffs, no ellipses. One block per file, and never
  two blocks for the same path.
- If a file's own contents contain a ``` fence — README.md and any other
  markdown almost always does — wrap that file in a LONGER fence: four
  backticks, or five. A three-backtick fence around content that itself uses
  three backticks ends at the content's first inner fence. The file is then
  written truncated, and the rest of it is read as though it were more files,
  which silently overwrites whichever file the leftover prose happens to name.
- Because you emit whole files, every line you were not asked to change is one
  you must reproduce character for character — docstrings, blank lines,
  formatting, and functions the ticket never mentions included. Rewriting a
  line that already worked is a change the spec did not ask for, and it will be
  rejected at review even when the behavior is identical. Copy first, then add.
"""

TESTER_SYSTEM = """You write tests that encode criteria decided upstream.

You are given acceptance criteria authored during planning. Encode exactly
those criteria as assertions. Do not invent additional criteria, and do not
weaken a criterion to make it easier to satisfy.

Assert on behavior, through the public interface, and on nothing else. The
whole suite runs on every later ticket too, so an assertion about the shape of
the repository rather than the behavior of the code becomes a trap that a
future ticket walks into and cannot disarm — the file is not in its scope.

Never:
- Read a source file and assert on its text. `read_to_string("src/lib.rs")`
  compared against an expected string fails the moment any later ticket adds a
  line to it, which is a thing later tickets are supposed to do.
- Assert that a file exists, does not exist, or has a particular length.
- Assert on the exact whole contents of any file, including generated assets.
- Declare foreign-function bindings (`extern`, `dlopen`, `ctypes`) against the
  code under test. Import it the way the rest of the project imports it. An
  `extern` block re-declares a symbol instead of referencing it, so the linker
  never pulls it in and the target fails to link rather than to assert.
- Write anything to a path other than the one file you are told to write.

Output the complete contents of that one test file: the path on its own line,
then a fenced code block with the whole file.
"""

REVIEWER_SYSTEM = """You are the reviewer in a plan-and-execute pipeline.

Lint, type-checking, and the test suite have already passed. Your job is what
tooling cannot catch. Review the diff AGAINST THE SPEC, not against "the tests
passed".

Check specifically:
- Every acceptance criterion is actually satisfied, not approximated.
- Tests assert real behavior rather than restating the implementation.
- No silent scope creep, dropped error handling, or swallowed exceptions.
- Nothing contradicts the established project context, when any is supplied.

Begin your reply with exactly one word on its own line: ACCEPT or REJECT.
Then give your reasoning, shortest decisive point first. Reject when a
criterion is unmet or the diff does something the spec did not ask for.
"""


RECORDER_SYSTEM = """You decide whether a completed ticket produced anything
worth writing to long-term project memory.

Memory is read back into every future ticket's prompt. An entry that is not
durable does not just waste space — it crowds out the entries that are, and it
will be presented to a future model as established fact. The default answer is
therefore NOTHING, and most tickets deserve it.

Record ONLY:
- A decision and its reasoning ("chose tiny-skia over resvg because resvg
  pulled in a font stack we do not need").
- A convention a future implementer must follow to stay consistent.
- A review correction: what was wrong, and what right looks like.

Never record:
- Narration of what this ticket did. That is what git history is for.
- File contents, diffs, function signatures, or anything reconstructible from
  the repository.
- Transient state: what passed, what the test output was, how many attempts.
- Restatements of the spec or the acceptance criteria.
- Credentials, tokens, keys, or connection strings, in any form.

Reply with exactly one of:

NOTHING
  (on its own line, when no durable outcome emerged — this is the common case)

or

TITLE: <one short line naming the decision or convention>
<one to four sentences: what was decided or corrected, and why. Written for
someone who will read it months from now with no memory of this ticket.>
"""


def _criteria_block(ticket: Ticket) -> str:
    return "\n".join(f"- {c}" for c in ticket.criteria) or "- (none stated)"


def _files_block(ticket: Ticket) -> str:
    return "\n".join(f"- {p}" for p in ticket.allowed_files) or "- (none stated)"


def _reference_block(ticket: Ticket) -> str:
    return "\n".join(f"- {p}" for p in ticket.reference_files) or "- (none stated)"


def merge_context(ticket_context: str, retrieved: str) -> str:
    """Combine the ticket's own context with anything retrieved from memory.

    Ticket context comes first: it was written for this specific work, while
    retrieved memory is topical at best. If the budget gate later has to trim,
    it drops the whole block — memory never displaces the spec.
    """
    parts = [part.strip() for part in (ticket_context, retrieved) if part and part.strip()]
    return "\n\n".join(parts)


def _context_message(ticket: Ticket, retrieved: str) -> Message | None:
    context = merge_context(ticket.context, retrieved)
    if not context:
        return None
    return Message(role="user", content=f"{CONTEXT_HEADING}\n{context}")


def _sources_block(sources: dict[str, str]) -> str:
    """Render file contents the executor needs to see.

    The executor has no filesystem: it returns whole files as text. Anything it
    is not shown, it invents — which is how a ticket ends up rejected for
    calling an export that does not exist, or stops with "I do not have access
    to the current contents of src/wasm.rs".
    """
    parts = []
    for path, content in sources.items():
        body = content if content.endswith("\n") else content + "\n"
        parts.append(f"#### {path}\n```\n{body}```")
    return "\n\n".join(parts)


def build_prompt(
    ticket: Ticket,
    failure_context: str = "",
    retrieved: str = "",
    sources: dict[str, str] | None = None,
    *,
    prior_failures: Sequence[str] = (),
) -> list[Message]:
    messages = [Message(role="system", content=EXECUTOR_SYSTEM)]

    context = _context_message(ticket, retrieved)
    if context is not None:
        messages.append(context)

    body = f"""Ticket: {ticket.ticket_id} — {ticket.title}

## Spec
{ticket.spec}

## Allowed scope (do not modify anything outside this list)
{_files_block(ticket)}

## Acceptance criteria
{_criteria_block(ticket)}
"""

    if sources:
        writable = set(ticket.allowed_files)
        current = {p: c for p, c in sources.items() if p in writable}
        reference = {p: c for p, c in sources.items() if p not in writable}
        if current:
            body += f"""
## Current contents of the files you may write
Return the complete file. Preserve everything you are not changing.

{_sources_block(current)}
"""
        if reference:
            body += f"""
## Reference — read only, do not return these files
This is the real source. Take export names, signatures, and enum order from
here rather than assuming them.

{_sources_block(reference)}
"""

    if failure_context:
        body += f"""
## Your previous attempt failed verification
Fix the cause. Do not work around the check.

{failure_context}
"""

    if prior_failures:
        # Without this the executor sees only the newest failure and can
        # oscillate: a change that fixes A breaks B, the fix for B brings A
        # back, and three attempts go by with nothing able to see the cycle.
        earlier = "\n\n".join(
            distill(entry, limit=800) for entry in prior_failures
        )
        body += f"""
## Earlier attempts on this ticket, oldest first
These already failed. A fix you have tried before will fail the same way
again — if the newest failure is one you have already seen here, the two
changes are undoing each other, and you need a third approach that satisfies
both rather than alternating between them.

{earlier}
"""

    body += "\nImplement this now."
    messages.append(Message(role="user", content=body))
    return messages


def tests_prompt(
    ticket: Ticket,
    changed_files: list[str],
    *,
    test_path: str,
    test_command: str = "",
    example_test: tuple[str, str] | None = None,
    failure_context: str = "",
    sources: dict[str, str] | None = None,
) -> list[Message]:
    """Ask the tester for assertions, with the evidence to match the repo.

    Telling a model to "follow the conventions of this repository" is only an
    instruction if it can see the repository, and the tester cannot — it gets a
    ticket, not a checkout. Left to guess it writes whichever framework is most
    common in its training data, which on a `unittest` project means a file the
    runner collects nothing from: zero tests, a non-zero exit, and a ticket
    that fails three times with a correct implementation on disk.

    So the two things that actually determine the answer are passed in: the
    command that will judge the tests, and a real test file from this repo to
    imitate. Both are already known to the daemon.

    `test_path` is the third, and it is not a suggestion. Left to name its own
    file, a tester picks a fresh name on every retry — `wasm_layer.rs`, then
    `tt004_wasm.rs`, then `wasm_exports.rs` — and each abandoned file stays on
    disk and keeps running. Verification is whole-project, so those orphans
    fail every *other* ticket in the backlog, and no ticket has them in scope
    to fix. One fixed path per ticket means a retry overwrites its own work
    instead of accumulating it.
    """
    files = "\n".join(f"- {p}" for p in changed_files) or "- (none)"
    body = f"""Ticket: {ticket.ticket_id} — {ticket.title}

## Write exactly one file, at exactly this path
```
{test_path}
```
This is the only path you may write. It already holds this ticket's tests if
there are any — replace it wholesale. Do not add a second file, do not pick a
different name, and do not append a suffix: anything else is discarded before
it reaches disk, and a file you abandon under another name keeps running
against every later ticket in this project.

## Files under test
{files}

## Criteria to encode as assertions
{_criteria_block(ticket)}
"""

    if test_command:
        body += f"""
## The command that will run your tests
```
{test_command}
```
Your tests must be collected and executed by that command. A file it does not
collect is worse than no file: it reports zero tests and fails the ticket.
"""

    if sources:
        body += f"""
## The code under test — read this before asserting anything
These are the real files. Every name, signature, visibility, and type you
assert against must come from here. A field that is actually a method, a
private field, or an argument of the wrong type does not fail the
implementation — it fails to compile, and takes the whole suite down with it.

{_sources_block(sources)}
"""

    if example_test is not None:
        path, content = example_test
        body += f"""
## An existing test in this repository — match this framework and style
`{path}`:
```
{content}
```
"""
    else:
        body += "\nFollow the test framework and conventions already used in this repository.\n"

    if failure_context:
        body += f"""
## The previous attempt did not pass verification

```
{failure_context}
```

Read this before writing anything, and decide which kind of failure it is.

A failure caused by **your own assertion being wrong** — asserting on something
the criterion never claimed, comparing a return value against source text,
importing a name that does not exist, a typo in an expected value — is yours to
correct. Write the assertion the criterion actually describes.

A failure caused by **the implementation being wrong** is not yours to correct.
Write the same assertion again, unchanged. Deleting it, loosening it, or
wrapping it in a skip would hide a real defect and end the ticket with a green
suite over broken code — a worse outcome than any failing test.

If you cannot tell which it is, keep the assertion as written.
"""

    return [
        Message(role="system", content=TESTER_SYSTEM),
        Message(role="user", content=body),
    ]


def review_prompt(
    ticket: Ticket,
    diff: str,
    retrieved: str = "",
    *,
    prior_verdicts: Sequence[str] = (),
    state: dict[str, str] | None = None,
) -> list[Message]:
    messages = [Message(role="system", content=REVIEWER_SYSTEM)]

    # The reviewer is asked to check that nothing contradicts established
    # conventions, which it cannot do without being told what they are.
    context = _context_message(ticket, retrieved)
    if context is not None:
        messages.append(context)

    body = f"""Ticket: {ticket.ticket_id} — {ticket.title}

## Spec
{ticket.spec}

## Acceptance criteria
{_criteria_block(ticket)}

## Diff
```diff
{diff or "(empty diff)"}
```
"""

    if state:
        # An empty diff means this ticket changed nothing — which is a finding,
        # but not the same finding as "the work was never done". Only the files
        # themselves distinguish "already correct before this ticket ran" from
        # "never written", and a reviewer without them guesses, confidently.
        body += f"""
## The diff is empty — here is what is actually on disk
This ticket wrote nothing this attempt. Judge the criteria against these
contents, not against the absence of a diff. If they are already satisfied,
say so and ACCEPT: a ticket whose work was already done is finished, not
failed. If they are not, REJECT and name what is missing.

{_sources_block(state)}
"""

    if prior_verdicts:
        earlier = "\n\n".join(
            f"### Attempt {index}\n{verdict.strip()}"
            for index, verdict in enumerate(prior_verdicts, start=1)
        )
        body += f"""
## You have already rejected this ticket
{earlier}

Read these before deciding. If the objection you raised has been addressed,
that is progress — do not replace it with a fresh objection you never raised
before, which ends the ticket in three rounds over three unrelated points. If
the same defect is still there, say so plainly and in the same terms: a
rejection that repeats is evidence the spec is wrong rather than the code, and
saying it in those words is what gets that noticed.
"""

    messages.append(Message(role="user", content=body))
    return messages


def parse_verdict(text: str) -> tuple[bool, str]:
    """Read a reviewer's reply as (approved, reason).

    Deliberately fail-closed. The obvious implementation — approve unless the
    reply starts with REJECT — treats every unreadable answer as a pass, and
    the ways a reply can be unreadable are not exotic. The one observed in
    practice: a model that echoes its own instruction, `ACCEPT or REJECT:`, on
    the first line. That does not start with REJECT, so a rejection sailed
    through and the ticket was marked done over work the reviewer refused.

    So a verdict line must say one word and not the other, approval must be
    stated rather than inferred, and anything else is a rejection carrying the
    reply for a human to read. A wrongly-rejected ticket costs an attempt; a
    wrongly-approved one is what the review step exists to prevent.
    """
    for raw in text.splitlines():
        # Tolerate `**ACCEPT**`, `# REJECT`, `ACCEPT.` — models decorate.
        line = raw.strip().strip("*#`_ \t.:—-").upper()
        if not line:
            continue
        has_accept, has_reject = "ACCEPT" in line, "REJECT" in line
        # Both words on one line is the instruction being repeated, not a
        # decision. Skipping it is what lets the real verdict below be found.
        if has_accept and has_reject:
            continue
        if has_reject:
            return False, text.strip()
        if has_accept:
            return True, text.strip()

    return False, (
        "reviewer gave no readable ACCEPT or REJECT verdict; treating as a "
        f"rejection.\n\n{text.strip()}"
    )


NOTHING_SENTINEL = "NOTHING"


def record_prompt(
    ticket: Ticket,
    diff: str,
    review: str,
    attempts: int,
    corrections: str = "",
    retrieved: str = "",
) -> list[Message]:
    """Ask whether this ticket produced anything worth remembering.

    Given the retrieved context too, so it can avoid re-recording something the
    palace already knows — duplicate entries are how a memory store becomes
    noise that everyone learns to ignore.
    """
    messages = [Message(role="system", content=RECORDER_SYSTEM)]

    context = _context_message(ticket, retrieved)
    if context is not None:
        messages.append(context)

    body = f"""Ticket: {ticket.ticket_id} — {ticket.title}

## Spec
{ticket.spec}

## Attempts
{attempts}
"""

    if corrections:
        # The most valuable memories usually come from here: something failed
        # verification, and the fix generalizes beyond this ticket.
        body += f"""
## What failed before it passed
{corrections}
"""

    body += f"""
## Reviewer's verdict
{review}

## Diff
```diff
{diff or "(empty diff)"}
```

Did this produce a durable decision, convention, or correction? If the project
context above already covers it, answer {NOTHING_SENTINEL}."""

    messages.append(Message(role="user", content=body))
    return messages


def parse_record(text: str) -> tuple[str, str]:
    """Split a recorder reply into (title, entry). Empty entry means nothing.

    Tolerant of a model that adds a preamble, because the cost of a strict
    parser here is silently discarding a good memory.
    """
    stripped = (text or "").strip()
    if not stripped:
        return "", ""

    upper = stripped.upper()
    if upper == NOTHING_SENTINEL or upper.startswith(NOTHING_SENTINEL):
        return "", ""

    title = ""
    lines = stripped.splitlines()
    body_start = 0
    for index, line in enumerate(lines):
        if line.strip().upper().startswith("TITLE:"):
            title = line.split(":", 1)[1].strip()
            body_start = index + 1
            break

    body = "\n".join(lines[body_start:]).strip()
    if not body:
        # A title with no body is not a memory worth keeping.
        return "", ""
    # A reply that mentions NOTHING anywhere but still has a body is ambiguous;
    # treat an explicit standalone NOTHING line as declining.
    if any(line.strip().upper() == NOTHING_SENTINEL for line in lines):
        return "", ""
    return title, body


RESPEC_SYSTEM = """You are the planner in a plan-and-execute pipeline, revising
one ticket that repeatedly failed verification.

The implementation was retried until its attempts ran out. Your job is to work
out what the *ticket* got wrong and fix that — not to implement anything.

Read the failures as evidence about the spec:

- The same rejection recurring across attempts means the spec or the criteria
  are wrong, ambiguous, or contradict something the executor cannot see. Fix
  the wording so the next executor cannot make the same reading.
- A rejection naming a file outside the allowed scope means the scope is too
  narrow. Widen `allowed_files` to what the work genuinely needs.
- The executor has no filesystem and cannot open anything. If it stopped
  saying it lacked a file's contents, or if it guessed an export name, a
  signature, or an enum order wrong, the fix is to list that file in
  `reference_files` — which pastes it into the prompt read-only. Telling it to
  "read" a file it was never given is the failure, not the remedy.
- A rejection about behaviour nobody asked for means a criterion is missing.
  Add it, stated so it can be checked rather than argued about.
- A concrete defect the reviewer identified (a wrong API, a bad assumption, a
  misread of how something behaves) belongs in `context`, so the next attempt
  starts already knowing it.

Rules:

- Keep the same goal. You are clarifying a ticket, not replacing it with an
  easier one. Never satisfy a criterion by deleting it.
- Prefer precision over volume. Add the sentence that removes the ambiguity;
  do not restate the whole ticket.
- Every criterion must describe the behavior of code this ticket writes. This
  ticket is one of many sharing a repository, and a criterion that reaches
  outside its own scope cannot be satisfied and cannot be retired. Never write
  a criterion that:
    * requires a file to be absent, deleted, or unchanged;
    * pins the exact or total contents of a file ("contains exactly the two
      lines ... and nothing else") — a later ticket will legitimately add to
      it, and then both tickets fail forever;
    * forbids some other part of the project from referencing a module, adding
      an import, or declaring a symbol;
    * describes the state of the build rather than the behavior of the code
      ("the suite compiles", "no target references X").
  If a failure was caused by a file this ticket does not own, that is not a
  spec defect. Leave the criteria alone and say so in the rationale.
- Write each criterion in the calling convention the language actually uses. A
  criterion stated as a bare C symbol invites a test that declares an `extern`
  binding, which fails to link instead of failing to assert.
- If the failures show the work simply was not finished — no recurring theme,
  no ambiguity, nothing the spec could have prevented — say so by returning
  the ticket essentially unchanged with a rationale explaining why.

Reply with JSON and nothing else:

{
  "rationale": "one or two sentences on what the ticket got wrong",
  "spec": "the revised spec",
  "criteria": ["revised acceptance criteria"],
  "allowed_files": ["revised scope"],
  "reference_files": ["files the executor must be shown to get this right"],
  "context": "what the next attempt should already know"
}"""


def respec_prompt(ticket: Ticket, failures: list[dict[str, str]]) -> list[Message]:
    """Ask the planner to fix a ticket that its own executor could not satisfy."""
    evidence = "\n\n".join(
        f"### Attempt {index}: {item['name']} failed\n{item['detail'].strip()}"
        for index, item in enumerate(failures, start=1)
    ) or "(no recorded step failures)"

    body = f"""Ticket: {ticket.ticket_id} — {ticket.title}

## Current spec
{ticket.spec}

## Current allowed scope (writable)
{_files_block(ticket)}

## Current reference files (pasted in read-only)
{_reference_block(ticket)}

## Current acceptance criteria
{_criteria_block(ticket)}

## Current context
{ticket.context.strip() or "(none)"}

## What happened, oldest attempt first
{evidence}

Revise the ticket so the next attempt can succeed."""

    return [
        Message(role="system", content=RESPEC_SYSTEM),
        Message(role="user", content=body),
    ]


def parse_respec(text: str) -> dict[str, Any]:
    """Parse a respec reply into the ticket fields it changes.

    Returns only the keys the planner actually supplied, so a reply that omits
    a field leaves the existing value alone rather than blanking it — a
    dropped `allowed_files` would silently narrow scope to nothing.
    """
    candidate = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*\n(.*?)\n```", candidate, re.DOTALL)
    if fence:
        candidate = fence.group(1)
    else:
        first, last = candidate.find("{"), candidate.rfind("}")
        if first != -1 and last > first:
            candidate = candidate[first : last + 1]

    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ValueError(f"planner did not return usable JSON: {(text or '')[:400]}") from exc
    if not isinstance(data, dict):
        raise ValueError("planner reply was not a JSON object")

    revision: dict[str, Any] = {}
    for key in ("spec", "context", "rationale"):
        if isinstance(data.get(key), str) and data[key].strip():
            revision[key] = data[key].strip()
    for key in ("criteria", "allowed_files", "reference_files"):
        value = data.get(key)
        if isinstance(value, list):
            items = [str(v).strip() for v in value if str(v).strip()]
            # An empty list is almost always a truncated reply rather than a
            # deliberate "this ticket needs no criteria"; treat it as absent.
            if items:
                revision[key] = items

    if not revision.get("spec"):
        raise ValueError("planner reply carried no revised spec")
    return revision
