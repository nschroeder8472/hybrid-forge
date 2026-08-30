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
from collections import Counter
from typing import Any, Sequence

from .failures import distill
from .providers import Message
from .state import Ticket

# The prefixes that mark a message as droppable to the budget gate. Memory and
# ticket context live behind the first; the spec never does.
CONTEXT_HEADING = "## Established project context"

# History, carried in its own message so it can be trimmed instead of blocking
# the ticket. Both are worth having and neither is worth a ticket: an executor
# without its earlier failures may oscillate, a reviewer without its earlier
# verdicts may raise a fresh objection — a ticket that will not fit does none
# of the work at all.
PRIOR_FAILURES_HEADING = "## Earlier attempts on this ticket, oldest first"

# The tally that says a mistake is being repeated rather than merely made. Its
# own message, and droppable, for the same reason the raw history is.
FAILURE_CLASSES_HEADING = "## What this ticket keeps failing on"

# What earlier attempts established about the repository, as opposed to what
# they failed on. Droppable like the rest of the history: it is worth having
# and it is not worth the ticket.
LEARNED_HEADING = "## What earlier attempts on this ticket established"
PRIOR_VERDICTS_HEADING = "## You have already rejected this ticket"

# The feedback turn in a conversational executor prompt, for every attempt but
# the newest. Marked so the gate can drop an old exchange whole.
PRIOR_ATTEMPT_HEADING = "## That attempt failed"

# What the roles settled before the ticket was built, carried into every prompt
# that acts on it. Droppable: it is the reason behind a contract, and the
# contract itself is stated in full a few lines further down whatever it costs.
RATIFICATION_HEADING = "## Settled before any code was written"

# The linter, compiler and runner configuration this repository grades code
# with. Droppable, and it is the first thing that should go: losing it costs
# the role a rule it can still infer from a failure, while losing the ticket
# costs the attempt outright. Carried at all because inferring those rules from
# failures is exactly what one run spent 512 attempts doing — see
# docs/CONVERGENCE.md.
TOOLCHAIN_HEADING = "## How this project checks the code you write"

# The path root every format example in this module uses. A small model shown a
# worked example will sometimes return the example along with its answer, and
# those edits are then rejected for being out of scope — which reads, to
# everything downstream, exactly like the ticket asking for scope it needs. One
# run's planner spent six revisions rewriting a spec around the two Java files
# from the example above, neither of which existed in the repository.
#
# So the examples are rooted somewhere no real tree can be, and the rejection is
# reported as what it is: a formatting mistake, not a scope request.
EXAMPLE_PATH_PREFIX = "EXAMPLE-ONLY/"

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
- If you believe NO implementation could satisfy this ticket as written — two
  criteria demanding different results from the same call, a criterion asserting
  a value the spec's own algorithm cannot produce — add a line starting
  `IMPOSSIBLE:` naming the criteria and the contradiction. Send your best
  implementation with it; both are read. `BLOCKED:` means you need something;
  `IMPOSSIBLE:` means the ticket is wrong, and the planner is asked to confirm
  or refute it rather than taking your word.
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

The format, exactly. The path goes OUTSIDE the fence, on the line above it:

EXAMPLE-ONLY/first_file.txt
```
the entire contents of the first file
```

EXAMPLE-ONLY/second_file.txt
```
the entire contents of the second file
```

Those two `EXAMPLE-ONLY/` paths are the shape of an answer, not part of one.
Never send them back. Every path in your reply comes from the allowed scope.

Nothing else is read. These are the ways a reply gets discarded, and the first
is by far the most common:

WRONG — the path is inside the fence, as a comment. Nothing is written:

```
// EXAMPLE-ONLY/first_file.txt
the entire contents of the first file
```

WRONG — the path is inside the fence, on the first line. Nothing is written,
and if it were, that line would become part of the file:

```
EXAMPLE-ONLY/first_file.txt
the entire contents of the first file
```

WRONG — a fenced block with no path anywhere. There is nothing to write it to:

```
the entire contents of the first file
```

The path line carries no decoration: no `//`, no `#`, no bullet, no bold, no
backticks, no heading marks. Just the path, then a newline, then the fence.
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
- Reshape a value before comparing it to what a criterion pins. A helper of
  your own that masks, shifts, scales or offsets the call's result — `>>> 0`,
  `& 0xFFFFFFFF`, `% 256` — turns the criterion into something it does not say
  and the assertion into one that cannot fail for the reason it exists. If the
  value comes back in the wrong form, report that; do not correct it on the way
  to the comparison.
- Write anything to a path other than the one file you are told to write.

Output the complete contents of that one test file: the path on its own line,
then a fenced code block with the whole file. The path goes OUTSIDE the fence,
on the line above it, carrying no comment marker and no other decoration:

EXAMPLE-ONLY/your_test_file.txt
```
the entire contents of the test file
```

That path is the shape of an answer, not part of one. Write the path you were
actually given; a reply naming any other is discarded.

A path written inside the fence — as `// src/test/...`, or as a bare first
line — is not read as a path. Nothing is written and the answer is discarded.
"""

REPRO_SYSTEM = """You write the test that proves a bug is real.

You are given a bug report and the code it is about. Write one test that
asserts the behavior the report says the code SHOULD have. Run against the code
as it stands today, that test must FAIL — that failure is the whole point of
it, and it is what earns the fix an attempt.

This is the opposite of your usual instruction, so be exact about it:

- Assert the CORRECT behavior. Never encode the fault as though it were
  expected: a test asserting that three pieces lock is a test that passes today
  and passes forever, and it certifies the bug instead of catching it.
- Fail for the reported reason and no other. `assert False`, a syntax error, an
  import of something that does not exist — all of those fail, and none of them
  proves anything. The failure must be the assertion you wrote comparing what
  the code does against what it should do.
- Assert on behavior through the public interface. This test outlives the fix
  and runs on every later ticket in this project, so it must keep testing the
  behavior rather than the arrangement of the code.
- If the report is too vague to assert anything specific, say so instead of
  guessing. Reply with a single line starting `BLOCKED:` naming what you would
  need to know. A test written from a guess proves nothing and is then trusted
  by everything downstream.

Never:
- Read a source file and assert on its text, or assert that a file exists.
- Assert on the exact whole contents of any file.
- Declare foreign-function bindings (`extern`, `dlopen`, `ctypes`) against the
  code under test. Import it the way the rest of the project imports it.
- Write anything to a path other than the one file you are told to write.

Output the complete contents of that one test file: the path on its own line,
then a fenced code block with the whole file. The path goes OUTSIDE the fence,
on the line above it, carrying no comment marker and no other decoration:

EXAMPLE-ONLY/your_test_file.txt
```
the entire contents of the test file
```

That path is the shape of an answer, not part of one. Write the path you were
actually given; a reply naming any other is discarded.

A path written inside the fence — as `// src/test/...`, or as a bare first
line — is not read as a path. Nothing is written and the answer is discarded.
"""

BUG_PLANNER_SYSTEM = """You turn a bug report into one implementable ticket.

A bug report is not a plan. It describes a symptom — what somebody saw — and
your job is to say which code is responsible and what it should do instead. You
are given the repository's file list and every place the report's own words
appear in the code, because you cannot open files yourself.

Rules:

- Name the files that must change in `allowed_files`, and be honest about
  uncertainty: list the files the fix plausibly touches, not every file that
  mentioned the words. Nothing outside that list can be written, so a list that
  is too narrow blocks the ticket and a list of forty files makes the scope
  meaningless.
- Put files the fix must be read against — the caller, the type, the module it
  has to stay consistent with — in `reference_files`. The executor has no
  filesystem and sees only what the ticket carries.
- The spec states the defect and the behavior that should replace it, in terms
  of this codebase. "Handle the timing better" is not a spec. "`Game::tick`
  drains its accumulator with a loop that can lock several pieces in one frame;
  it should lock at most one per tick and reset the accumulator on lock" is.
- Do not describe the fix as a diff or name the lines to change. State the
  behavior; the executor is the one reading the code.
- Acceptance criteria are optional here and usually unnecessary: a reproduction
  test is written before any fix is attempted, and that test is the contract.
  Add one only for a consequence the reproduction cannot check.
- If the report cannot be located in this repository at all — nothing it names
  exists, or it describes a different project — say so in `unclear` rather than
  inventing a plausible ticket.

Reply with a single JSON object and nothing else:

{"title": "...", "spec": "...", "allowed_files": ["..."],
 "reference_files": ["..."], "criteria": [],
 "reproduce": "what a failing test should assert, in one sentence"}
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


STUCK_REVIEWER_SYSTEM = """You are asked a different question from the usual
review. This ticket's last few cycles failed on exactly the same things, so
whether its current diff meets the bar is already known — it does not. The
question is whether the ticket can be met at all as written.

You are the only role positioned to ask it. The executor is trying to satisfy
the ticket and reads every failure as its own; the planner rewrites the ticket
from those failures and will keep finding a plausible rewrite. You are being
shown the contract and the evidence side by side and asked whether they agree.

Three answers, and the middle one is the common case:

VERDICT: unwinnable
  No implementation of this spec satisfies these criteria. Two criteria demand
  different results from the same call; a criterion asserts a value the spec's
  own algorithm cannot produce; the scope excludes the file that would have to
  change. Name the specific clause and the specific contradiction, in terms a
  person can check against the text above without rerunning anything.

VERDICT: winnable
  The ticket is satisfiable and the attempts have been going about it wrongly.
  Say what the attempts keep doing and what they would have to do instead. Be
  concrete: "every attempt indexes the lookup table directly; the type checker
  requires a guard first" is useful, "try a different approach" is not.

VERDICT: unclear
  You cannot tell from what you were shown. This is a real answer and it is
  better than a confident guess in either direction — a wrong `unwinnable`
  parks work that would have landed, and a wrong `winnable` spends another
  dozen cycles.

Never:
- Judge the implementation's quality. It failed; that is established.
- Propose a revised spec or new criteria. That is the planner's, and you are
  being asked about the contract, not writing one.
- Repeat the failure text back. It is above; say what it means.

Reply with the verdict line, then two to five sentences and nothing else."""


