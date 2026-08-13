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

EVERY objection must cite what you looked at. Quote the line you are objecting
to, or — when you are objecting that something is missing — name the exact text
you searched for and did not find. An objection with neither is not a finding,
and you must not raise it.

This is not a formality. Reviewers reject work that is plainly present: one
said a canvas "does not specify a width of 240 and a height of 480" about a
file whose second line read `<canvas id="board" width="240" height="480">`, and
said it three times. Quoting first is what catches that before you send it.
Search the material you were given for the thing you are about to call missing,
and if you find it, you have no objection.

Begin your reply with exactly one word on its own line: ACCEPT or REJECT.
Then give your reasoning, shortest decisive point first, each point carrying
its citation. Reject when a criterion is unmet or the diff does something the
spec did not ask for.

Write only your verdict and your reasoning. Do not restate the sections you
were given — no `## Spec`, no `## Diff`, no repetition of earlier attempts.
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


def _criteria_block(ticket: Ticket, criteria: Sequence[str] | None = None) -> str:
    items = ticket.criteria if criteria is None else criteria
    return "\n".join(f"- {c}" for c in items) or "- (none stated)"


def _criteria_provenance_block(ticket: Ticket) -> str:
    """The criteria, grouped by who wrote them.

    Provenance is the whole distinction: a criterion a human put in the plan
    outranks the planner revising the ticket, and one an earlier revision
    invented does not. Without the marks the planner cannot tell which of its
    own past inventions it is allowed to take back.

    Grouped under headings rather than tagged line by line, because the planner
    is asked to return these criteria *verbatim* and a per-line tag is part of
    the line it copies. One run returned all thirteen of a ticket's criteria
    exactly as written, each carrying `_(from the plan — you may not change
    this)_` on the end, and the provenance check scored the same thirteen as
    both dropped and newly invented — a planner doing precisely as it was told,
    reported as trying to gut the contract and raise the bar at once. A heading
    is not part of any line, so there is nothing to carry.
    """
    original = set(ticket.original_criteria)
    # No anchor recorded — a run ingested before originals were kept. Everything
    # is treated as the plan's, which errs toward leaving a human's contract
    # alone.
    if not ticket.original_criteria:
        plan_stated, revision_added = list(ticket.criteria), []
    else:
        plan_stated = [c for c in ticket.criteria if c in original]
        revision_added = [c for c in ticket.criteria if c not in original]

    if not plan_stated and not revision_added:
        return "- (none stated)"

    sections = []
    if plan_stated:
        sections.append(
            "### From the plan — you may not change these\n"
            + "\n".join(f"- {c}" for c in plan_stated)
        )
    if revision_added:
        sections.append(
            "### Added by an earlier revision — you may revise or retire these\n"
            + "\n".join(f"- {c}" for c in revision_added)
        )
    return "\n\n".join(sections)


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
    malformed: str = "",
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

    if malformed:
        # Not a failed attempt — the same answer, rejected before it reached
        # disk because the harness could not read it. Worth its own heading
        # rather than being folded in with the verification failures above: the
        # implementation may be perfectly good and nothing about it should
        # change, which is the opposite of what "your attempt failed" invites.
        body += f"""
## Your last answer could not be read, and nothing was written
{malformed}

Send the same implementation again in the format above. Do not rewrite the
code to fix this — the code was never the problem, and changing it now loses
work that may already have been correct.
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
    rejected_bindings: list[str] | None = None,
    own_file_errors: list[str] | None = None,
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

Read this before writing anything, and decide which of three kinds it is.

A failure that **names your own test file** is about your code, not your
assertions. A compile error, an unused variable, an unused import, a lint the
project denies — these are defects in how the test is written, and the ticket
cannot pass while they stand. Nobody else can fix them: your file is outside
the scope of every other role here, so rewriting the same assertions with the
same unused variable fails the ticket again in exactly this way, and keeps
failing. Fix the line the error points at. Keep the assertions.

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

    if own_file_errors:
        quoted = "\n".join(f"  {line}" for line in own_file_errors)
        body += f"""
## These errors are in the file you are about to write

```
{quoted}
```

