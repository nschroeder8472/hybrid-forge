"""Prompts for each role.

Kept in one file because they are a contract, not implementation detail: the
executor prompt's rules about scope and `BLOCKED:` are what `patch.py` parses,
and the reviewer's `REJECT` verdict is what the loop branches on. Changing the
wording without changing the parser is how an autonomous loop starts silently
accepting work it should have rejected.
"""

from __future__ import annotations

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
  file. No partial files, no diffs, no ellipses.
"""

TESTER_SYSTEM = """You write tests that encode criteria decided upstream.

You are given acceptance criteria authored during planning. Encode exactly
those criteria as assertions. Do not invent additional criteria, and do not
weaken a criterion to make it easier to satisfy.

Output the complete contents of each test file: the path on its own line, then
a fenced code block with the whole file.
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


def build_prompt(
    ticket: Ticket, failure_context: str = "", retrieved: str = ""
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

    if failure_context:
        body += f"""
## Your previous attempt failed verification
Fix the cause. Do not work around the check.

{failure_context}
"""

    body += "\nImplement this now."
    messages.append(Message(role="user", content=body))
    return messages


def tests_prompt(ticket: Ticket, changed_files: list[str]) -> list[Message]:
    files = "\n".join(f"- {p}" for p in changed_files) or "- (none)"
    body = f"""Ticket: {ticket.ticket_id} — {ticket.title}

## Files under test
{files}

## Criteria to encode as assertions
{_criteria_block(ticket)}

Follow the test framework and conventions already used in this repository.
"""
    return [
        Message(role="system", content=TESTER_SYSTEM),
        Message(role="user", content=body),
    ]


def review_prompt(ticket: Ticket, diff: str, retrieved: str = "") -> list[Message]:
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
    messages.append(Message(role="user", content=body))
    return messages


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