CONVENTION_RECORDER_SYSTEM = """You decide whether a ticket that never passed
established anything about this project worth writing to long-term memory.

This ticket failed. Its implementation was never verified and is being taken
back out of the tree, so **nothing about the work itself may be recorded** — not
what the approach was, not why it might have been right, not what the next
attempt should try. A conclusion drawn from unverified code is a rumour that
every future ticket will read as fact.

What can be recorded is narrower and comes from one place: the list below of
things earlier attempts established about the repository. Those were derived
from what the project's own linters, compilers and test runners actually said,
which is evidence about the *project* rather than about this ticket's code. A
compiler flag is true whether or not the ticket that discovered it passed.

Record ONLY a convention or constraint of the project that a future ticket in
any part of this repository would need to know:
- "The type checker runs with noUncheckedIndexedAccess, so every index access
  needs a guard."
- "Imports in this package resolve with an explicit .js extension."

Never record:
- Anything about this ticket's implementation, approach, or what went wrong
  with it. It failed, and why it failed is in the run log where it belongs.
- A convention that applies only to the files this one ticket owned.
- A restatement of the spec, the criteria, or the failure text.
- Transient state: attempt counts, error messages, what the tests printed.
- Credentials, tokens, keys, or connection strings, in any form.

If the list below holds nothing that generalizes past this ticket — which is
the common case — say NOTHING.

Reply with exactly one of:

NOTHING
  (on its own line)

or

TITLE: <one short line naming the convention>
<one to three sentences stating it as a fact about this project, for someone
reading it months from now who has never heard of this ticket.>
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
    # The ratified criteria where a sign-off pass settled them, the plan's
    # otherwise. Both are somebody's decision on the record, which is what the
    # protection is about — and after ratification the ingested text is a draft
    # four roles have already superseded.
    original = set(ticket.contract_criteria)
    # No anchor recorded — a run ingested before originals were kept. Everything
    # is treated as the plan's, which errs toward leaving a human's contract
    # alone.
    if not ticket.contract_criteria:
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


def _toolchain_message(toolchain: dict[str, str] | None) -> Message | None:
    """The configuration the verify commands enforce, as its own message.

    Its own message rather than a section of the ticket body, for the same
    reason retrieved context is: the budget gate drops whole messages, and a
    repository with a large linter config must not be able to stop a ticket
    fitting. Ahead of the ticket, because it is background the role should read
    before the work rather than a demand made of it.

    Stated as *what will reject this code*, not as *reference*. The reference
    block says take the signatures from here; this says the check has already
    been configured and you are being measured against it. A role that reads
    `noUncheckedIndexedAccess: true` writes the guard on the first attempt; one
    shown the same file as ordinary reference reads it as somebody else's.
    """
    if not toolchain:
        return None
    return Message(
        role="user",
        content=f"""{TOOLCHAIN_HEADING}
These are this repository's real linter, compiler and test-runner settings, at
the paths shown. The verify commands enforce them, so code that breaks one of
these fails before anyone reads it, and the failure will look like a mistake in
your implementation rather than a rule you were not told about.

Read them as constraints on what you write. You are not asked to change them
and they are not in your scope.

{_sources_block(toolchain)}
""",
    )


def _classes_message(classes: Sequence[dict]) -> Message | None:
    """The kinds of failure this ticket has produced, counted.

    The raw history says what went wrong last time and the time before. It
    cannot say *this is the fortieth time*, because two instances of one
    mistake are two different strings — `TS2532` at line 40 and at line 51, an
    assertion quoting a hash that differs every run. So the anti-oscillation
    paragraph below it, which has been in this prompt all along, could never
    fire: the executor was shown two failures and asked to notice a cycle 400
    attempts long.

    Counted, it is one line. See docs/CONVERGENCE.md.
    """
    repeated = [entry for entry in classes if entry.get("count", 0) > 1]
    if not repeated:
        return None
    lines = []
    for entry in repeated:
        span = (
            f" — first seen on attempt {entry['first_attempt']}, "
            f"last on attempt {entry['last_attempt']}"
            if entry["last_attempt"] > entry["first_attempt"]
            else ""
        )
        lines.append(f"- {entry['name']} — {entry['count']} times{span}")
    return Message(
        role="user",
        content=f"""{FAILURE_CLASSES_HEADING}
Each line is one kind of mistake, counted across every attempt this ticket has
had, including earlier retry cycles. The exact line numbers and values differ
between them; the mistake does not.

{chr(10).join(lines)}

A count in double figures is not a hard problem being worked on, it is the
same fix being tried repeatedly. Read the rule it breaks — the compiler and
linter settings are above — and change the approach rather than the line.
""",
    )


# What a learning is *about*: the named thing it makes a claim on. Backticked
# spans first, because a model writing about a flag or an API almost always
# quotes it, then the bare spellings of the same shapes for the entries that do
# not — `OS.exit_code` written plain is the same subject as `` `OS.exit_code` ``,
# and the pair that contradicted each other on one run was split exactly that
# way.
_BACKTICKED = re.compile(r"`([^`\n]{1,60})`")
_BARE_CODE = re.compile(
    # `quit()`, `load_file()`; dotted names; snake_case; file extensions.
    r"\b[A-Za-z_][\w]*\(\)|\b[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)+|"
    r"\b[a-z]+(?:_[a-z0-9]+)+\b|(?<![\w.])\.[a-z]{1,4}\b"
)
# Ordinary prose that survives the bare patterns above and is nobody's subject.
_NOT_A_SUBJECT = frozenset({"e.g", "i.e", "etc", "vs"})


def _subjects(text: str) -> set[str]:
    """The named things a learning makes a claim about."""
    found = {token for token in _BACKTICKED.findall(text)}
    found |= set(_BARE_CODE.findall(text))
    cleaned = set()
    for token in found:
        token = token.strip().strip(".,;:").lower()
        # A backticked sentence fragment is a quotation, not a subject, and a
        # backticked operator is not a name. `!` was read as one, which made
        # every entry advising a non-null assertion share a subject with every
        # entry explaining why one is needed — and the two were then reported
        # as disagreeing.
        if len(token.split()) > 3 or token in _NOT_A_SUBJECT:
            continue
        if len(token) < 2 or not any(char.isalpha() for char in token):
            continue
        cleaned.add(token)
    return cleaned


# Whether a learning says its subject is called for or ruled out. Checked in
# this order: "must not" is a prohibition and contains "must".
_RULES_OUT = re.compile(
    r"\b(?:must not|cannot|can not|does not|do not|is not|are not|will not|"
    r"never|no longer|not supported|rejects?|forbids?|disallows?|unsupported)\b",
    re.IGNORECASE,
)
_CALLS_FOR = re.compile(
    r"\b(?:must|requires?|needs?|always|has to|have to|should)\b", re.IGNORECASE
)


def _stance(text: str) -> str:
    """`"out"`, `"for"`, or `""` — what the learning asks be done with its subject."""
    if _RULES_OUT.search(text):
        return "out"
    if _CALLS_FOR.search(text):
        return "for"
    return ""


# How many statements about one subject are worth showing. Two, because the
# second is usually the one carrying the detail the first left out, and a third
# on one run was the ninth restatement of `.js` extensions in a list of twelve.
_PER_SUBJECT = 2


def contested_subjects(entries: Sequence[dict]) -> set[str]:
    """Things earlier attempts could not agree about.

    A subject one learning says is required and another says is not supported.
    Both cannot be facts about this repository, and the prompt introduces this
    list as conclusions about how the project works — so a role reads
    `gdUnit4 requires an import at the top of a test file` and acts on it. That
    entry sat beside `GDScript does not support import for scripts` for the
    whole of one ticket's eighty-four builds, and the language has no `import`.

    Marked, not resolved, and never withheld. Counting which side was reached
    more often looked like a tiebreak and is not one: on the same ticket it
    would have suppressed `Tool scripts with class_name are not visible to
    gdUnit4 tests at parse time unless explicitly preloaded` — true, and the
    single most useful line in the list — because four other entries mentioned
    `class_name` while requiring something. A subject many statements are made
    about is not a subject in dispute, and nothing here can tell the two apart.

    What is safe is saying so. An entry the loop flatly disagreed with itself
    about should not be read as established, and a role told that much can go
    and look instead of building on it.
    """
    stances: dict[str, set[str]] = {}
    for entry in entries:
        text = entry.get("text", "")
        stance = _stance(text)
        if not stance:
            continue
        for subject in _subjects(text):
            stances.setdefault(subject, set()).add(stance)
    return {subject for subject, sides in stances.items() if len(sides) > 1}


def _one_subject_at_a_time(entries: Sequence[dict], limit: int) -> list[dict]:
    """`limit` entries, no more than `_PER_SUBJECT` of them about one thing.

    The list is ordered by how often each conclusion was rediscovered, and on a
    ticket that kept rediscovering one convention that ordering hands the whole
    budget to it. One prompt carried twelve learnings of which seven restated
    that local imports need a `.js` extension, while the entries about
    `noUncheckedIndexedAccess` — the rule that ticket kept actually failing on
    — were crowded out of the list entirely.

    An entry is skipped when *any* subject it names is already at the cap, not
    when all of them are. Every restatement of a convention arrives carrying
    one or two other tokens as well, so the weaker test lets all seven through
    on the strength of what they mention in passing.

    A rediscovered fact still deserves the top of the list; it does not deserve
    seven places in it. An entry naming nothing in particular is never crowded
    out, since it shares no subject with anything.
    """
    seen: Counter[str] = Counter()
    kept: list[dict] = []
    for entry in entries:
        # Tested before the append, not after: `learnedLimit: 0` turns the
        # whole section off, and a loop that appends first honours it as one.
        if len(kept) >= limit:
            break
        subjects = _subjects(entry.get("text", ""))
        if any(seen[subject] >= _PER_SUBJECT for subject in subjects):
            continue
        for subject in subjects:
            seen[subject] += 1
        kept.append(entry)
    return kept


ADVICE_HEADING = "## What a person said about this ticket"


def advice_message(ticket: Ticket, limit: int = 8) -> Message | None:
    """Notes a human wrote against this ticket, newest last.

    The loop has seven ways to hand a ticket back and, until this, no way to be
    handed anything in return. Every exit wrote a sentence to a person who
    could not write one back.

    Framed the way `learned` is framed, with one difference stated in as many
    words: this was written by a person about this ticket, and it outranks what
    earlier attempts concluded. `learned` is what the loop worked out from its
    own failures; a note is what somebody who can read the repository decided.
    Where they disagree, the loop is the one that has been wrong before.

    Never concatenated into a system message, and rendered under its own
    heading like every other history block. Text that entered the harness from
    outside must not be able to imitate the harness — the same reason
    `strip_prompt_echo` exists.

    Not shown to the reviewer. What the reviewer is shown is the bar, and the
    bar moves through criteria or it does not move; a person who wants it moved
    adds a criterion, which `forge criteria --add` lets them do.
    """
    entries = [note for note in (ticket.human_note or []) if note.get("text")]
    if not entries:
        return None
    # Newest last, so the most recent thing a person said is the last thing
    # read before the ticket itself.
    shown = entries[-limit:]
    dropped = len(entries) - len(shown)
    lines = [f"- {note['text'].strip()}" for note in shown]
    older = (
        f"\n\n({dropped} earlier note(s) not shown.)" if dropped else ""
    )
    return Message(
        role="user",
        content=f"""{ADVICE_HEADING}
