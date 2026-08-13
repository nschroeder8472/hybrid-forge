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
from typing import Callable

from .ingest import derive_needs, whole_file_claims
from .patch import is_safe_path
from .prompts import parse_respec, respec_prompt
from .providers import Completion, Message, ProviderError
from .state import Store, Ticket

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
    # The planner's report that no revision can make this ticket satisfiable.
    # A complete answer, not a failure to answer — the ticket parks for a human
    # rather than spending another full attempt budget.
    impossible: str = ""

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
    return re.sub(r"[^a-z0-9]+", "", _PROVENANCE_NOTE.sub("", criterion).lower())


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
    plan_stated = {_key(c) for c in (ticket.original_criteria or ticket.criteria)}
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
    original = {_key(c) for c in (ticket.original_criteria or ticket.criteria)}
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
) -> Revision:
    """Rewrite one ticket in place from its recorded failures.

    Best-effort by design. The requeue that precedes this is already committed,
    so a planner that is unreachable or answers with nonsense costs the caller
    a revision, never the retry itself — the ticket simply goes back on the
    backlog as written.

    `gave_up_note` is the ticket's `blocked_note`, which the requeue clears. For
    a ticket the executor abandoned with `BLOCKED:` it is the only record of
    what it could not decide, and no step was ever logged as failed.
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

    try:
        completion = call(
            respec_prompt(
                ticket,
                failures,
                sources=sources,
                criteria_locked=criteria_locked,
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

    # Instruction-following is not an access control, so provenance is enforced
    # here rather than merely described in the prompt.
    refused: list[str] = []
    minted: list[str] = []
    if criteria_locked and "criteria" in revision:
        revision["criteria"], refused, minted = _merge_criteria(
            ticket, revision["criteria"]
        )

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
    if minted:
        # Surfaced rather than dropped in silence, for the same reason a
        # restoration is: respec reaching for the same new criterion every
        # cycle is the plan being underspecified in a nameable way.
        store.log(
            run_id,
            f"{ticket.ticket_id}: respec proposed {len(minted)} criterion(s) the "
            f"plan does not state; refused. A ticket that keeps failing does not "
            f"need a higher bar — if these are things it genuinely must do, the "
            f"plan is what needs changing:\n"
            + "\n".join(f"  - {criterion}" for criterion in minted[:5]),
            level="warn",
            kind="ticket",
            data={"minted": minted},
        )

    if not changed:
        return Revision(
            ticket.ticket_id,
            rationale=rationale,
            note="planner kept the ticket as written",
            refused_criteria=refused,
            minted_criteria=minted,
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
    )
