"""Rewriting a ticket from the evidence of why it failed.

A retry that re-runs the spec which already failed three times is just a slower
failure. Respec reads the recorded step failures — the reviewer's rejections,
the compiler's complaints, the `BLOCKED:` the executor gave up with — and asks
the planner for a ticket the next attempt can actually satisfy.

Two callers need exactly this, and differ only in how they report it: `forge
retry --respec`, which prints to a terminal, and the loop's automatic retry
cycles, which log to the run. So the revision itself lives here and neither
owns it. The model call is passed in rather than built here for the same
reason: the loop's call has to go through the budget gate and the control
channel, and the CLI's must not wait on either.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from .evidence import MAX_LOCATE, locate_named, repo_files
from .ingest import derive_needs, plan_decisions, whole_file_claims
from .patch import is_safe_path, normalize_path
from .prompts import parse_respec, respec_prompt
from .providers import Completion, Message, ProviderError
from .state import Store, Ticket, _criterion_key

# `(messages, max_tokens) -> Completion`. Temperature is the caller's business.
Caller = Callable[[list[Message], int], Completion]


@dataclass
class Revision:
    """What respec did to one ticket. Exactly one of `changed`/`note` is set."""

    ticket_id: str
    changed: list[str] = field(default_factory=list)
    rationale: str = ""
    # Why the ticket was left as written, when it was.
    note: str = ""
    # Plan-authored criteria the planner tried to drop or reword, put back.
    # Surfaced rather than restored in silence: a planner reaching for the same
    # criterion every cycle is the signal that a human should look at whether
    # it is satisfiable at all.
    refused_criteria: list[str] = field(default_factory=list)
    # Criteria the planner tried to add. Refused while the criteria are
    # locked: respec runs on a ticket that has just failed, and its job is
    # to make that ticket satisfiable rather than harder.
    minted_criteria: list[str] = field(default_factory=list)
    # Criteria the planner added that only restate a sentence of the spec. Kept
    # rather than refused: the reviewer is given the spec and enforces it
    # already, so writing one down makes an existing demand checkable rather
    # than raising the bar.
    admitted_criteria: list[str] = field(default_factory=list)
    # Decisions the plan marked as settled that a revised spec dropped. The
    # spec revision was refused whole; these say which sentence cost it.
    refused_decisions: list[str] = field(default_factory=list)
    # Whether the plan's context paragraph had to be put back in front of a
    # revision that replaced it.
    restored_context: bool = False
    # The planner's report that no revision can make this ticket satisfiable.
    # A complete answer, not a failure to answer — the ticket parks for a human
    # rather than spending another full attempt budget.
    impossible: str = ""
    # Scope the revision asked for and did not get here: test files holding an
    # assertion that contradicts a bug ticket's reproduction. Proposing it is
    # respec's to do; granting it is not, because respec is the role that wants
    # the ticket to pass. The caller takes these to the reviewer.
    pending_scope: list[str] = field(default_factory=list)

    @property
    def revised(self) -> bool:
        return bool(self.changed)


# Per-file ceiling, matching the executor's own reference limit. The planner
# needs enough of a file to check a claim against it, not enough to reproduce
# it — and a lockfile must not crowd out the failures it is reasoning from.
SOURCE_LIMIT = 24_000


def sources_for(root: Path, ticket: Ticket) -> dict[str, str]:
    """The code this ticket writes and reads, as it exists on disk.

    Used where there is no Orchestrator to ask — `forge retry --respec` runs
    in the CLI process. Never raises: a file that cannot be read is one the
    planner does without, which is worse than showing it and better than
    failing a retry over it.
    """
    found: dict[str, str] = {}
    for path in list(ticket.allowed_files) + list(ticket.reference_files):
        # A glob in allowed_files is a scope rule, not a readable file.
        if any(character in path for character in "*?["):
            continue
        if path in found or not is_safe_path(root, path):
            continue
        candidate = (root / path).resolve()
        try:
            if not candidate.is_file():
                continue
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if len(text) > SOURCE_LIMIT:
            text = text[:SOURCE_LIMIT] + f"\n[truncated at {SOURCE_LIMIT} characters]\n"
        found[path] = text
    return found


def _ground_references(
    root: Path, proposed: Sequence[str]
) -> tuple[list[str], list[tuple[str, str]], list[str]]:
    """Point a revised read scope at files that are actually there.

    Returns `(kept, remapped, invented)` — the paths to use, the ones moved to
    where the file really is, and the ones nothing in the repository answers to.

    `reference_files` is the one field whose whole purpose is to be read off
    disk. A path that does not resolve is not a smaller version of the right
    answer, it is silence: `sources_for` skips what it cannot open, and the
    executor is handed a prompt that mentions the file and shows none of it.
    Nothing downstream notices, because a reference the executor never sees
    looks exactly like a reference it read and ignored.

    That is how a Java run died. Respec reported "added minimal stubs for these
    classes to reference_files" and wrote three paths one package short of the
    real ones. The executor, shown nothing, imported the package the paths
    implied, and the next five attempts were spent being told by javac that the
    symbol does not exist — with the same wrong paths handed back every cycle,
    because nothing checked them.

    `allowed_files` gets no such treatment and must not: a ticket's writable
    scope is where its work is *going*, and most of those files do not exist
    until it runs.
    """
    kept: list[str] = []
    remapped: list[tuple[str, str]] = []
    invented: list[str] = []
    # Listed once. `locate_named` would otherwise walk the repository per path,
    # and a revision naming six references would list it six times.
    pool = repo_files(root, limit=MAX_LOCATE)
    for path in proposed:
        candidate = normalize_path(str(path).strip())
        if not candidate:
            continue
        if not is_safe_path(root, candidate):
            invented.append(candidate)
            continue
        if (root / candidate).is_file():
            kept.append(candidate)
            continue
        found = locate_named(root, candidate, files=pool)
        if found:
            remapped.append((candidate, found))
            kept.append(found)
            continue
        invented.append(candidate)
    return list(dict.fromkeys(kept)), remapped, invented


# The provenance note the respec prompt puts beside a criterion, in either the
# emphasised or bare spelling, wherever a planner has echoed it back.
_PROVENANCE_NOTE = re.compile(
    r"_?\((?:from the plan|added by an earlier revision)[^)]*\)_?",
    re.IGNORECASE,
)


def _key(criterion: str) -> str:
    """A criterion reduced to what it asserts, for comparing two spellings.

    Backticks, punctuation and case are presentation; a planner that changes
    only those has reworded the same demand, not raised a new one.

    The provenance note comes off first. The prompt asks the planner to return
    plan-authored criteria verbatim, and a planner that does so may carry the
    note with them — which is not a rewording of anything, but survives
    normalisation as `fromtheplanyoumaynotchangethis` and makes the copy match
    nothing. One run reported nine criteria dropped and eleven invented on a
    reply that had changed neither: the nine, echoed with their notes, counted
    once as missing and once as new. Instruction-following is not an access
    control, and it is not a comparison key either.
    """
    return _criterion_key(_PROVENANCE_NOTE.sub("", criterion))


# Ways of describing the executor's reply format. That format is the harness's
# contract — stated in the executor's own system prompt and parsed on the way
# back — so a ticket has no business restating it, and every restatement is a
# chance to contradict it.
_PROTOCOL_PHRASES = (
    "code fence",
    "fenced code",
    "fenced block",
    "code block",
    "backtick",
    "in your response",
    "in your reply",
    "your output",
    "path on its own line",
    "prefixed by the filename",
    "raw contents",
    "markdown prose",
)


def _protocol_language(text: str) -> set[str]:
    lowered = (text or "").lower()
    return {phrase for phrase in _PROTOCOL_PHRASES if phrase in lowered}


def _refuse_protocol_edits(
    ticket: Ticket, revision: dict, ruled_out: Sequence[tuple[str, str]] = ()
) -> list[tuple[str, str]]:
    """Drop a revised `spec` or `context` that has taken up formatting rules.

    Respec sees a ticket that failed and reaches for the nearest cause. When the
    failure was the executor's output not parsing, the nearest cause looks like
    the output format — so it writes formatting instructions into the spec, and
    those instructions are guesses about a contract it was never shown. One
    revision told the executor to emit "raw contents ... not wrapped in markdown
    code fences", which is the one thing that guarantees the parser finds
    nothing at all: a fence is what it matches on. The ticket became impossible
    by construction, and the criteria guard did not cover it because none of it
    was a criterion.

    Judged by what the revision *introduces*, so a ticket whose plan legitimately
    talks about fences — a markdown tool, a docs generator — can still be
    revised. Returns the fields dropped, with the phrase that cost them.
    """
    anchors = {
        "spec": _anchor(ticket, ruled_out),
        "context": ticket.context if ruled_out else (ticket.original_context or ticket.context),
    }
    dropped: list[tuple[str, str]] = []
    for field_name, anchor in anchors.items():
        if field_name not in revision:
            continue
        introduced = _protocol_language(revision[field_name]) - _protocol_language(anchor)
        if introduced:
            revision.pop(field_name)
            dropped.append((field_name, sorted(introduced)[0]))
    return dropped


# Ways of telling a role that a failing check does not count. Respec sees a
# ticket that failed on errors it did not cause, and the cheapest revision
# available is not to fix anything — it is to write down that the failure was
# already there and may be passed over. That sentence is a claim about the
# state of the tree, made by a planner that cannot see the tree, and it
# outlives the cycle that made it.
#
# Matched as a verb reaching for a failure rather than on the words alone: a
# ticket may legitimately say "ignore case", "ignore hidden files", or "skip
# the header row", and none of those excuse anything.
_WAIVERS = (
    (
        "excusing an error",
        re.compile(
            r"\bignor\w*\b[^.\n]{0,80}\b(?:error|failure|diagnostic)s?\b", re.IGNORECASE
        ),
    ),
    (
        "excusing an error",
        re.compile(r"\b(?:error|failure)s?\b[^.\n]{0,80}\bignor\w*", re.IGNORECASE),
    ),
    (
        "calling a failure pre-existing",
        re.compile(
            r"\bpre-?existing\b[^.\n]{0,60}\b(?:error|failure|breakage)s?\b",
            re.IGNORECASE,
        ),
    ),
    (
        "leaving a failure unfixed",
        re.compile(
            r"\b(?:do not|don't|no need to|need not)\s+"
            r"(?:try to\s+|attempt to\s+)?(?:fix|repair|address|resolve)\b"
            r"[^.\n]{0,60}\b(?:error|failure)s?\b",
            re.IGNORECASE,
        ),
    ),
    (
        "waiving a verify step",
        re.compile(
            r"\b(?:skip|bypass|disable|suppress|waive)\w*\b[^.\n]{0,40}"
            r"\b(?:verification|verify|typecheck|type check|compilation|"
            r"the tests?|test suite|the build)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "declaring a failure out of scope",
        re.compile(
            r"\bnot (?:your|this ticket'?s?|the ticket'?s?)\s+"
            r"(?:concern|problem|responsibility|job|fault)\b",
            re.IGNORECASE,
        ),
    ),
)


def _waiver_language(text: str) -> set[str]:
    return {label for label, pattern in _WAIVERS if pattern.search(text or "")}


def _refuse_verification_waivers(
    ticket: Ticket, revision: dict, ruled_out: Sequence[tuple[str, str]] = ()
) -> list[tuple[str, str]]:
    """Drop a revised `spec` or `context` that excuses a failing check.

    The sibling of `_refuse_protocol_edits`, against a costlier revision. One
    run's respec wrote into a ticket's context: "MainPanel.java ... currently
    has a pre-existing compilation error regarding com.plexnamer.model. Do not
    modify it ... Ignore this pre-existing compilation error during
    verification." Every clause of that was wrong. The package had never
    existed, the error was the ticket's own scope failing to compile, and the
    sentence told five further attempts and two retry cycles to look past the
    one diagnostic that said what was broken.

    What makes it worse than an ordinary bad revision is that it is durable. A
    spec can be re-revised from the next cycle's failures; a context sentence
    saying the failures do not count teaches every later role to discard the
    evidence a revision would be made from, so the ticket stops accumulating
    any.

    Amnesty for pre-existing breakage is a real thing and the harness already
    grants it — from a baseline it measured itself, per signature, recorded in
    the step log. It is not a planner's to assert in prose.

    Judged by what the revision introduces, so a plan that legitimately talks
    about tolerating errors can still be revised. Returns the fields dropped,
    with the kind of waiver that cost them.
    """
    anchors = {
        "spec": _anchor(ticket, ruled_out),
        "context": ticket.context if ruled_out else (ticket.original_context or ticket.context),
    }
    dropped: list[tuple[str, str]] = []
    for field_name, anchor in anchors.items():
        if field_name not in revision:
            continue
        introduced = _waiver_language(revision[field_name]) - _waiver_language(anchor)
        if introduced:
            revision.pop(field_name)
            dropped.append((field_name, sorted(introduced)[0]))
    return dropped


def _disarmed_context(ticket: Ticket, ruled_out: Sequence[tuple[str, str]] = ()) -> str:
    """The context this ticket should carry, once a waiver is stripped from it.

    Returns the ticket's own context unless it has been talked out of caring
    about a failing check, in which case it returns the plan's paragraph — the
    empty string included, which is a legitimate restoration for a ticket whose
    plan gave it no context and whose whole context is a planner's invention.

    The guard above stops a waiver being written. This clears one already
    written, and both are needed: a ticket carries its context across every
    cycle, so a sentence that landed before the guard existed — or in a run
    since resumed — would otherwise go on instructing the executor forever.
    The plan's paragraph is the fixed point, exactly as it is for
    `_preserve_plan_context`.

    Only the context, never the spec. A spec legitimately evolves away from the
    plan's wording and is re-derived from the failures every cycle; the context
    is the field respec is invited to write freely, which is why it is also the
    field where a waiver survives unexamined.
    """
    if ruled_out:
        return ticket.context
    plan = ticket.original_context or ""
    if _waiver_language(ticket.context) - _waiver_language(plan):
        return plan
    return ticket.context


def _anchor(ticket: Ticket, ruled_out: Sequence[tuple[str, str]] = ()) -> str:
    """The spec a revision is judged against.

    Normally the ingested original. Every revision is derived from the last, so
    without a human's text as the fixed point the loop revises away from the
    plan one plausible step at a time, and each step looks reasonable next to
    the one before it.

    Not so once a bug ticket has been re-diagnosed. There `original_spec` holds
    the *first hypothesis* — a cause the loop has since disproved by running a
    test against it — and anchoring on it drags the ticket back to the
    explanation it just ruled out. One run did exactly that. It re-diagnosed
    from the Rust to `web/main.js`, reproduced the bug there, and the next
    respec reverted the scope to `src/lib.rs` on the reasoning that "the
    previous revision drifted into build/JS paths, but the original intent and
    all failures point to a Rust initialization". The executor then blocked,
    because the code it had been told to change was outside its scope.

    A bug ticket's fixed point was never the first hypothesis anyway — it is
    the report, which no revision rewrites. Where one exists, the current spec
    is the live hypothesis and drift from a dead one is the point.

    A ticket that has been through a sign-off pass has a nearer fixed point
    than the plan: `ratified_spec`, which four roles agreed to before anything
    was built. It is preferred where it exists, because the ingested text is
    then a draft that has already been superseded on the record.
    """
    if ruled_out:
        return ticket.spec
    return ticket.contract_spec or ticket.spec


# How much of a ruled-out spec a revision must restate before it counts as
# proposing that cause again. High, because the alternative failure — refusing
# a genuinely new hypothesis that happens to share vocabulary with a dead one —
# costs the ticket its last chance at being diagnosed.
_REVIVAL_COVERAGE = 0.85


def _revives_ruled_out(proposed: str, ruled_out: Sequence[tuple[str, str]]) -> str:
    """The disproved spec this revision is re-proposing, or "".

    The prompt says not to, and the prompt is not an access control. A planner
    handed a ticket that has failed a dozen attempts reaches for the reading it
    finds most natural, which is the one the report's own words suggest — and
    that is precisely the hypothesis the loop tested first and disproved.
    """
    wanted = set(_content_words(proposed))
    if len(wanted) < _ENTAILMENT_FLOOR:
        return ""
    for spec, _why in ruled_out:
        dead = set(_content_words(spec))
        if not dead:
            continue
        covered = sum(1 for word in dead if word in wanted)
        if covered / len(dead) >= _REVIVAL_COVERAGE:
            return spec
    return ""


def _normalise(text: str) -> str:
    """Text reduced to what it says, for asking whether it is still there."""
    return re.sub(r"[^a-z0-9]+", "", text.lower())


# A decision short enough to appear inside an unrelated sentence by accident.
# Below this, containment proves nothing and the guard would fire on noise.
_DECISION_FLOOR = 24


def _dropped_decisions(
    ticket: Ticket, proposed: str, ruled_out: Sequence[tuple[str, str]] = ()
) -> list[str]:
    """Decisions the plan marked as settled that a revised spec no longer states.

    Compared on a normalised form, so punctuation and backticks are free to
    change; the words are not. That is the bar the prompt asks for — copy the
    sentence back — and the one a human can check by reading.
    """
    anchor = _anchor(ticket, ruled_out)
    kept = _normalise(proposed)
    return [
        decision
        for decision in plan_decisions(anchor)
        if len(_normalise(decision)) >= _DECISION_FLOOR
        and _normalise(decision) not in kept
    ]


def _preserve_plan_context(ticket: Ticket, revision: dict) -> bool:
    """Fold a revised `context` into the plan's rather than over it.

    `context` is the one field with no provenance rule: respec returns a whole
    new string and the plan's paragraph is simply gone. It went that way on
    five of six tickets in one run, replaced by a sentence of the planner's
    reasoning — the executor's path-line rule and the do-not-write-tests rule
    with it. The system prompt still carries both, so this was degradation
    rather than deletion, but the redundancy holding a weak local model to
    format was what got deleted.

    Appending keeps both halves and costs a paragraph. The plan's text leads,
    because it is what a human wrote and what the executor should read first.
    Returns whether anything was restored, for reporting.
    """
    anchor = (ticket.original_context or "").strip()
    if not anchor or "context" not in revision:
        return False
    proposed = (revision["context"] or "").strip()
    if _normalise(anchor) in _normalise(proposed):
        return False
    revision["context"] = f"{anchor}\n\n{proposed}".strip()
    return True


# Words that carry no demand of their own. A criterion and the spec sentence it
# restates rarely share their grammar, and matching on it would let two
# unrelated statements agree about "the" and "must".
_FILLER = frozenset(
    """a an and are as at be begins by can contains do does each every for from
    has have in is it its may must not of on or should so start starts than that
    the then this to when which will with""".split()
)

# What a criterion has to have in common with a spec sentence before it counts
# as a restatement of it. Deliberately close to total: a false positive here
# lets the loop raise its own bar, which is the regression the ratchet exists
# to stop. A criterion with fewer content words than the floor is not judged at
# all — at that length, overlap is coincidence.
_ENTAILMENT_COVERAGE = 0.8
_ENTAILMENT_FLOOR = 5


def _content_words(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9_][\w:./\\+#!\-]*", text.lower())
    return [word for word in words if word not in _FILLER]


def _spec_entailed(
    ticket: Ticket, criterion: str, ruled_out: Sequence[tuple[str, str]] = ()
) -> bool:
    """Whether a proposed criterion only restates something the spec states.

    The reviewer is given the spec and told to reject work that contradicts it,
    so the bar it actually applies is spec ∪ criteria — while `_merge_criteria`
    tested novelty against the criteria alone. The planner was therefore
    forbidden from writing down a requirement the reviewer was required to
    enforce. One run spent three cycles on that gap over a single line: the
    planner proposed "build.sh must start with #!/usr/bin/env sh and set -eu"
    and was refused twice, while the reviewer rejected the ticket for exactly
    that requirement twice, and the plan stated it in the spec the whole time.

    A criterion that restates a spec sentence is not a ratchet — the spec is
    enforced either way. Judged against `original_spec` where there is one, so
    the loop cannot rewrite the spec and then mint criteria out of what it just
    wrote.
    """
    wanted = _content_words(criterion)
    if len(wanted) < _ENTAILMENT_FLOOR:
        return False
    anchor = _anchor(ticket, ruled_out)
    for line in anchor.splitlines():
        for sentence in re.split(r"(?<=[.:;])\s+", line):
            stated = set(_content_words(sentence))
            if not stated:
                continue
            covered = sum(1 for word in wanted if word in stated)
            if covered / len(wanted) >= _ENTAILMENT_COVERAGE:
                return True
    return False


def _drop_whole_file_claims(
    store: Store, run_id: int, ticket: Ticket, proposed: list[str]
) -> tuple[list[str], list[tuple[str, str]]]:
    """Remove proposed criteria that pin a file another ticket also writes.

    Ingest refuses these outright, because there a human can restate them for
    free. Mid-run there is nobody to ask, and the rest of the revision may be
    sound — so the offending criterion is dropped rather than the whole
    revision, and the drop is reported.

    A criterion the *plan* stated is never dropped here: `_merge_criteria` has
    already restored those, and overruling a human on the strength of a rule
    respec itself broke would be the wrong way round. Only what this revision
    is adding is filtered.
    """
    plan_stated = {_key(c) for c in (ticket.contract_criteria or ticket.criteria)}
    backlog = [t for t in store.list_tickets(run_id) if t.ticket_id != ticket.ticket_id]
    candidate = Ticket(
        ticket_id=ticket.ticket_id,
        allowed_files=ticket.allowed_files,
        criteria=proposed,
    )
    offending = {
        claim: path
        for _, where, claim, path in whole_file_claims(backlog + [candidate])
        if where == "criterion" and _key(claim) not in plan_stated
    }
    if not offending:
        return proposed, []
    kept = [c for c in proposed if c not in offending]
    return kept, sorted(offending.items())


def _order_shared_scope(
    store: Store, run_id: int, ticket: Ticket
) -> list[tuple[str, str, str]]:
    """Order any pair this revision has left writing the same file.

    Reuses the ingest rule, so a scope respec widened mid-run is arranged the
    same way the plan would have been: position order, and only where the graph
    does not already connect the pair in either direction — which is what keeps
    it from contradicting a declared edge or closing a cycle.

    `ticket` is passed in already carrying the revision, so the caller can
    persist it once. Every *other* ticket the ordering touches is written here.
    """
    backlog = [
        ticket if other.ticket_id == ticket.ticket_id else other
        for other in store.list_tickets(run_id)
    ]
    added = derive_needs(backlog)
    by_id = {other.ticket_id: other for other in backlog}
    for later, _earlier, _path in added:
        if later != ticket.ticket_id:
            store.update_ticket(run_id, by_id[later])
    return added


def _merge_criteria(
    ticket: Ticket, proposed: list[str]
) -> tuple[list[str], list[str], list[str]]:
    """Apply a proposed criteria list under the provenance rule.

    Plan-authored criteria survive whatever the planner does; criteria an
    earlier revision added are the loop's own and it may take them back. Both
    halves matter. A blanket freeze let the loop mint a criterion no
    implementation could satisfy and then kept it forever; no freeze at all let
    the failing party rewrite the standard until it asserted the opposite of
    what a human wrote.

    Returns `(criteria, refused, minted)` — the list to store, the
    plan-authored criteria the planner tried to remove and got put back, and
    the criteria it tried to *add*, which are refused.

    Adding is refused because of when respec runs. It runs on a ticket that
    has just exhausted its attempts, and its whole purpose is to produce one
    the next attempt can satisfy; raising the bar at that moment cannot serve
    that purpose. Left open, the bar only ever rose — one ticket went from the
    plan's nine criteria to sixteen across six cycles, and the criterion
    blocking it at the end was one respec had invented two cycles earlier. The
    party being judged does not get to add to the standard it is judged
    against, which is the same rule the loop already enforces one level down
    when it stops the executor writing its own tests.

    A ticket already inflated unwinds on its next revision: the invented
    criteria are not protected, so they survive only while respec keeps asking
    for them, and as fresh proposals they are now refused.

    Clarification still has somewhere to go. Respec rewrites `spec` and
    `context` freely, and that is where a vague requirement belongs — the
    criteria are the contract, the spec explains it. A revision that needs a
    new clause in the contract is reporting that the plan was wrong, which is
    a human's call.

    What is protected is the plan's criteria *still on the ticket*, not every
    criterion the plan ever stated. Resurrecting one a human has since removed
    would overrule the human in the name of protecting them, and on a ticket
    whose criteria were all reworded early it would restore thirteen near
    duplicates of the fifteen already there.

    With no anchor recorded — a run ingested before originals were kept — every
    criterion counts as the plan's, which errs toward leaving a contract alone.

    Matching is on a normalised form, not the exact string. A planner that
    reworded a criterion rather than dropping it — stripping backticks, fixing
    an article — produces a string that no longer matches the original, so an
    exact comparison restores the plan's wording *and* keeps the rewording as a
    new criterion. One ticket reached twenty-seven criteria that way, from a
    plan stating thirteen: every one of them present twice, in two spellings,
    for the executor to read as two separate demands.
    """
    original = {_key(c) for c in (ticket.contract_criteria or ticket.criteria)}
    known = {_key(criterion) for criterion in ticket.criteria}
    wanted = {_key(criterion) for criterion in proposed}

    protected = [c for c in ticket.criteria if _key(c) in original]
    refused = [c for c in protected if _key(c) not in wanted]
    # Criteria an earlier revision invented. Not protected — the loop may take
    # its own back, and that is how a ticket already inflated returns to the
    # plan's bar — but kept while respec still asks for them.
    retained = [c for c in ticket.criteria if _key(c) not in original and _key(c) in wanted]
    minted = [c for c in proposed if _key(c) not in known]

    # Protected criteria first, in the order the ticket already had them, so
    # restoring one never reshuffles the contract a human reads.
    return protected + retained, refused, minted


def revise(
    store: Store,
    run_id: int,
    ticket: Ticket,
    gave_up_note: str = "",
    *,
    call: Caller,
    budget: int,
    sources: dict[str, str] | None = None,
    criteria_locked: bool = True,
    contradiction: dict[str, list[str]] | None = None,
    protected: Sequence[str] = (),
    root: Path | None = None,
    stuck: dict | None = None,
) -> Revision:
    """Rewrite one ticket in place from its recorded failures.

    Best-effort by design. The requeue that precedes this is already committed,
    so a planner that is unreachable or answers with nonsense costs the caller
    a revision, never the retry itself — the ticket simply goes back on the
    backlog as written.

    `gave_up_note` is the ticket's `blocked_note`, which the requeue clears. For
    a ticket the executor abandoned with `BLOCKED:` it is the only record of
    what it could not decide, and no step was ever logged as failed.

    `protected` names paths that may be shown to the planner but must never end
    up writable, whatever it proposes. A bug ticket's reproduction is one: it
    is now pasted in as a source, because respec is the role that has to judge
    whether the standard itself is wrong, and a role that can read a file will
    sooner or later propose owning it. Reading it is the point; writing it is
    the thing the whole reproduce-first order exists to prevent.

    `root` is the repository, and without it a revised `reference_files` is
    taken on trust — see `_ground_references` for what that cost. Optional only
    because a caller may have no tree to check against; every caller in this
    codebase passes it.
    """
    failures = store.ticket_failures(run_id, ticket.ticket_id)
    # A ticket that never ran has nothing to learn from, and its `blocked_note`
    # says only which dependency was missing. Passed to the planner that reads
    # as evidence — filed under "what happened, oldest attempt first", answered
    # by a schema whose only move is to revise the ticket — and it will rewrite
    # a spec no executor has yet read. Three untried tickets were rewritten
    # twice each that way, acquiring a fabricated xorshift constant and a
    # `lib.rs must contain exactly` clause that contradicted their successors.
    if not failures and not ticket.attempts:
        return Revision(ticket.ticket_id, note="the ticket has not run yet")

    note = (gave_up_note or "").strip()
    if note:
        failures = failures + [{"name": "gave up", "detail": note}]
    if not failures:
        return Revision(ticket.ticket_id, note="nothing recorded to learn from")

    # Causes this ticket has already tested and disproved. Empty for everything
    # but a re-diagnosed bug ticket, and decisive for one: without it respec
    # treats the first hypothesis as the human's intent and reverts to it. See
    # `_anchor`.
    ruled_out = store.ruled_out(run_id, ticket.ticket_id)
    run = store.get_run(run_id)
    report = (run["source"] if run is not None else "") or ""

    try:
        completion = call(
            respec_prompt(
                ticket,
                failures,
                sources=sources,
                criteria_locked=criteria_locked,
                ruled_out=ruled_out,
                report=report,
                contradiction=contradiction or {},
                reproduction=protected,
                # Set only on the escalation path, where the question the
                # planner is asked is inverted. See `respec_prompt`.
                stuck=stuck,
            ),
            budget,
        )
        # A reply cut off mid-JSON parses as malformed, which sends the reader
        # looking at the prompt when the output budget is what ran out. Say
        # which it was.
        if completion.truncated:
            raise ValueError(
                f"planner ran out of output room after {budget:,} tokens; "
                f"raise maxOutputTokens for the planner model"
            )
        revision = parse_respec(completion.text)
    except (ProviderError, ValueError) as exc:
        return Revision(ticket.ticket_id, note=str(exc))

    rationale = revision.pop("rationale", "")

    impossible = revision.pop("impossible", "")
    if impossible:
        # Nothing is applied. A spec revised to satisfy a criterion the planner
        # has just called impossible is a spec bent around a contradiction —
        # which is how an xorshift constant got changed to chase a sequence no
        # xorshift produces.
        store.log(
            run_id,
            f"{ticket.ticket_id}: respec reports the ticket cannot be "
            f"satisfied as written — {impossible}",
            level="error",
            kind="ticket",
            data={"criteria": ticket.criteria},
        )
        return Revision(
            ticket.ticket_id,
            rationale=rationale,
            note="the planner says the criteria cannot be satisfied",
            impossible=impossible,
        )

    # The graph is not the planner's to edit. Respec sees one ticket and the
    # reasons it failed; it cannot see the file conflict on the other side of
    # an edge, so dropping one would let two tickets race for a file that the
    # backlog had already ordered. Edges are added below, from scope, where
    # the whole backlog is in view.
    revision.pop("needs", None)

    # Same reason as the criteria guard below: the prompt forbids it, and the
    # prompt is not an access control.
    # Scope over a contradicting test is proposed here and granted elsewhere.
    # Respec is the role that rewrites a ticket so it can pass, which makes it
    # the wrong role to also decide that the assertion standing in its way is
    # wrong. Held out of the revision and handed back for the reviewer to argue.
    # Refused outright rather than handed to the reviewer to argue over. A
    # contradicting test belongs to some earlier ticket and retiring it is a
    # real decision; this ticket's own reproduction is the contract it is being
    # measured by, and there is no argument that makes the party under
    # measurement the right one to rewrite it. When the reproduction really is
    # what is wrong, `_stale_reproduction` retires it and the *tester* writes
    # the replacement.
    if "allowed_files" in revision and protected:
        guarded = {normalize_path(path) for path in protected}
        surrendered = [
            p for p in revision["allowed_files"] if normalize_path(p) in guarded
        ]
        if surrendered:
            revision["allowed_files"] = [
                p for p in revision["allowed_files"] if normalize_path(p) not in guarded
            ]
            store.log(
                run_id,
                f"{ticket.ticket_id}: respec proposed making "
                f"{', '.join(surrendered)} writable. That is this ticket's own "
                f"reproduction — the test it is being judged by — and it is "
                f"pasted in read-only so the spec can be checked against it, "
                f"not so it can be edited. Dropped from the scope; the rest of "
                f"the revision stands.",
                level="warn",
                kind="ticket",
                data={"ticket": ticket.ticket_id, "refused": surrendered},
            )

    pending_scope: list[str] = []
    if contradiction and "allowed_files" in revision:
        gated = {normalize_path(path) for path in contradiction}
        kept = [p for p in revision["allowed_files"] if normalize_path(p) not in gated]
        pending_scope = [
            p for p in revision["allowed_files"] if normalize_path(p) in gated
        ]
        if pending_scope:
            revision["allowed_files"] = kept

    # A revision that re-proposes a cause the loop already disproved by running
    # a test against it. Refused whole rather than merged, because the scope
    # comes with it — this is the step that sent a reproduced bug back to a
    # file containing four `pub mod` lines.
    revived = _revives_ruled_out(revision.get("spec", ""), ruled_out)
    if revived:
        for field_name in ("spec", "allowed_files", "reference_files", "context"):
            revision.pop(field_name, None)
        store.log(
            run_id,
            f"{ticket.ticket_id}: respec proposed a cause this ticket has "
            f"already disproved, and the scope that goes with it; refused. The "
            f"live hypothesis stands. A reproduction that failed against a "
            f"cause is evidence about that cause, not a reason to return to "
            f"it:\n  ruled out earlier: {revived.splitlines()[0][:200]}",
            level="warn",
            kind="ticket",
            data={"ticket": ticket.ticket_id, "revived": revived[:2000]},
        )

    for field_name, phrase in _refuse_protocol_edits(ticket, revision, ruled_out):
        store.log(
            run_id,
            f"{ticket.ticket_id}: respec rewrote {field_name} to describe the "
            f"executor's reply format ({phrase!r}); dropped. How the executor "
            f"formats its answer is fixed by the harness — a ticket that "
            f"restates it can only contradict it, and one that told the "
            f"executor not to use code fences made itself unparseable.",
            level="warn",
            kind="ticket",
        )

    # A read scope is only worth what the executor can open. Checked against
    # the tree here rather than trusted, because every role downstream treats
    # an unreadable reference as no reference at all and says nothing.
    if root is not None and revision.get("reference_files"):
        revision["reference_files"], remapped, invented = _ground_references(
            root, revision["reference_files"]
        )
        for wrong, right in remapped:
            store.log(
                run_id,
                f"{ticket.ticket_id}: respec asked the executor to read "
                f"{wrong}, which does not exist; the only file by that name in "
                f"this repository is {right}, so that is what it will be shown.",
                level="warn",
                kind="ticket",
                data={"ticket": ticket.ticket_id, "was": wrong, "now": right},
            )
        if invented:
            store.log(
                run_id,
                f"{ticket.ticket_id}: respec named {len(invented)} reference "
                f"file(s) this repository does not contain under any name; "
                f"dropped. A path that cannot be opened reaches the executor as "
                f"silence, not as a smaller hint — it is shown nothing and left "
                f"to guess at the contents:\n"
                + "\n".join(f"  - {path}" for path in invented[:5]),
                level="warn",
                kind="ticket",
                data={"ticket": ticket.ticket_id, "invented": invented},
            )
        # Every path it proposed was invented. Keeping the empty list would
        # strip the read scope the plan gave the ticket on the strength of a
        # revision that named nothing real.
        if not revision["reference_files"]:
            revision.pop("reference_files")

    # The scope the ticket is already carrying, when this revision did not
    # touch it. A phantom path that landed in an earlier cycle is not
    # self-correcting: the next respec sees a ticket whose references look
    # settled, proposes nothing, and the executor is shown the same silence
    # again. Same reasoning as clearing a stale waiver below.
    if root is not None and "reference_files" not in revision and ticket.reference_files:
        grounded, remapped, invented = _ground_references(root, ticket.reference_files)
        if grounded and grounded != list(ticket.reference_files):
            revision["reference_files"] = grounded
            store.log(
                run_id,
                f"{ticket.ticket_id}: this ticket was carrying "
                f"{len(remapped) + len(invented)} reference path(s) that do not "
                f"resolve, left by an earlier revision; corrected where the file "
                f"was findable by name and dropped where it was not. The "
                f"executor was being shown nothing for them.",
                level="warn",
                kind="ticket",
                data={
                    "ticket": ticket.ticket_id,
                    "remapped": remapped,
                    "invented": invented,
                },
            )

    for field_name, waiver in _refuse_verification_waivers(ticket, revision, ruled_out):
        store.log(
            run_id,
            f"{ticket.ticket_id}: respec rewrote {field_name} to excuse a "
            f"failing check ({waiver}); dropped. What pre-dates a ticket is "
            f"measured from a baseline the harness takes itself and recorded "
            f"per error — a ticket that asserts it in prose is telling every "
            f"later attempt to look past the diagnostic that says what broke.",
            level="warn",
            kind="ticket",
            data={"ticket": ticket.ticket_id, "field": field_name, "waiver": waiver},
        )

    # A decision is not a criterion, so the ratchet never covered it. Refused
    # whole: a spec revised around a dropped decision has already reasoned from
    # its absence, and keeping the sentence while keeping the rest of that
    # reasoning would produce a spec that contradicts itself.
    dropped: list[str] = []
    if "spec" in revision:
        dropped = _dropped_decisions(ticket, revision["spec"], ruled_out)
        if dropped:
            revision.pop("spec")
            store.log(
                run_id,
                f"{ticket.ticket_id}: respec dropped {len(dropped)} decision(s) "
                f"the plan marked as settled; the spec revision was refused. A "
                f"decision is not a criterion, so nothing downstream would have "
                f"noticed — the ticket would have gone green against a choice "
                f"nobody made:\n"
                + "\n".join(f"  - {decision}" for decision in dropped[:5]),
                level="warn",
                kind="ticket",
                data={"decisions": dropped},
            )

    restored_context = _preserve_plan_context(ticket, revision)
    if restored_context:
        store.log(
            run_id,
            f"{ticket.ticket_id}: respec replaced the plan's context; the "
            f"plan's paragraph was put back and the revision appended to it. "
            f"Context is where the plan states rules the executor needs every "
            f"attempt — put your reasoning in the rationale instead.",
            level="warn",
            kind="ticket",
        )

    # A waiver that landed in an earlier cycle — before this guard existed, or
    # in a run since resumed. Cleared here rather than only refused above,
    # because the context is the one field that survives every revision: left
    # alone it goes on telling each new attempt that the check it is failing
    # does not count.
    if "context" not in revision:
        disarmed = _disarmed_context(ticket, ruled_out)
        if disarmed != ticket.context:
            revision["context"] = disarmed
            store.log(
                run_id,
                f"{ticket.ticket_id}: this ticket was carrying a context that "
                f"excused a failing check, written by an earlier revision. It "
                f"has been reset to the plan's; the harness decides what "
                f"pre-dates a ticket, from a baseline it measures.",
                level="warn",
                kind="ticket",
                data={"ticket": ticket.ticket_id, "was": ticket.context[:2000]},
            )

    # Facts, not demands, and screened for the difference. `learned` is read by
    # every later attempt and never revised away, so a sentence saying a
    # failing check does not count would teach every role after it to discard
    # the evidence a revision is made from — the durable form of the failure
    # `_refuse_verification_waivers` exists for.
    learned_add = [
        entry
        for entry in revision.pop("learned_add", [])
        if not _waiver_language(entry)
    ]
    if learned_add:
        added = store.learn(run_id, ticket, learned_add)
        if added:
            store.log(
                run_id,
                f"{ticket.ticket_id}: recorded {len(added)} thing(s) this ticket "
                f"established about the repository. They travel with every later "
                f"attempt and are not a bar — nothing downstream enforces them:\n"
                + "\n".join(f"  - {entry}" for entry in added[:5]),
                kind="ticket",
                data={"ticket": ticket.ticket_id, "learned": added},
            )

    # Instruction-following is not an access control, so provenance is enforced
    # here rather than merely described in the prompt.
    refused: list[str] = []
    minted: list[str] = []
    admitted: list[str] = []
    if criteria_locked and "criteria" in revision:
        revision["criteria"], refused, minted = _merge_criteria(
            ticket, revision["criteria"]
        )
        # Admitted after the ratchet has run, not inside it: the ratchet's rule
        # is about who wrote a criterion, and this one is about whether the
        # reviewer is already enforcing it.
        admitted = [c for c in minted if _spec_entailed(ticket, c, ruled_out)]
        if admitted:
            revision["criteria"] = revision["criteria"] + admitted
            minted = [c for c in minted if c not in admitted]

    if "criteria" in revision:
        revision["criteria"], pinned = _drop_whole_file_claims(
            store, run_id, ticket, revision["criteria"]
        )
        for claim, path in pinned:
            store.log(
                run_id,
                f"{ticket.ticket_id}: respec added a criterion pinning all of "
                f"{path}, which another ticket also writes; dropped. "
                f"({claim.strip()[:120]})",
                level="warn",
                kind="ticket",
            )

    changed = [
        field_name
        for field_name, value in revision.items()
        if value != getattr(ticket, field_name)
    ]
    if refused:
        store.log(
            run_id,
            f"{ticket.ticket_id}: respec tried to drop or reword "
            f"{len(refused)} criterion(s) the plan states; they were put back. "
            f"If it keeps reaching for the same one, read it yourself — that is "
            f"usually a criterion nobody can satisfy rather than a planner "
            f"looking for an easier ticket.",
            level="warn",
            kind="ticket",
            data={"restored": refused},
        )
    if admitted:
        # Logged because it is a heuristic deciding that two sentences say the
        # same thing, and a heuristic nobody can audit is one nobody can fix.
        store.log(
            run_id,
            f"{ticket.ticket_id}: respec proposed {len(admitted)} criterion(s) "
            f"that restate the spec; admitted. The reviewer is given the spec "
            f"and enforces it either way, so writing one down raises no bar — "
            f"it only makes the bar checkable:\n"
            + "\n".join(f"  - {criterion}" for criterion in admitted[:5]),
            kind="ticket",
            data={"admitted": admitted},
        )
    if minted:
        # Surfaced rather than dropped in silence, for the same reason a
        # restoration is: respec reaching for the same new criterion every
        # cycle is the plan being underspecified in a nameable way.
        store.log(
            run_id,
            f"{ticket.ticket_id}: respec proposed {len(minted)} criterion(s) the "
            f"plan states nowhere — not in the criteria, and not in the spec, "
            f"which the reviewer would have enforced; refused. A ticket that "
            f"keeps failing does not need a higher bar. If these are things it "
            f"genuinely must do, adopt one with "
            f"`forge criteria {ticket.ticket_id} --accept N` and it becomes the "
            f"plan's:\n"
            + "\n".join(f"  - {criterion}" for criterion in minted[:5]),
            level="warn",
            kind="ticket",
            # The ticket id is in the message too, but a reader that has to
            # parse it back out of prose is a reader that breaks when the prose
            # changes. `forge criteria` reads this.
            data={"minted": minted, "ticket": ticket.ticket_id},
        )
    if not changed:
        return Revision(
            ticket.ticket_id,
            rationale=rationale,
            note="planner kept the ticket as written",
            refused_criteria=refused,
            minted_criteria=minted,
            admitted_criteria=admitted,
            refused_decisions=dropped,
            restored_context=restored_context,
            pending_scope=pending_scope,
        )

    for field_name, value in revision.items():
        setattr(ticket, field_name, value)

    # Widening scope into a file another ticket writes is legal — a ticket is a
    # testable unit, not a file lease — but it is only safe once the two are
    # ordered. Taking the file without the edge leaves them racing for it, and
    # whichever runs second overwrites the first. Respec asks for the file; the
    # backlog decides who goes first.
    for later, earlier, path in _order_shared_scope(store, run_id, ticket):
        store.log(
            run_id,
            f"{later}: now waits for {earlier} — respec took on {path}, "
            f"which {earlier} also writes.",
            level="warn",
            kind="ticket",
        )
        if later == ticket.ticket_id and "needs" not in changed:
            changed.append("needs")

    store.update_ticket(run_id, ticket)
    store.log(
        run_id,
        f"{ticket.ticket_id}: respec revised {', '.join(changed)}. {rationale}".strip(),
        kind="ticket",
    )
    return Revision(
        ticket.ticket_id,
        changed=changed,
        rationale=rationale,
        refused_criteria=refused,
        minted_criteria=minted,
        admitted_criteria=admitted,
        refused_decisions=dropped,
        restored_context=restored_context,
        pending_scope=pending_scope,
    )