Written by a person about this ticket, after seeing where it got stuck. This
outranks anything earlier attempts concluded: those are the loop's own guesses
from its own failures, and this is somebody who can read the repository.

It is not an acceptance criterion. What you are judged against is the criteria
below and nothing else — if a note asks for something the criteria do not, the
criteria are what the reviewer will read.

{chr(10).join(lines)}{older}""",
    )


def learned_message(ticket: Ticket, limit: int = 12) -> Message | None:
    """What earlier attempts worked out about this repository.

    Facts, not demands. Nothing downstream enforces a line of this: the
    reviewer is not shown it, no criterion is minted from it, and a role that
    ignores one is not failing anything — which is what keeps it out of the
    criteria ratchet's jurisdiction. The ratchet stops the loop raising its own
    bar; this stops the loop forgetting.

    Ordered by how often the loop has had to rediscover each one, because that
    ordering is itself the signal: a conclusion reached on four separate cycles
    is one the plan should have stated, and it should be the first thing the
    next attempt reads.
    """
    all_entries = [entry for entry in (ticket.learned or []) if entry.get("text")]
    contested = contested_subjects(all_entries)
    entries = _one_subject_at_a_time(all_entries, limit)
    if not entries:
        return None
    lines = []
    disputed = False
    for entry in entries:
        count = int(entry.get("count", 1))
        again = f"  (established {count} separate times)" if count > 1 else ""
        if _subjects(entry["text"]) & contested:
            again += "  [earlier attempts disagreed about this — check it]"
            disputed = True
        lines.append(f"- {entry['text']}{again}")
    note = ""
    if disputed:
        note = (
            "\n\nA line marked as disputed is one earlier attempts contradicted "
            "each other on, so it is not established and should not be built on. "
            "Check it against the files you were given."
        )
    return Message(
        role="user",
        content=f"""{LEARNED_HEADING}
These are conclusions earlier attempts reached about this repository, kept so
you do not have to reach them again. They are what the loop worked out from
what this project's tools reported, not requirements you are judged against —
the acceptance criteria below are the bar, and nothing here adds to it.

{chr(10).join(lines)}{note}
""",
    )


def build_prompt(
    ticket: Ticket,
    failure_context: str = "",
    retrieved: str = "",
    sources: dict[str, str] | None = None,
    *,
    toolchain: dict[str, str] | None = None,
    learned_limit: int = 12,
    failure_classes: Sequence[dict] = (),
    prior_failures: Sequence[str] = (),
    malformed: str = "",
    prior_turns: Sequence[tuple[str, str]] = (),
) -> list[Message]:
    """The executor's prompt, in one of two shapes.

    `prior_turns` selects the second. Given `(reply, what failed)` pairs it
    writes a real exchange — the ticket, then each of the executor's own
    answers as an `assistant` turn with the failure that followed as the reply
    to it — instead of one user message that mutates every attempt. What that
    buys is the thing the flat shape cannot say: *you wrote these files*. Shown
    the same files as disk state with no such claim, a model reads its own work
    as somebody else's and answers "they already implement the spec correctly".

    The flat shape stays the default and stays here rather than in a second
    function: everything above the failure history is identical, and two copies
    of the spec block would drift.
    """
    messages = [Message(role="system", content=EXECUTOR_SYSTEM)]

    context = _context_message(ticket, retrieved)
    if context is not None:
        messages.append(context)

    rules = _toolchain_message(toolchain)
    if rules is not None:
        messages.append(rules)

    # After the toolchain settings and before the ticket: these are usually
    # conclusions *about* those settings, and they read as a gloss on them.
    established = learned_message(ticket, learned_limit)
    if established is not None:
        messages.append(established)

    # After the loop's own conclusions, because where the two disagree
    # the person is the one who has not already been wrong about this
    # ticket, and the last thing read before the ticket should be theirs.
    advice = advice_message(ticket)
    if advice is not None:
        messages.append(advice)

    settled = ratification_message(ticket)
    if settled is not None:
        messages.append(settled)

    # Its own message, ahead of the ticket, because the gate drops whole
    # messages and this is one the executor can lose and still do the work.
    # Without it it sees only the newest failure and can oscillate: a change
    # that fixes A breaks B, the fix for B brings A back, and three attempts go
    # by with nothing able to see the cycle.
    #
    # Superseded by the turns themselves when there are any: the same failures,
    # each one attached to the answer that caused it.
    # Ahead of the raw history: the count is the fact that changes what the
    # next attempt should do, and the history is the detail it works from.
    counted = _classes_message(failure_classes)
    if counted is not None:
        messages.append(counted)

    if prior_failures and not prior_turns:
        earlier = "\n\n".join(distill(entry, limit=800) for entry in prior_failures)
        messages.append(
            Message(
                role="user",
                content=f"""{PRIOR_FAILURES_HEADING}
These already failed. A fix you have tried before will fail the same way
again — if the newest failure is one you have already seen here, the two
changes are undoing each other, and you need a third approach that satisfies
both rather than alternating between them.

{earlier}
""",
            )
        )

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

    tail = ""
    if failure_context:
        tail += f"""
## Your previous attempt failed verification
Fix the cause. Do not work around the check.

{failure_context}
"""

    if malformed:
        # Not a failed attempt — the same answer, rejected before it reached
        # disk because the harness could not read it. Worth its own heading
        # rather than being folded in with the verification failures above: the
        # implementation may be perfectly good and nothing about it should
        # change, which is the opposite of what "your attempt failed" invites.
        tail += f"""
## Your last answer could not be read, and nothing was written
{malformed}

Send the same implementation again in the format above. Do not rewrite the
code to fix this — the code was never the problem, and changing it now loses
work that may already have been correct.
"""

    if not prior_turns:
        messages.append(Message(role="user", content=body + tail + "\nImplement this now."))
        return messages

    # The ticket as it was first asked, unchanged by what happened next: this
    # is the turn the executor already answered, and rewriting it now would
    # make its own replies look like answers to a question nobody asked.
    messages.append(Message(role="user", content=body + "\nImplement this now."))

    for reply, failed in prior_turns[:-1]:
        messages.append(Message(role="assistant", content=reply))
        messages.append(
            Message(
                role="user",
                content=f"{PRIOR_ATTEMPT_HEADING}\n{distill(failed, limit=800)}",
            )
        )

    last_reply, last_failed = prior_turns[-1]
    messages.append(Message(role="assistant", content=last_reply))
    # The newest failure is the instruction for this attempt, so it is stated
    # in full and outside the droppable headings. `failure_context` is the same
    # failure the loop has in hand; the stored one stands in when a caller has
    # not passed it.
    newest = tail or f"""
## Your previous attempt failed verification
Fix the cause. Do not work around the check.

{last_failed}
"""
    messages.append(
        Message(
            role="user",
            content=newest
            + "\nReturn the complete files again, in the format above.",
        )
    )
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
    toolchain: dict[str, str] | None = None,
    learned_limit: int = 12,
    rejected_bindings: list[str] | None = None,
    laundered: list[str] | None = None,
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

    if laundered:
        quoted = "\n".join(f"  {line}" for line in laundered)
        body += f"""
## Your last answer was rejected before it reached disk

It compared a reshaped value instead of the one the code returned:

```
{quoted}
```

A criterion pins what the implementation must produce. Putting the call through
a helper you defined — `>>> 0`, `& 0xFFFFFFFF`, `% 256`, a scale, an offset —
before comparing means the assertion no longer says anything about that
criterion. It passes for an implementation that returns the wrong value in the
right bits, which is precisely the bug the criterion exists to catch.

Assert on the call itself. If the value comes back in a form the criterion does
not describe, that is the implementation's defect to fix and your test's job to
report — not something to correct on the way to the comparison.
"""

    messages = [Message(role="system", content=TESTER_SYSTEM)]
    # The tester is graded by the same lint and type checks as the executor,
    # and on one run it was the tester's file that carried 117 of the 160 lint
    # failures — every one of them trailing whitespace, against a config it had
    # never been shown.
    rules = _toolchain_message(toolchain)
    if rules is not None:
        messages.append(rules)
    # The tester rediscovers a convention as readily as the executor does, and
    # on one run it was the tester's file that kept breaking the linter.
    established = learned_message(ticket, learned_limit)
    if established is not None:
        messages.append(established)

    # After the loop's own conclusions, because where the two disagree
    # the person is the one who has not already been wrong about this
    # ticket, and the last thing read before the ticket should be theirs.
    advice = advice_message(ticket)
    if advice is not None:
        messages.append(advice)
    # What the roles settled before any code existed. The tester is the role
    # most likely to have asked for a criterion to be made measurable, and it
    # should see whether it got it rather than rediscovering the same problem
    # while writing the assertion.
    settled = ratification_message(ticket)
    if settled is not None:
        messages.append(settled)
    messages.append(Message(role="user", content=body))
    return messages


def bug_prompt(
    report: str, evidence: str = "", sources: dict[str, str] | None = None
) -> list[Message]:
    """Ask the planner to turn a prose bug report into one ticket.

    `evidence` is gathered by the harness — the file list and where the
    report's words appear — because the planner has no filesystem and the file
    that needs changing is exactly what is being looked for. Without it a
    planner writes a confident ticket scoped to files that do not exist.
    """
    body = f"""## The report, as it was written
{report.strip()}
"""
    if evidence.strip():
        body += f"""