Not the implementation's — `{test_path}` is yours, and it is the only file in
this project you can change. Whatever the rest of the failure says, the ticket
cannot pass until these are gone. Fix exactly what they point at and keep every
assertion you had.
"""

    if rejected_bindings:
        quoted = "\n".join(f"  {line}" for line in rejected_bindings)
        body += f"""
## Your last answer was rejected before it reached disk

It declared the code under test as a foreign binding:

```
{quoted}
```

An `extern` block, `dlopen`, `ctypes.CDLL` or `DllImport` *re-declares* a
symbol rather than referencing the one this project builds. The linker has
nothing to resolve it against, so the target fails to **link** — which does not
fail your test, it fails the whole suite, including every other ticket's tests
in the same target.

Call the functions the way the rest of this project calls them: import the
module and call it directly. These tests run on the host, where an exported
function is an ordinary function of its own language.
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
    unchanged: dict[str, str] | None = None,
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

    if unchanged:
        # The retry case, and the one that used to be unrecoverable. A ticket
        # requeued after a failed cycle starts with the previous cycle's work
        # already on disk, so the executor rewrites it byte for byte and git
        # reports no change — while the discarded test file reappears as new.
        # The reviewer then sees a diff holding nothing but tests, concludes
        # the implementation was never written, and rejects. Correctly, on the
        # evidence it was given, forever.
        body += f"""
## Written by this attempt, but identical to what was already on disk
These files are part of this ticket's work. The executor wrote them again this
attempt and the contents matched what was already there, so they do not appear
in the diff above. They are **not** missing. This is their current content —
judge the criteria against it, exactly as if it were in the diff.

{_sources_block(unchanged)}
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


# The headings `review_prompt` writes into its own body. Listed here so
# `strip_prompt_echo` cannot drift from the prompt it is cleaning up after.
#
# Split by how much of a line has to match. The long ones are sentences no
# reviewer writes by accident, so a prefix is safe. The short ones are ordinary
# markdown a reviewer might legitimately quote out of a README it is reviewing,
# so they only count as an echo when the line is nothing else.
_ECHOED_SECTIONS = (
    "## The diff is empty",
    "## Written by this attempt",
    "## You have already rejected this ticket",
    "### Attempt ",
)
_ECHOED_EXACTLY = ("## Spec", "## Acceptance criteria", "## Diff")


def strip_prompt_echo(verdict: str) -> str:
    """Cut a verdict at the first heading the reviewer copied out of its prompt.

    A rejection is fed back to the next attempt as a prior verdict, so anything
    left in it is quoted into the following prompt and offered for copying
    again. One reviewer echoed `## You have already rejected this ticket`,
    complete with the attempt it was shown and the paragraph telling it not to
    invent fresh objections; the block then nested on itself every round.

    Only the tail is dropped, and only from the copy used as a prompt. The
    verdict itself comes first — the format requires the ACCEPT/REJECT line at
    the top — so cutting at the first heading keeps every word the reviewer
    actually wrote about the code. The raw completion is kept whole in the step
    log regardless.
    """
    lines = (verdict or "").splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        echoed = stripped in _ECHOED_EXACTLY or any(
            stripped.startswith(heading) for heading in _ECHOED_SECTIONS
        )
        if echoed:
            return "\n".join(lines[:index]).strip()
    return (verdict or "").strip()


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
- Never write anything about how the executor should format its reply. Fences,
  backticks, where the file path goes, whether contents are "raw" — all of that
  is fixed by the harness, which states it to the executor directly and parses
  what comes back. A failure that looks like a formatting problem is not yours
  to fix, and a spec that contradicts the harness makes the ticket impossible:
  one told the executor not to use code fences, when a fence is the only thing
  the parser can read. Say it in the rationale instead.
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
}

`context` is read by the executor as established fact. Put conclusions there,
never your reasoning towards them: "the board is 10x20, not 20x20" belongs in
it, "let me re-verify whether the sequence could be..." does not.

Reply with `{"impossible": "..."}` instead when the ticket cannot be made
satisfiable — see the section on that below, if this ticket has one."""