## What is actually in this repository
You cannot open files. This is what the harness found, and every path you name
must come from it.

{evidence.strip()}
"""
    else:
        body += """
## No repository evidence was available
Nothing could be gathered — this may not be a git checkout. Name files only if
the report itself names them, and say so in `unclear` if you cannot.
"""

    if sources:
        # The files a survey pass asked for, read by the harness. This is the
        # difference between a ticket written from filenames and one written
        # from the code: the defect is usually visible here, and the spec
        # should describe *it* rather than the symptom that led to it.
        body += f"""
## The code itself
These are the files worth reading, in full. State the defect in terms of what
is actually here — the function, the condition, the value it produces — rather
than restating the report. If none of this can produce the reported behavior,
say so in `unclear` instead of picking the nearest plausible file.

{_sources_block(sources)}
"""

    body += "\nWrite the ticket now."
    return [
        Message(role="system", content=BUG_PLANNER_SYSTEM),
        Message(role="user", content=body),
    ]


LOCATE_SYSTEM = """You are finding where a reported problem lives.

You are given a bug report and what the harness could gather from the
repository: its files, every place the report's words appear, and — when the
report named nothing specific — the definitions in the project. You cannot open
files yourself, so this pass exists to ask for the ones worth reading.

Name the files most likely to contain the fault. Prefer few: the point is to
read them properly, not to skim the project. Six is plenty and two is often
right. Reason from what the code is *for* rather than from the words alone — a
report about a score that stops updating belongs wherever scoring happens, even
if the word "score" appears nowhere near it.

Every path must come from the evidence exactly as written there. A path you
invent is a file that will not be read, and this pass will have found nothing.

Reply with JSON and nothing else:

{"candidates": ["src/game.rs", "src/board.rs"],
 "reasoning": "one sentence on why these"}
"""


def locate_prompt(report: str, evidence: str) -> list[Message]:
    """Ask which files to open before any ticket is written.

    The pass that makes a vague report workable. A report naming a function is
    already located; "it sometimes drops inputs" is not, and a planner given
    only a file tree picks by filename. So the first call spends nothing but a
    little context deciding what to read, and the ticket is then written
    against the real contents of those files.
    """
    return [
        Message(role="system", content=LOCATE_SYSTEM),
        Message(
            role="user",
            content=f"""## The report
{report.strip()}

## What the harness found
{evidence.strip() or "(nothing — this may not be a git checkout)"}

Name the files to read.""",
        ),
    ]


def parse_locate(text: str, known: Sequence[str], limit: int = 6) -> list[str]:
    """Candidate paths from a locate reply, filtered to files that exist.

    A path the model invented is dropped rather than passed on: reading it
    would fail silently, and the ticket would then be written as though the
    file had been read and found irrelevant.
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
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict):
        return []

    tracked = {str(path).replace("\\", "/"): str(path) for path in known}
    chosen: list[str] = []
    for raw in data.get("candidates", []) or []:
        path = str(raw).strip().replace("\\", "/").lstrip("./")
        if path in tracked and tracked[path] not in chosen:
            chosen.append(tracked[path])
    return chosen[:limit]


REDIAGNOSE_SYSTEM = """You are re-diagnosing a bug whose first explanation was wrong.

A ticket was written from a report, naming code that was supposed to be at
fault. A test was then written to demonstrate that fault, and it did not: the
test passed against the code as it stands, or it could not be written at all.
That is not a dead end. It is a measurement, and it rules something out.

The report itself is not in question. Somebody saw the behavior they described.
What has been disproved is where the previous ticket said it comes from.

So: propose a different cause.

- Do not re-propose what has already been ruled out. Naming the same files
  again with the same reasoning wastes the only budget this ticket has.
- Reason from what would actually produce the reported symptom. If the value
  the report mentions is correct everywhere it is computed, then what is wrong
  is where it is displayed, transported, cached, or re-initialised — follow it
  outward from the code that was cleared.
- A symptom seen in a running program whose logic checks out is usually in the
  layer between the logic and the eye: the bindings, the entry point, the
  template, the build output, the wiring that never ran.
- Say `unclear` when you have nothing better than another guess, and say what
  would settle it. Parking with an honest question beats a third wrong ticket,
  and it beats a ticket nobody can write a failing test for.

Reply with a single JSON object and nothing else, exactly as the first ticket
was written:

{"title": "...", "spec": "...", "allowed_files": ["..."],
 "reference_files": ["..."], "criteria": [],
 "reproduce": "what a failing test should assert, in one sentence"}
"""


def rediagnose_prompt(
    ticket: Ticket,
    report: str,
    *,
    disproof: str,
    ruled_out: Sequence[tuple[str, str]] = (),
    evidence: str = "",
    sources: dict[str, str] | None = None,
) -> list[Message]:
    """Ask for a new cause after a reproduction failed to reproduce anything.

    The step that keeps a wrong first guess from ending the ticket. The tester
    reporting "this code already does what the report asks for" is a fact about
    the code, and the right use of it is to look somewhere else — not to park
    and tell the reporter their report was vague when it was not.

    `ruled_out` carries every hypothesis already disproved, so the planner
    cannot spend the next one re-proposing the last.
    """
    body = f"""## The report, unchanged
{report.strip()}

## The explanation that was just disproved
{ticket.spec.strip()}

Scoped to: {', '.join(ticket.allowed_files) or "(nothing named)"}

## How it was disproved
{disproof.strip()}
"""

    if ruled_out:
        body += "\n## Already ruled out — do not propose these again\n"
        for spec, why in ruled_out:
            body += f"\n- **{spec.strip().splitlines()[0][:200]}**\n  {why.strip()[:400]}\n"

    if sources:
        body += f"""
## The code that was cleared
This is what the last hypothesis blamed. It has been read and it does not
produce the reported behavior — use it to work out what does.

{_sources_block(sources)}
"""

    if evidence.strip():
        body += f"""
## The repository
Every path you name must come from here.

{evidence.strip()}
"""

    body += "\nWrite the next ticket, or say `unclear`."
    return [
        Message(role="system", content=REDIAGNOSE_SYSTEM),
        Message(role="user", content=body),
    ]


def parse_bug(text: str) -> dict[str, Any]:
    """Parse a bug-planner reply into the fields of one ticket.

    Shares `tickets_from_json`'s tolerance of a fenced block or prose around
    the object, and its refusal to guess: a reply with no spec has not written
    a ticket, whatever else it contains.
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

    unclear = str(data.get("unclear", "") or "").strip()
    if unclear:
        raise ValueError(f"the planner could not place this report: {unclear}")

    spec = str(data.get("spec", "") or "").strip()
    if not spec:
        raise ValueError("planner reply carried no spec")

    return {
        "title": str(data.get("title", "") or "").strip(),
        "spec": spec,
        "allowed_files": [str(p).strip() for p in data.get("allowed_files", []) if str(p).strip()],
        "reference_files": [
            str(p).strip() for p in data.get("reference_files", []) if str(p).strip()
        ],
        "criteria": [str(c).strip() for c in data.get("criteria", []) if str(c).strip()],
        "reproduce": str(data.get("reproduce", "") or "").strip(),
    }


def repro_prompt(
    ticket: Ticket,
    *,
    test_path: str,
    test_command: str = "",
    example_test: tuple[str, str] | None = None,
    sources: dict[str, str] | None = None,
    reproduce: str = "",
    own_file_errors: list[str] | None = None,
    passed_instead: str = "",
    superseded: str = "",
) -> list[Message]:
    """Ask the tester for the test that must fail before anything is fixed.

    Separate from `tests_prompt` because the instruction is inverted. That one
    encodes criteria and treats a failing assertion as the ticket's failure;
    this one is asked for a failure on purpose, and a test that passes is the
    result that stops the ticket.
    """
    body = f"""Bug: {ticket.ticket_id} — {ticket.title}

## The defect, as the ticket states it
{ticket.spec}
"""
    if reproduce:
        body += f"""
## What the test should assert
{reproduce}
"""

    body += f"""
## Write exactly one file, at exactly this path
```
{test_path}
```
This is the only path you may write, and it is the deliverable: it outlives the
fix, and it is what stops this bug coming back. Anything you write anywhere
else is discarded before it reaches disk.
"""

    if test_command:
        body += f"""
## The command that will run it
```
{test_command}
```
Your test must be collected and executed by that command. A file it does not
collect reports nothing, which is indistinguishable from a bug that is not
there.
"""

    if sources:
        body += f"""
## The code as it is today — the code that has the bug
Assert against these names, signatures and types exactly. A test that fails to
compile is not a reproduction: it proves nothing about the behavior, and it
takes the rest of the suite down with it.

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

    if passed_instead:
        body += f"""
## Your last test passed, so it proved nothing

```
{passed_instead}
```

A test that passes against code that has the reported bug is either asserting
something the bug does not affect, or asserting the buggy behavior itself.
Re-read the report and assert the behavior it says is missing — the specific
value, the specific count, the specific ordering. If the report does not give
you one, reply `BLOCKED:` and say what you would need.
"""

    if own_file_errors:
        quoted = "\n".join(f"  {line}" for line in own_file_errors)
        body += f"""
## These errors are in the file you are about to write

```
{quoted}
```

They are defects in the test, not evidence about the bug — `{test_path}` is
yours and nobody else can fix it. Either it did not build, or it ran and died
before it reached an assertion: it tried to start a process, open a path, or
reach a service, and did not get one. Neither reproduces anything.

Fix exactly what they point at and keep the assertion. If what failed was
something outside the test process, do not retry it differently — assert on the
same behavior by a means the running process can observe directly.
"""

    if superseded:
        body += f"""
## An earlier reproduction was retired, and you are replacing it

{superseded}

Read that failure before you write anything. It never reached an assertion, or
it reached one that no permitted edit could satisfy — either way it measured
something other than the reported bug, and the fix has been blocked on it
rather than on the code.

Two things usually cause it. The test depended on something outside the process
it runs in — a subprocess, a build tool, a path relative to a working directory
it does not control, a file another task produces. Or it asserted against a
literal the ticket's own spec contradicts, so the two demands could never both
hold.

Assert on the same behavior, by different means. Prefer what the running
process can observe directly over anything it has to launch, and check every
literal you assert against the spec above. If the report genuinely cannot be
reproduced without the thing that failed, reply `BLOCKED:` and say so — that is
a real answer, and it is better than a second reproduction nobody can pass.
"""

    body += "\nWrite the test now."
    return [
        Message(role="system", content=REPRO_SYSTEM),
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
    reproduced: tuple[str, str] | None = None,
    unchecked: str = "",
) -> list[Message]:
    messages = [Message(role="system", content=REVIEWER_SYSTEM)]

    # The reviewer is asked to check that nothing contradicts established
    # conventions, which it cannot do without being told what they are.
    context = _context_message(ticket, retrieved)
    if context is not None:
        messages.append(context)

    # The reviewer signed off on this contract before anything was built, and
    # is shown what it agreed to. A reviewer that was overruled reads the
    # reason here instead of raising the objection again on the diff — which is
    # the failure the sign-off pass exists to move earlier, not to duplicate.
    settled = ratification_message(ticket)
    if settled is not None:
        messages.append(settled)

    verdicts = _prior_verdicts_message(prior_verdicts)
    if verdicts is not None:
        messages.append(verdicts)

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

    if reproduced is not None:
        # A bug ticket carries evidence no feature ticket has: the fault was
        # demonstrated before it was fixed. Without this the reviewer judges
        # the diff against a spec describing a defect it cannot see any trace
        # of, and "the tests pass" is the least informative thing about it.
        path, output = reproduced
        body += f"""
## This bug was reproduced before it was fixed
`{path}` was written first and failed against the code as it stood:

```
{output.strip()}
```

That test passes now. Judge the diff on whether it fixes the *cause* of that
failure: a change that satisfies this one test while leaving the same fault
reachable another way is the thing to reject.

The test file appears in the diff below. It was written before any fix was
attempted, by a different role, and the author of this fix could not edit it —
so read it as evidence rather than as the fix's own work, and say so if it
asserts something weaker than the report describes.
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

    if unchecked:
        # The one case where "the tests pass" means nothing, and the reviewer
        # has no way to know it. A tester that kept reshaping the value before
        # comparing it had its file discarded twice, so nothing ran against
        # these criteria at all — and the reviewer that saw the *previous*
        # version of this ticket read `expect(u32(pcg.randi())).toBe(...)` as
        # evidence the criterion was met and approved an implementation
        # returning -223148877 where the criterion said 4071818419.
        body += f"""
## No test was written for these criteria

{unchecked}

So nothing in the suite checks the criteria above; whatever else went green
went green without them. You are the only thing standing between this ticket
and being recorded as done.

Check each criterion against the diff directly, and check it against what the
code **returns**, not what a caller could convert it to. A criterion naming an
exact value is not met by a function that produces the right bits in the wrong
representation — the wrong sign, the wrong width, the wrong units. If you
cannot tell from the diff what a criterion's expression evaluates to, that is a
REJECT and not a benefit of the doubt.
"""

    messages.append(Message(role="user", content=body))
    return messages


def _prior_verdicts_message(prior_verdicts: Sequence[str]) -> Message | None:
    """The reviewer's own earlier rejections, in a message the gate may drop.

    Kept out of the body deliberately. This block is the only one that grows
    with every attempt, and while it was part of the ticket message a ticket
    that accumulated enough rejection text overflowed the window and came back
    `blocked` — a hard stop, for the crime of having been reviewed too often.
    Trimming costs the reviewer some memory; blocking costs the ticket.
    """
    if not prior_verdicts:
        return None
    earlier = "\n\n".join(
        f"### Attempt {index}\n{verdict.strip()}"
        for index, verdict in enumerate(prior_verdicts, start=1)
    )
    return Message(
        role="user",
        content=f"""{PRIOR_VERDICTS_HEADING}
{earlier}

Read these before deciding. If the objection you raised has been addressed,
that is progress — do not replace it with a fresh objection you never raised
before, which ends the ticket in three rounds over three unrelated points. If
the same defect is still there, say so plainly and in the same terms: a
rejection that repeats is evidence the spec is wrong rather than the code, and
saying it in those words is what gets that noticed.
""",
    )


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


SCOPE_ARGUMENT_SYSTEM = """You are the reviewer, ruling on one question about a \
ticket's scope.

A bug ticket's fix works — its reproduction passes — and the suite still fails,
because an older test asserts the behavior the report calls a bug. The two
assertions are opposites and both cannot hold. You are being asked whether the
older assertion should be retired, which would let the ticket write the test
file it is currently forbidden to touch.

This is the most dangerous grant in the pipeline. The whole reason a bug is
reproduced before it is fixed is that a model which writes both the code and
the assertion it is judged by will encode its bugs as passing tests. Handing a
ticket authority over the test that contradicts it is that failure in one step —
if the reproduction asserts the wrong thing, you are approving the deletion of
the test that would have caught it.

So do not rule on which assertion is more recent, or which is more convenient,
or on the fact that the ticket is stuck. Rule on which one is *right*, and say
why in terms of the report and the two assertions in front of you.

Answer with exactly one of these on the first line, then your argument:

GRANT: the older assertion is stale and should be retired
REFUSE: the older assertion is correct, or you cannot tell from what is here

Your argument must do these things, or the grant is discarded and read as a
refusal:

- Name the test file and quote what it asserts.
- Say what the report claims the behavior should be instead.
- Say which of the two is right and how you know — from the report, from the
  spec, from the code you were shown. "The ticket cannot pass otherwise" is not
  a reason; that is true of every contradiction and settles nothing.

REFUSE is a complete answer and often the correct one. A contradiction you
cannot settle from the evidence is one a person should settle.
"""


def scope_argument_prompt(
    ticket: Ticket,
    report: str,
    *,
    test_path: str,
    test_source: str,
    blamed: Sequence[str] = (),
    repro_path: str = "",
    repro_source: str = "",
) -> list[Message]:
    """Ask the reviewer to argue for or against retiring a stale assertion.

    Deliberately a separate call from the ordinary review, and deliberately the
    reviewer's rather than the planner's. Respec proposes the widening because
    it is the role that rewrites tickets; it is also the role that wants the
    ticket to pass, and a scope grant justified by the party that benefits is
    not a check. The reviewer gains nothing from the ticket going green.
    """
    body = f"""Ticket: {ticket.ticket_id} — {ticket.title}

## The report this ticket came from
{(report or ticket.original_spec or ticket.spec).strip()[:3000]}

## What the ticket says the behavior should be
{ticket.spec.strip()}

## The assertion that contradicts it
`{test_path}`, which this ticket may not write. The suite fails here:

{chr(10).join(f"  {line}" for line in blamed) or "  (no location reported)"}

```
{test_source.strip()[:8000]}
```
"""

    if repro_source.strip():
        body += f"""
## The reproduction, which passes
`{repro_path}` was written from the report before any fix was attempted, and it
passes against the fix as it stands. It is the ticket's contract.

```
{repro_source.strip()[:6000]}
```
"""

    body += f"""
## The question
Should `{test_path}` be retired so this ticket can write it?

Answer GRANT or REFUSE on the first line, then argue it.
"""

    return [
        Message(role="system", content=SCOPE_ARGUMENT_SYSTEM),
        Message(role="user", content=body),
    ]


# How much argument a grant has to carry. Not a quality measure — nothing here
# can measure that — but enough to separate a reviewer that reasoned from one
# that answered `GRANT: yes, it must be changed`, which is the failure this
# whole gate exists to catch.
ARGUMENT_FLOOR = 160


def parse_scope_argument(text: str, test_path: str) -> tuple[bool, str, str]:
    """Read the reviewer's ruling as `(granted, argument, why not)`.

    Fail-closed, like `parse_verdict`, and for a sharper reason: an unreadable
    reply here would hand a ticket write access to the assertion judging it.

    A grant must also be *argued*. The reply has to name the file it is talking
    about and say enough to be checkable by a person reading the run log later.
    A bare `GRANT:` is recorded as a refusal with its own explanation, because
    "the reviewer asserted this" and "the reviewer argued this" are different
    facts and only one of them is worth acting on.
    """
    verdict = ""
    for raw in text.splitlines():
        line = raw.strip().strip("*#`_ \t.:—-").upper()
        if not line:
            continue
        has_grant, has_refuse = "GRANT" in line, "REFUSE" in line
        # Both on one line is the instruction echoed back, not a ruling.
        if has_grant and has_refuse:
            continue
        verdict = "grant" if has_grant else "refuse" if has_refuse else ""
        if verdict:
            break

    argument = text.strip()
    if verdict != "grant":
        return False, argument, "" if verdict else "no GRANT or REFUSE line in the reply"

    name = test_path.rsplit("/", 1)[-1].lower()
    if name and name not in argument.lower():
        return False, argument, f"the argument never mentions {test_path}"
    if len(argument) < ARGUMENT_FLOOR:
        return False, argument, (
            f"the grant was asserted in {len(argument)} characters rather than "
            f"argued; retiring an assertion needs a reason a person can check"
        )
    return True, argument, ""


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


def _learned_block(ticket: Ticket) -> str:
    """A ticket's accumulated learnings, for a prompt that judges them."""
    return "\n".join(
        f"- {entry['text']}"
        + (f"  (established {entry['count']} separate times)" if entry.get("count", 1) > 1 else "")
        for entry in (ticket.learned or [])
        if entry.get("text")
    )


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

    established = _learned_block(ticket)
    if established:
        # The raw material this step never had. These are facts about the
        # repository that the ticket's own attempts worked out — which is
        # exactly the shape of thing worth outliving the run, and until now it
        # was thrown away with the ticket.
        body += f"""
## What this ticket's attempts established about the project
Each of these was written down because an attempt had to work it out. A count
above one means the loop had to work it out more than once, which is the
strongest signal here that it belongs in memory rather than in a ticket.

{established}
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


def convention_prompt(ticket: Ticket, retrieved: str = "") -> list[Message]:
    """Ask what a *failed* ticket established about the project, if anything.

    The sibling of `record_prompt`, against the case it refuses to touch. The
    rule that step enforces — never record a conclusion drawn from unverified
    work — is right, and the reason it is right does not reach a toolchain
    fact: `noUncheckedIndexedAccess` is set or it is not, and the compiler said
    so. That the ticket which discovered it went on to fail says nothing about
    whether the flag is set.

    Worth having because the tickets that learn most are the ones that fail
    most. On the run this comes from, the two tickets that spent 650 attempts
    between them ended blocked, and everything their failures had demonstrated
    about the project went into the artifact directory and nowhere else.

    So the system prompt is narrower than the recorder's on every axis except
    that one: no decisions, no approaches, no corrections, nothing about the
    implementation at all — only a constraint of the project that a ticket in
    another part of the repository would need to know.
    """
    messages = [Message(role="system", content=CONVENTION_RECORDER_SYSTEM)]

    context = _context_message(ticket, retrieved)
    if context is not None:
        messages.append(context)

    messages.append(
        Message(
            role="user",
            content=f"""Ticket: {ticket.ticket_id} — {ticket.title}