def respec_prompt(
    ticket: Ticket,
    failures: list[dict[str, str]],
    *,
    sources: dict[str, str] | None = None,
    criteria_locked: bool = True,
) -> list[Message]:
    """Ask the planner to fix a ticket that its own executor could not satisfy.

    `sources` is the code this ticket owns and reads, as it exists right now.
    Without it the planner is doing the thing this pipeline forbids everywhere
    else — writing about a codebase it cannot see. It wrote "SoftDrop
    decrements y" into a spec whose implementation increments it, and the
    executor was then judged against the planner's guess.
    """
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
"""

    # Listed once. When the criteria are scoped by provenance they are shown
    # below with their marks instead, and printing them twice invites a reply
    # that revises one copy.
    if not criteria_locked:
        body += f"""
## Current acceptance criteria
{_criteria_block(ticket)}
"""

    body += f"""
## Current context
{ticket.context.strip() or "(none)"}
"""

    if ticket.drifted:
        # The anchor. Each revision is derived from the last, so without the
        # ingested text in front of it the planner cannot tell its own
        # accumulated drift from what a human actually asked for — and it will
        # keep revising away from the plan, one plausible step at a time.
        body += f"""
## What this ticket said when the plan was ingested
This is the human-authored original. The "current" text above is what earlier
revisions have made of it. Where the two disagree, the original is the intent;
treat any difference you cannot justify from the failures below as drift you
should undo rather than build on.

### Original spec
{ticket.original_spec}

### Original acceptance criteria
{_criteria_block(ticket, ticket.original_criteria)}
"""

    if sources:
        body += f"""
## The code as it exists right now
These are the real contents of the files this ticket writes and reads. Every
statement you make about how this code behaves must be checked against them.
Do not describe a function, a field, a coordinate convention, or an index base
that contradicts what is here — if the code and the current spec disagree,
say which one you are changing and why.

{_sources_block(sources)}
"""

    body += f"""
## What happened, oldest attempt first
{evidence}
"""

    if criteria_locked:
        # Scoped by provenance rather than frozen outright. A blanket freeze
        # made a machine-invented criterion as immutable as a human-authored
        # one, so the loop could mint an impossible criterion and then never
        # retire it — which is exactly what happened, and then the planner
        # rewrote the spec around it instead.
        body += f"""
## What you may do to the acceptance criteria

{_criteria_provenance_block(ticket)}

Return `criteria` as the complete list you want the ticket to have. The rules
applied to it:

- Criteria under **From the plan** are a human's contract. Copy each one back
  exactly as written, with nothing appended. Drop or reword one and it will be
  put back, and the attempt to change it reported.
- Criteria under **Added by an earlier revision** are the loop's own. Revise
  them, or leave them out to retire them, if the evidence says they were wrong.
- Anything else you list is added. Add a criterion when the failures show
  behavior nobody asked for, or when the spec requires something no criterion
  checks. Never add one to describe a bug the attempts happened to produce.

Omit `criteria` entirely to leave them exactly as they are.

## If a criterion cannot be satisfied at all
Some criteria are not wrong, they are impossible: two that contradict each
other, or one asserting a specific value that no implementation of this spec
produces. Do not rewrite the spec to chase it, and do not weaken it by hand.

Reply with an `impossible` field naming the criterion and the contradiction,
in plain terms a human can check. That parks the ticket for a person to settle
and costs nothing further. It is the right answer, not a failure to answer:

{{"impossible": "Criterion 3 requires Game::new(1) to yield [6, 3, 5, 7, 4]. \
No xorshift32 with the shifts this spec defines produces that sequence — \
seed 1 yields [2, ...]. Either the constant or the criterion is wrong, and \
nothing in the failures says which."}}
"""

    body += "\nRevise the ticket so the next attempt can succeed."

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
    for key in ("spec", "context", "rationale", "impossible"):
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

    # A ticket whose criteria cannot be satisfied has no revised spec to give,
    # and demanding one is what produced a planner that changed an xorshift
    # constant to chase a sequence no xorshift produces. Reporting the
    # contradiction is a complete answer.
    if not revision.get("spec") and not revision.get("impossible"):
        raise ValueError("planner reply carried no revised spec")
    return revision