This ticket did not pass. Its code is being taken back out of the tree.

## What its attempts established about the project
{_learned_block(ticket) or "(nothing was recorded)"}

Does any of that generalize past this ticket, to a constraint a future ticket
anywhere in this repository would need to know? If the project context above
already covers it, or if it is only about the files this ticket owned, answer
{NOTHING_SENTINEL}.""",
        )
    )
    return messages


# What a stuck review can conclude. `unclear` is listed because it is the
# honest answer often enough to need somewhere to go: a verdict parser that
# only understands the two confident answers turns "I cannot tell" into
# whichever one it resembles.
STUCK_WINNABLE = "winnable"
STUCK_UNWINNABLE = "unwinnable"
STUCK_UNCLEAR = "unclear"

_STUCK_VERDICT = re.compile(
    r"^\s*VERDICT\s*:\s*(winnable|unwinnable|unclear)\b", re.IGNORECASE | re.MULTILINE
)


def stuck_review_prompt(
    ticket: Ticket,
    diff: str,
    classes: Sequence[dict],
    failure: str,
    retrieved: str = "",
) -> list[Message]:
    """Ask the reviewer whether a stuck ticket can be met at all.

    Run against a red tree, which every other review refuses to do, and the
    refusal is why this exists. Review sits behind verification, so a ticket
    stuck on the same failure for cycles never reaches the only role positioned
    to say the contract is wrong. On the run this comes from, 1,350 executor
    calls produced 17 reviews, and the ticket that spent 6.7M tokens on an
    unsatisfiable contract gave the reviewer 43k of them.

    Advisory. It cannot pass a red tree and it does not change the ticket's
    status; what it produces is an opinion for the planner to work from and a
    line in the log for a person.
    """
    messages = [Message(role="system", content=STUCK_REVIEWER_SYSTEM)]

    context = _context_message(ticket, retrieved)
    if context is not None:
        messages.append(context)

    settled = ratification_message(ticket)
    if settled is not None:
        messages.append(settled)

    repeated = "\n".join(
        f"- {entry['name']} — {entry['count']} times" for entry in classes
    ) or "- (none recorded)"

    body = f"""Ticket: {ticket.ticket_id} — {ticket.title}

## Spec
{ticket.spec}

## Acceptance criteria
{_criteria_block(ticket)}

## Scope this ticket may write
{_files_block(ticket)}

## What it keeps failing on
The same kinds of failure, cycle after cycle. Counts are across every attempt.

{repeated}

## The newest failure in full
{distill(failure, limit=3000) or "(nothing recorded)"}

## The code as it stands
```diff
{diff or "(empty diff)"}
```

Read the criteria against the spec, and both against what keeps failing. Can
this ticket be satisfied as written?"""

    messages.append(Message(role="user", content=body))
    return messages


def parse_stuck_review(text: str) -> tuple[str, str]:
    """Split a stuck review into `(verdict, reasoning)`.

    An unparseable reply is `unclear` rather than an error. The caller acts on
    a confident verdict and this one is advisory, so a reply nobody can read
    should leave the ticket exactly where it was.
    """
    stripped = (text or "").strip()
    if not stripped:
        return STUCK_UNCLEAR, ""
    found = _STUCK_VERDICT.search(stripped)
    if not found:
        return STUCK_UNCLEAR, stripped[:2000]
    verdict = found.group(1).lower()
    reasoning = stripped[found.end() :].strip()
    return verdict, (reasoning or stripped)[:2000]


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
- A spec may state a decision as well as a requirement — "randomness is a
  xorshift32 seeded from JavaScript", under a heading saying it is settled.
  Copy every such sentence back into the revised spec, in its own words. They
  are not criteria, so nothing downstream checks them: drop one and the ticket
  goes green against a choice nobody made. If the failures show a decision is
  the problem, say so in the rationale — that is a human's call, not yours.
- `context` is appended to the plan's, never written over it. It carries rules
  the executor needs on every attempt, and they are not yours to retire.
- If the failures show the work simply was not finished — no recurring theme,
  no ambiguity, nothing the spec could have prevented — say so by returning
  the ticket essentially unchanged with a rationale explaining why.

- `learned_add` is where a fact about *this repository* goes: something the
  failures have demonstrated about how the project is built, checked or wired,
  which the next attempt would otherwise have to work out again. "The type
  checker runs with `noUncheckedIndexedAccess`, so every index needs a guard."
  "Imports in this package resolve with a `.js` extension." One short sentence
  each, stated as fact.
  It is not a requirement and nothing enforces it: the reviewer is not shown
  it and no criterion is made from it. That is the point — write down what is
  true here, not a new bar for the executor to clear. If the thing you want to
  say is a demand, it belongs in `criteria` or nowhere.
  Entries accumulate across cycles and are never removed, so a fact you state
  twice is counted rather than duplicated. Say nothing rather than restating
  the spec.

Reply with JSON and nothing else:

{
  "rationale": "one or two sentences on what the ticket got wrong",
  "spec": "the revised spec",
  "criteria": ["revised acceptance criteria"],
  "allowed_files": ["revised scope"],
  "reference_files": ["files the executor must be shown to get this right"],
  "context": "what the next attempt should already know",
  "learned_add": ["facts about this repository the next attempt should not have to rediscover"]
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
    ruled_out: Sequence[tuple[str, str]] = (),
    report: str = "",
    contradiction: dict[str, list[str]] | None = None,
    reproduction: Sequence[str] = (),
    stuck: dict | None = None,
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

    if ruled_out:
        # A re-diagnosed bug ticket has no "original intent" worth returning
        # to: its first spec was a hypothesis, and the loop disproved it by
        # running a test. Showing the drift block below instead told a planner
        # that the disproved cause was the human's intent and the reproduction
        # was drift — so it reverted a bug that had just been reproduced in
        # `web/main.js` back to a Rust file holding four `pub mod` lines.
        dead = "\n\n".join(
            f"### Ruled out {index}: no longer a candidate\n{spec.strip()}\n\n"
            f"**Disproved by:** {why.strip()[:600]}"
            for index, (spec, why) in enumerate(ruled_out, start=1)
        )
        body += f"""
## The report this ticket came from
This is the fixed point, not the spec above. The spec is the current *theory*
of what causes it; the report is what a person actually saw, and no revision
rewrites it.

{(report or ticket.original_spec or ticket.spec).strip()[:4000]}

## Explanations already tested and disproved
Each of these was the ticket's spec at some point. Each was tested by writing a
reproduction against it, and each failed to reproduce anything — which is a
fact about that cause, not about the report.

{dead}

**Do not propose any of these again**, and do not narrow the scope back to the
files they named. A reproduction that would not fail against a cause is the
strongest evidence available that the cause is not where the bug is. If the
current spec is also wrong, propose something *new*; if you have nothing new,
say so in `rationale` and leave the ticket as written.
"""
    elif ticket.drifted:
        # The anchor. Each revision is derived from the last, so without the
        # ingested text in front of it the planner cannot tell its own
        # accumulated drift from what a human actually asked for — and it will
        # keep revising away from the plan, one plausible step at a time.
        # Which fixed point this is depends on whether the ticket was
        # ratified. Saying "human-authored" over text four roles negotiated
        # would be telling the planner something untrue about who it is
        # overruling.
        settled = (
            "This is what every role signed off on before any code was "
            "written, and where it and the current text disagree, the "
            "ratified version is the intent."
            if ticket.ratified_spec
            else "This is the human-authored original, and where the two "
            "disagree, the original is the intent."
        )
        body += f"""
## What this ticket said before any attempt was made
{settled} The "current" text above is what earlier revisions have made of it;
treat any difference you cannot justify from the failures below as drift you
should undo rather than build on.

### Original spec
{ticket.contract_spec or ticket.original_spec}

### Original acceptance criteria
{_criteria_block(ticket, ticket.contract_criteria)}
"""
        if ticket.original_context.strip():
            body += f"""
### Original context
This paragraph is the plan's, and it is kept whatever you return: anything you
write in `context` is appended to it, not put in its place.

{ticket.original_context.strip()}
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
        if reproduction:
            listed = ", ".join(f"`{path}`" for path in reproduction)
            body += f"""
One of those files is this ticket's reproduction: {listed}. It is the test the
ticket has been failing, and it is pasted here so you can read what it actually
demands rather than infer it from a runner's summary. Check every literal your
spec states — a filename, a path, a count, an order — against what that file
asserts. A spec that instructs one thing while the reproduction asserts another
cannot be satisfied by any edit, and six consecutive revisions guessed at a
filename that was written three lines into a test nobody was shown.

Do not put it in `allowed_files`. It is the standard this ticket is measured
against, and it will be removed from any scope you propose. If you conclude the
reproduction itself is what is wrong, say that in `rationale` and leave the spec
alone — the loop retires a reproduction by having the tester write a new one,
never by letting the executor edit it.
"""

    if contradiction:
        blocking = "\n\n".join(
            f"### `{path}`\n" + "\n".join(f"  {line}" for line in lines)
            for path, lines in contradiction.items()
        )
        body += f"""
## Why this ticket cannot pass as scoped
The fix works — the reproduction written from the report passes against it. What
fails is an assertion in a file this ticket may not write:

{blocking}

That assertion states the behavior the report calls a bug, so it and the
reproduction are direct opposites and no edit inside the current scope satisfies
both. An earlier ticket wrote that test alongside its own implementation, which
is how a defect ends up encoded as a passing assertion.

You have two honest answers:

- The assertion is stale. Propose adding its file to `allowed_files` so it can
  be retired along with the fix. **You are proposing this, not deciding it** —
  the reviewer is asked separately to argue whether the assertion is genuinely
  wrong, and the scope is granted only if it does. Say in `rationale` why you
  think the report is right and the assertion is not.
- The assertion is correct and the report is wrong. Reply with `impossible`
  saying so. That parks the ticket for a person and costs nothing further, and
  it is the right answer whenever the report contradicts something the project
  deliberately decided.

Do not rewrite the spec to dodge the contradiction, and do not weaken the
reproduction. Both leave the disagreement in place and hide it.
"""

    body += f"""
## What happened, oldest attempt first
{evidence}
"""

    if ticket.abandoned_values:
        # The planner sees the current spec and the failures, never the fact
        # that it has already rewritten this same constant twice. One ticket's
        # seeding increment went `(seed << 1) | 1` -> `3n` ->
        # `29739081755268826799n` -> `1442695040888963407n` across four cycles,
        # each revision confidently correcting the last one's invention, and
        # the rule below was in this prompt the whole time.
        listed = "\n".join(f"- `{value}`" for value in ticket.abandoned_values[:12])
        body += f"""
## Constants this ticket's spec has already stated and dropped
{listed}

Each was written into the spec by a revision of this ticket, and each was taken
back out by a later one. They were tried; the attempts failed; the number was
changed again.

If the value you are about to write is another guess at the same quantity, that
is the pattern above continuing, and the next cycle will read exactly like this
one. A spec whose stated algorithm cannot produce the value a criterion demands
is not a wording problem — say so instead.
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
- Anything else you list is added. A criterion that restates a sentence of the
  spec is kept: the reviewer is given the spec and enforces it either way, so
  writing it down raises no bar, it only makes an existing demand checkable.
  Quote the spec's own wording when you do that — the closer the two are, the
  more reliably it is recognised as a restatement rather than a new demand.
  A criterion the spec does not state is refused. Never add one to describe a
  bug the attempts happened to produce.

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

    notes = [n for n in (ticket.human_note or []) if n.get("text")]
    if notes:
        # A fifth evidence block into a shape already built to carry four, and
        # the only one whose author can see the repository. It goes before the
        # stuck block deliberately: if a person has already said why the ticket
        # is stuck, that should be read before the loop's own account of it.
        written = "\n".join(f"- {n['text'].strip()}" for n in notes[-8:])
        body += f"""
## What a person said about this ticket
Written by a human after seeing where it got stuck, and the only evidence here
whose author could read the repository. Where it disagrees with the failures
below, they are what the loop observed and this is what somebody made of it.

{written}

A person may state an acceptance criterion and you may not — that rule is about
provenance, not position, and they are not the party being judged. If a note
asks for one, it is theirs to add with `forge criteria --add`; putting it in
your revision is still you raising the bar.
"""

    if stuck:
        # The question inverted. `impossible` has been available on every
        # respec call since the field existed, and in 86 consecutive cycles on
        # one ticket the planner never reached for it once — because it was
        # asked, every time, to revise the ticket so the next attempt could
        # succeed, and that question has an answer whether or not one exists.
        # Asking the other question is the whole feature. See
        # docs/CONVERGENCE.md.
        cycles = stuck.get("flat_cycles", 0)
        repeated = "\n".join(f"- {name}" for name in stuck.get("classes", ())) or "- (none)"
        body += f"""
## This ticket has stopped moving
Its last {cycles} cycles failed on exactly these, and on nothing else:

{repeated}

The line numbers and the values differ between them; the mistakes do not. That
is not a hard problem being worked on — it is the same ticket producing the
same result from a fresh attempt budget, {cycles} times over.
"""
        opinion = (stuck.get("review") or "").strip()
        if opinion:
            body += f"""
### What the reviewer said when it was shown this
It was asked one question — can this ticket be satisfied as written — against
the criteria and the failures together. Its answer is **{stuck.get('verdict', 'unclear')}**:

{opinion[:1500]}

It is an opinion, not a finding. Weigh it; you have the text in front of you
and it did too.
"""
        if stuck.get("executor_claim"):
            body += f"""
### What the executor said
It replied `IMPOSSIBLE:` rather than only implementing:

{str(stuck['executor_claim'])[:1000]}

An executor that cannot pass a ticket has every reason to conclude nobody can,
so this is a claim to check against the criteria above, not a finding either.
"""
        body += """
Answer one of two things, and do not split the difference.

- **The ticket cannot be satisfied as written.** Reply with `impossible`,
  naming the criterion and the contradiction in plain terms a person can check.
  That parks it for someone to settle and costs nothing further. It is the
  right answer here more often than anywhere else in this loop, because a
  ticket that has produced identical results from repeated fresh attempts has
  already demonstrated that the variable is not the sampling.

- **It can, and the attempts have been going about it wrongly.** Then say what
  they keep doing and what the revision makes them do instead, and revise for
  that specifically. A rewrite that restates the same requirement in different
  words spends another cycle proving it again — if you cannot name the thing
  that will now happen differently, the honest answer is the one above.

Do not return the ticket essentially unchanged. Unchanged is the one reply that
guarantees another identical cycle."""
    else:
        body += "\nRevise the ticket so the next attempt can succeed."

    return [
        Message(role="system", content=RESPEC_SYSTEM),
        Message(role="user", content=body),
    ]


def _json_object(text: str, *, who: str = "model") -> dict[str, Any]:
    """The JSON object in a reply, however the model wrapped it.

    Fenced, bare, or with prose either side of it — all three arrive, and a
    parser that accepts only the first spends attempts on presentation. Shared
    by every prompt in this module that asks for JSON, so a model that learns
    to satisfy one satisfies the rest.
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
        raise ValueError(
            f"{who} did not return usable JSON: {(text or '')[:400]}"
        ) from exc
    if not isinstance(data, dict):
        raise ValueError(f"{who} reply was not a JSON object")
    return data


def parse_respec(text: str) -> dict[str, Any]:
    """Parse a respec reply into the ticket fields it changes.

    Returns only the keys the planner actually supplied, so a reply that omits
    a field leaves the existing value alone rather than blanking it — a
    dropped `allowed_files` would silently narrow scope to nothing.
    """
    data = _json_object(text, who="planner")

    revision: dict[str, Any] = {}
    for key in ("spec", "context", "rationale", "impossible"):
        if isinstance(data.get(key), str) and data[key].strip():
            revision[key] = data[key].strip()
    for key in ("criteria", "allowed_files", "reference_files", "learned_add"):
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


# ----------------------------------------------------------------------
# Ratification — the sign-off pass before a ticket is built
# ----------------------------------------------------------------------

RATIFY_SYSTEM = """You are the {role} in a plan-and-execute pipeline, and a \
ticket has been put to you before any code exists.

Nothing has been built yet. Nothing will be until this ticket is agreed. You
are not reviewing work — you are being asked one question about the contract
you will have to work under:

**{question}**

Answer it honestly and narrowly. This is the only moment where changing the
ticket is free. After this, the same objection costs a rejected diff, a wasted
attempt, or a criterion nobody can check.

Two things are worth separating, and the format keeps them apart:

- **Blocking** — you cannot do your part as written. A scope missing a file you
  must edit, a criterion that cannot be turned into an assertion, a spec that
  contradicts itself. Say what would have to change for it to stop blocking.
- **Suggestion** — you can do your part, and the ticket would still be better
  with this. Sign off anyway.

Do not object to a ticket for being small, for lacking detail you do not need,
or for stylistic reasons. Do not restate the ticket back. Do not propose work
the ticket does not ask for — a ticket that does less than you would like is
not a defect, and scope you add here is scope somebody has to verify.

Do not object that the work has not been done yet. There is no implementation,
no diff and no test run to look at, and there is not meant to be — that is the
premise of this pass, not something missing from it. "I cannot verify this
because the code is not here" is true of every ticket that reaches you, and it
answers a question nobody asked.

Reply in exactly this format and nothing else:

SIGNOFF: yes
BLOCKING:
- (one line each, or NONE)
SUGGEST:
- (one line each, or NONE)

`SIGNOFF: yes` with a blocking objection listed is read as no. You have named
something you cannot work under, and the loop takes your reason over your vote.
"""

# What each role is actually being asked. The question is the whole difference
# between four sign-offs and four opinions: a role asked "is this ticket good?"
# answers about somebody else's job, and a planner reading four such answers
# cannot tell which of them it has to act on.
RATIFY_QUESTIONS = {
    "planner": (
        "Is this still one testable unit of the work the plan asked for, with "
        "the right dependencies, and does its scope match what it describes?"
    ),
    "executor": (
        "Could you produce this implementation from the spec, the scope, and "
        "the reference files listed — without opening a file you have not been "
        "given, and without writing outside the allowed scope?"
    ),
    "tester": (
        "Could you turn every acceptance criterion into a test that fails "
        "before the change and passes after it? Name any criterion you could "
        "not express as an assertion."
    ),
    # Written in the future tense on purpose. "Rule on this from a diff" reads
    # to a smaller model as though a diff had been supplied and withheld, and
    # it answers by listing what the missing implementation prevents it from
    # checking — three such objections parked one ticket that had never been
    # attempted, and the suggestions attached to them restated the ticket's own
    # spec back as instructions.
    "reviewer": (
        "Once this ticket has been built, could you rule on the diff from "
        "these criteria alone? Name any criterion you could not settle by "
        "reading a change — one needing the code run, a value nobody has "
        "measured, or a judgement the criteria do not pin down."
    ),
}


def _ratify_notes_block(notes: Sequence[dict], limit: int = 12) -> str:
    """The argument so far, oldest first.

    Rendered from the stored records rather than re-summarised, so the role
    that raised a point sees its own words and the answer it was given. A role
    shown a paraphrase of its own objection raises the objection again.
    """
    lines = []
    for note in list(notes)[-limit:]:
        verdict = "signed off" if note.get("signed") else "did not sign off"
        lines.append(f"Pass {note.get('pass', '?')} — {note.get('role', '?')}: {verdict}")
        for point in note.get("blocking") or []:
            lines.append(f"  blocking: {point}")
        for point in note.get("suggestions") or []:
            lines.append(f"  suggested: {point}")
        if note.get("response"):
            lines.append(f"  planner: {note['response']}")
    return "\n".join(lines)


def ratify_prompt(
    ticket: Ticket,
    role: str,
    *,
    sources: dict[str, str] | None = None,
    retrieved: str = "",
    notes: Sequence[dict] = (),
    learnings: str = "",
) -> list[Message]:
    """Ask one role to sign off on a ticket, before anything is built."""
    question = RATIFY_QUESTIONS.get(
        role, "Could you do your part of this ticket as written?"
    )
    messages = [
        Message(role="system", content=RATIFY_SYSTEM.format(role=role, question=question))
    ]

    context = _context_message(ticket, retrieved)
    if context is not None:
        messages.append(context)

    # Earlier tickets in this run, so the second ticket does not re-open what
    # the first settled. Behind the context heading, which makes it droppable:
    # worth having, never worth the ticket.
    if learnings.strip():
        messages.append(
            Message(
                role="user",
                content=f"{CONTEXT_HEADING}\n"
                f"### Already settled on earlier tickets in this run\n"
                f"{learnings.strip()}",
            )
        )

    body = f"""Ticket: {ticket.ticket_id} — {ticket.title}

## Spec
{ticket.spec}

## Allowed scope (the only files this ticket may write)
{_files_block(ticket)}

## Reference files (readable, not writable)
{_reference_block(ticket)}

## Acceptance criteria
{_criteria_block(ticket)}
"""

    if sources:
        body += f"""
## The files as they exist now
{_sources_block(sources)}
"""

    if notes:
        body += f"""
{RATIFICATION_HEADING}
{_ratify_notes_block(notes)}

The ticket above already reflects what the planner changed. Do not re-raise a
point that has been answered unless the answer is wrong, and say why if it is.
"""

    messages.append(
        Message(role="user", content=body + "\nAnswer now, in the format given.")
    )
    return messages


_SIGNOFF_LINE = re.compile(
    r"^\s*signoff\s*[:\-]\s*\**\s*(yes|no|y|n|accept|reject|agree|object)",
    re.IGNORECASE,
)
_RATIFY_SECTION = re.compile(
    r"^\s*\**\s*(blocking|suggest(?:ion|ions|ed)?)\s*\**\s*[:\-]\s*(.*)$", re.IGNORECASE
)
_RATIFY_NONE = re.compile(r"^\(?\s*(none|n/?a|nothing)\b", re.IGNORECASE)
_BARE_VOTE = ("yes", "accept", "agree", "ok", "signoff: yes", "signoff yes")


def _ratify_points(raw: Sequence[str]) -> list[str]:
    """Clean one section's lines: strip bullets, drop the NONE placeholder."""
    points = []
    for line in raw:
        text = line.strip().lstrip("-*• \t").strip()
        if not text or _RATIFY_NONE.match(text):
            continue
        points.append(text)
    return points


def parse_ratify(text: str) -> tuple[bool, list[str], list[str]]:
    """Read one role's sign-off. Returns `(signed, blocking, suggestions)`.

    Fail-closed, for the reason `parse_verdict` is: a reply nobody can read is
    not agreement. The cost of that is bounded — an unreadable answer costs a
    pass, and the resolution rule does not require every role to sign off —
    while the other direction builds a ticket on a contract a role never
    accepted.

    A `yes` alongside a blocking objection is read as no. The role has named
    something it cannot work under, and taking the vote over the reason is how
    a sign-off pass becomes a formality that changes nothing.
    """
    signed = False
    saw_vote = False
    section = ""
    blocking: list[str] = []
    suggestions: list[str] = []

    for line in (text or "").splitlines():
        vote = _SIGNOFF_LINE.match(line)
        if vote:
            saw_vote = True
            signed = vote.group(1).lower() in ("yes", "y", "accept", "agree")
            section = ""
            continue
        heading = _RATIFY_SECTION.match(line)
        if heading:
            section = "blocking" if heading.group(1).lower() == "blocking" else "suggest"
            trailing = heading.group(2).strip()
            if trailing:
                (blocking if section == "blocking" else suggestions).append(trailing)
            continue
        if section == "blocking":
            blocking.append(line)
        elif section == "suggest":
            suggestions.append(line)

    blocking = _ratify_points(blocking)
    suggestions = _ratify_points(suggestions)

    if not saw_vote:
        # Absorbed rather than refused where the answer is plainly one word: a
        # small model that writes "ACCEPT" and nothing else has voted, and
        # failing it over the missing label spends a pass on formatting. The
        # same absorption is not extended to a refusal buried in prose — that
        # is already a no, and reading it as one costs nothing.
        if (text or "").strip().lower().rstrip(".!") in _BARE_VOTE:
            return True, [], []
        return False, ["reply could not be read as a sign-off"], suggestions

    return (signed and not blocking), blocking, suggestions


RATIFY_REVISE_SYSTEM = """You are the planner. Every role has been asked \
whether it can do its part of one ticket, and at least one said it could not. \
Nothing has been built yet.

Rewrite the ticket so the blocking objections stop being true, and answer each
one. You decide what the ticket says — the other roles propose, you dispose —
but an objection you decline is one you have to give a reason for, and the role
that raised it will read that reason.

You may change the spec, the context, the allowed scope, the reference files,
and — unlike a revision after a failure — the acceptance criteria. This is the
moment the contract is settled: a criterion that cannot be tested should be
made testable now, and a missing one should be added now.

What you must not do:

- Do not weaken a criterion to make an objection go away. A criterion nobody
  checks is worse than one somebody objected to.
- Do not widen the scope past the files the work actually needs.
- Do not turn this into a different ticket. The plan asked for something; a
  revision that does more than was asked is a new ticket, not this one.
- Do not drop something the plan stated because a role found it inconvenient.

Reply with a JSON object and nothing else:

```json
{
  "spec": "the revised spec",
  "criteria": ["every criterion, in full, including the unchanged ones"],
  "allowed_files": ["path/one"],
  "reference_files": ["path/two"],
  "context": "anything the roles must carry, or omit this key",
  "responses": ["one line per objection: what you changed, or why you did not"]
}
```

Every key is optional, except that the reply has to change something. Omit a
key to leave that field exactly as it is. `criteria` and the file lists are
replacements rather than additions, so send them in full or not at all.
"""


def ratify_revision_prompt(
    ticket: Ticket,
    notes: Sequence[dict],
    *,
    sources: dict[str, str] | None = None,
    learnings: str = "",
) -> list[Message]:
    """Ask the planner to rewrite a ticket the roles could not all sign off."""
    messages = [Message(role="system", content=RATIFY_REVISE_SYSTEM)]

    if learnings.strip():
        messages.append(
            Message(
                role="user",
                content=f"{CONTEXT_HEADING}\n"
                f"### Already settled on earlier tickets in this run\n"
                f"{learnings.strip()}",
            )
        )

    body = f"""Ticket: {ticket.ticket_id} — {ticket.title}

## Spec
{ticket.spec}

## Allowed scope
{_files_block(ticket)}

## Reference files
{_reference_block(ticket)}

## Acceptance criteria
{_criteria_block(ticket)}

## What the plan originally asked for
{ticket.original_spec or ticket.spec}

{RATIFICATION_HEADING}
{_ratify_notes_block(notes)}
"""

    if sources:
        body += f"""
## The files as they exist now
{_sources_block(sources)}
"""

    messages.append(
        Message(role="user", content=body + "\nReturn the revised ticket as JSON now.")
    )
    return messages


def parse_ratify_revision(text: str) -> dict[str, Any]:
    """Parse a ratify revision. Respec's grammar, one rule relaxed.

    Respec demands a revised spec, because a revision with nothing in it is a
    planner that gave up on a ticket it was asked to rescue. Here a revision
    that only widens a scope, or only rewords one criterion, is a complete
    answer: the objection was narrow and so is the fix. What is still refused
    is a reply that changes nothing at all, which spends a pass and leaves the
    next vote reading the identical ticket.
    """
    data = _json_object(text, who="planner")

    revision: dict[str, Any] = {}
    for key in ("spec", "context", "rationale"):
        if isinstance(data.get(key), str) and data[key].strip():
            revision[key] = data[key].strip()
    for key in ("criteria", "allowed_files", "reference_files", "responses"):
        value = data.get(key)
        if isinstance(value, list):
            items = [str(v).strip() for v in value if str(v).strip()]
            # An empty list is a truncated reply far more often than it is a
            # deliberate "this ticket needs no criteria" — the same reading
            # `parse_respec` takes, and for the same reason.
            if items:
                revision[key] = items

    if not set(revision) - {"rationale", "responses"}:
        raise ValueError("planner reply revised nothing")
    return revision


def ratification_message(ticket: Ticket) -> Message | None:
    """What the roles agreed, for the prompts that act on the ticket afterwards.

    Carried into build, tests and review so the role that asked for something
    can see whether it got it, and the role that was overruled reads the reason
    here rather than raising the same objection again on the diff.
    """
    if not ticket.ratify_notes:
        return None
    status = ticket.ratify_status or "unsettled"
    return Message(
        role="user",
        content=f"""{RATIFICATION_HEADING}
Every role was asked whether it could do its part of this ticket before any
code existed. The ticket above is the result, agreed {status}.

{_ratify_notes_block(ticket.ratify_notes)}

This is settled. Do not re-open it — work to the ticket as it now stands.
""",
    )
