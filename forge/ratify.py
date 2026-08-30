"""The sign-off pass a ticket goes through before anything is built.

A ticket arrives as a contract nobody has agreed to. The roles that have to
work under it — executor, tester, reviewer — first meet it at the moment they
are being measured against it, and every disagreement about the contract
surfaces as something else: a scope the executor cannot work in, discovered on
attempt one; a criterion the tester cannot assert, discovered while writing the
test that has to encode it; a bar the reviewer never accepted, discovered on a
diff that did exactly what it was told.

Ratification asks each role the one question it can answer before any code
exists — *can you do your part of this, as written?* — and gives the planner
the objections to rewrite from. Up to `loop.ratifyPasses` times.

Two rules decide the outcome, and they answer different questions. The planner
has final say over the *text*: it is the only role that writes a revision.
A majority decides whether the ticket *ships*: see `resolve`.

The model call is passed in rather than built here, for the reason respec does
the same — the loop's call goes through the budget gate and the control
channel, and nothing in this module should know that.

See docs/RATIFY.md, including the rule this knowingly bends: a reviewer that
helped write the contract is not independent of it.
"""

from __future__ import annotations

import hashlib

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from .patch import is_safe_path, normalize_path
from .prompts import (
    Message,
    parse_ratify,
    parse_ratify_revision,
    ratify_prompt,
    ratify_revision_prompt,
)
from .providers import Completion, ProviderError
from .respec import (
    _dropped_decisions,
    _drop_whole_file_claims,
    _ground_references,
    _order_shared_scope,
    _preserve_plan_context,
    _refuse_protocol_edits,
    _refuse_verification_waivers,
    dropped_criteria,
)
from .state import Store, Ticket

# `(role, messages, max_tokens) -> Completion`.
Caller = Callable[[str, list[Message], int], Completion]

# How a ticket left the pass. Anything but `blocked` and `unavailable` proceeds
# to build; `unavailable` means no role could be reached and the pass was not
# held against the ticket.
UNANIMOUS = "unanimous"
MAJORITY = "majority"
SPLIT = "split"
BLOCKED = "blocked"
UNAVAILABLE = "unavailable"

PLANNER = "planner"


@dataclass
class Vote:
    """One role's answer, as the pass recorded it."""

    role: str
    signed: bool
    blocking: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    # Set when the model could not be reached at all, which is not the same as
    # a refusal and is not counted as one.
    error: str = ""

    def as_note(self, pass_number: int) -> dict:
        return {
            "pass": pass_number,
            "role": self.role,
            "signed": self.signed,
            "blocking": list(self.blocking),
            "suggestions": list(self.suggestions),
            "response": "",
        }


@dataclass
class Ratification:
    """What a sign-off pass settled about one ticket."""

    ticket_id: str
    status: str = ""
    passes: int = 0
    notes: list[dict] = field(default_factory=list)
    # Ticket fields a planner revision actually changed, across every pass.
    changed: list[str] = field(default_factory=list)
    # Why it could not be settled, when it could not. Becomes the ticket's
    # `blocked_note`, so it has to read as something a human can act on.
    blocked_note: str = ""

    @property
    def proceeds(self) -> bool:
        return self.status not in (BLOCKED,)


def resolve(votes: Sequence[Vote]) -> str:
    """Decide whether a ticket ships on the votes it got.

    | signed off        | outcome     |
    |-------------------|-------------|
    | everyone          | `unanimous` |
    | a majority        | `majority`  |
    | planner + 1 other | `split`     |
    | anything less     | `blocked`   |

    The two rules coexist because they are about different things. A majority
    ships a ticket the planner voted against — the planner's final say is over
    what the ticket *says*, not over whether three roles that can all do their
    part are allowed to start. Below a majority the planner's agreement becomes
    load-bearing again: two of four is not a mandate, and the one thing worse
    than stalling a ticket is building a contract only its author wanted.

    Nobody reachable is not a verdict. A pass where every call failed returns
    `unavailable`, and the caller proceeds unratified rather than parking a
    ticket over an outage.
    """
    if not votes:
        return UNAVAILABLE
    if all(vote.error for vote in votes):
        return UNAVAILABLE

    total = len(votes)
    signed = sum(1 for vote in votes if vote.signed)
    if signed == total:
        return UNANIMOUS
    if signed * 2 > total:
        return MAJORITY
    planner_signed = any(vote.signed for vote in votes if vote.role == PLANNER)
    if planner_signed and signed >= 2:
        return SPLIT
    return BLOCKED


def learnings(store: Store, run_id: int, *, exclude: str = "", limit: int = 6) -> str:
    """What earlier tickets in this run settled, for the next one to read.

    Read out of the database rather than carried in the daemon, so a run
    resumed after a reboot still knows what it agreed to yesterday. Read from
    the notes rather than summarised by a model, because a summary of an
    argument is a second thing that can be wrong about it.

    The point is narrow: the second ticket should not re-open what the first
    settled. A tester that asked for a criterion to be made measurable on
    TT-001 should see, on TT-002, that it asked and what it got.
    """
    lines: list[str] = []
    for ticket in store.list_tickets(run_id):
        if ticket.ticket_id == exclude or not ticket.ratify_notes:
            continue
        settled = [
            note
            for note in ticket.ratify_notes
            if note.get("blocking") or note.get("response")
        ]
        if not settled:
            continue
        lines.append(f"{ticket.ticket_id} ({ticket.ratify_status or 'unsettled'}):")
        for note in settled[-3:]:
            for point in (note.get("blocking") or [])[:2]:
                lines.append(f"  {note.get('role', '?')} objected: {point}")
            if note.get("response"):
                lines.append(f"  planner: {note['response']}")
        if len(lines) > limit * 4:
            break
    return "\n".join(lines)


def _vote(
    store: Store,
    run_id: int,
    ticket: Ticket,
    role: str,
    *,
    call: Caller,
    budget: int,
    sources: dict[str, str] | None,
    retrieved: str,
    notes: Sequence[dict],
    digest: str,
) -> Vote:
    """Ask one role, and never let its answer end the run.

    A provider that is down costs a vote, not a ticket. The distinction is kept
    on the `Vote` itself rather than folded into "did not sign off", because
    parking a ticket for disagreement that never happened is the kind of
    misreport that takes a human hours to see through.
    """
    try:
        completion = call(
            role,
            ratify_prompt(
                ticket,
                role,
                sources=sources,
                retrieved=retrieved,
                notes=notes,
                learnings=digest,
            ),
            budget,
        )
    except ProviderError as exc:
        store.log(
            run_id,
            f"{ticket.ticket_id}: the {role} could not be reached for sign-off "
            f"({exc}). Its vote is not counted either way.",
            level="warn",
            kind="ticket",
            data={"ticket": ticket.ticket_id, "role": role},
        )
        return Vote(role, signed=False, error=str(exc))

    signed, blocking, suggestions = parse_ratify(completion.text)
    if completion.truncated and not signed:
        # A reply cut off mid-list reads as an unreadable refusal, which sends
        # a human to the ticket when the output budget is what ran out.
        blocking = blocking or [
            f"the {role} ran out of output room after {budget:,} tokens"
        ]
    return Vote(role, signed=signed, blocking=blocking, suggestions=suggestions)


# How much longer than the spec it revises a proposed spec may be before it is
# refused as a runaway. Generous: ratification legitimately expands a terse plan
# into something four roles can work from, and the tickets that went on to pass
# in the measured run grew by well under half. The case this stops grew 21x.
_SPEC_RUNAWAY = 4.0


def _prompt_digest(prompt) -> str:
    """A stable fingerprint of a revision prompt, for "have we asked this?".

    Over the messages' text, so a prompt rebuilt from the same ticket and the
    same objections fingerprints the same across cycles and across a restart.
    """
    try:
        body = chr(0).join(f"{m.role}:{m.content}" for m in prompt)
    except (AttributeError, TypeError):
        return ""
    return hashlib.sha256(body.encode("utf-8", "replace")).hexdigest()


def _normalise_criterion(text: str) -> str:
    """A criterion reduced to what makes two of them the same claim.

    Backticks, case and trailing punctuation move on every rewrite and mean
    nothing; this is only ever used to say which criteria in a longer list are
    new, so it errs towards calling a rewording new rather than missing an
    addition.
    """
    return " ".join(str(text).replace("`", "").split()).rstrip(".").lower()


def _apply(
    store: Store,
    run_id: int,
    ticket: Ticket,
    revision: dict,
    *,
    root: Path | None,
    criteria_locked: bool = True,
) -> list[str]:
    """Apply a planner revision to the ticket, under the guards that still hold.

    Ratification is the one place the acceptance criteria may move — there is
    no failure to rationalise yet, no attempt to rescue, and the whole point of
    the pass is to settle a contract before it is expensive to change. Every
    *other* guard respec enforces still applies here, and for the same reasons:
    the prompt forbidding something is not an access control.

    Moving them is not the same as *adding* them, and the difference is the
    ratchet. Rewording a criterion nobody can assert is the work this pass
    exists to do. Inventing a new one is raising the bar the ticket is judged
    against, and the planner has no way to compute a value it makes up: asked
    to settle a ticket carrying four measured hash vectors, one planner added
    four more of its own, got three right by luck and the fourth wrong —
    `postVariant(3130775471, 0, 0, 10) returns 2`, where the hash it had just
    agreed to ends in 7. Nothing downstream could tell that criterion from the
    measured ones. It cost five attempts, parked the ticket, and skipped the
    two that depended on it.

    So a revision that ends with more criteria than it started with is refused
    whole. Refused rather than trimmed, because there is no way to tell which
    of fourteen replaced which of ten, and keeping the ten a human wrote is the
    safe direction. `loop.respecCriteria` unlocks it for an operator who wants
    the additions.
    """
    revision.pop("needs", None)
    revision.pop("rationale", None)
    revision.pop("responses", None)

    proposed = revision.get("criteria")
    if criteria_locked and isinstance(proposed, list) and len(proposed) > len(ticket.criteria):
        known = {_normalise_criterion(text) for text in ticket.criteria}
        added = [text for text in proposed if _normalise_criterion(text) not in known]
        revision.pop("criteria")
        store.log(
            run_id,
            f"{ticket.ticket_id}: ratify grew the acceptance criteria from "
            f"{len(ticket.criteria)} to {len(proposed)}; the revision was "
            f"refused. Rewording a criterion is this pass's job; adding one "
            f"raises the bar the ticket is judged against, and a value the "
            f"planner invented is indistinguishable from a measured one. Set "
            f"loop.respecCriteria to allow it:\n"
            + "\n".join(f"  + {text}" for text in added[:5]),
            level="warn",
            kind="ticket",
            data={"added": added},
        )

    for field_name, phrase in _refuse_protocol_edits(ticket, revision, ()):
        store.log(
            run_id,
            f"{ticket.ticket_id}: ratify rewrote {field_name} to describe the "
            f"executor's reply format ({phrase!r}); dropped. How the executor "
            f"formats its answer is fixed by the harness.",
            level="warn",
            kind="ticket",
        )

    for field_name, waiver in _refuse_verification_waivers(ticket, revision, ()):
        store.log(
            run_id,
            f"{ticket.ticket_id}: ratify rewrote {field_name} to excuse a "
            f"failing check ({waiver}); dropped. What pre-dates a ticket is "
            f"measured from a baseline the harness takes itself.",
            level="warn",
            kind="ticket",
        )

    if "spec" in revision:
        # A revision that balloons is not a revision. Asked to rewrite PF-009
        # from four objections, one planner began copying the reference files
        # into the `spec` field — the reply ended mid-source of
        # `test_move_resolver.gd` — and ran to 83 KB against a 3.8 KB original
        # before the output budget cut it off. A spec many times the length of
        # the one it revises has stopped restating the ticket and started
        # quoting the repository at it, and no ceiling on `maxOutputTokens`
        # makes that the right answer.
        anchor = (ticket.original_spec or ticket.spec or "").strip()
        proposed_spec = (revision.get("spec") or "").strip()
        if anchor and len(proposed_spec) > _SPEC_RUNAWAY * len(anchor):
            revision.pop("spec")
            store.log(
                run_id,
                f"{ticket.ticket_id}: ratify returned a spec "
                f"{len(proposed_spec) / len(anchor):.0f}x the length of the one "
                f"it was revising ({len(proposed_spec):,} chars against "
                f"{len(anchor):,}); the spec revision was refused. A revision "
                f"that long has stopped rewriting the ticket and started "
                f"inlining the files it points at.",
                level="warn",
                kind="ticket",
                data={"proposed": len(proposed_spec), "anchor": len(anchor)},
            )

    if "spec" in revision:
        dropped = _dropped_decisions(ticket, revision["spec"], ())
        if dropped:
            revision.pop("spec")
            store.log(
                run_id,
                f"{ticket.ticket_id}: ratify dropped {len(dropped)} decision(s) "
                f"the plan marked as settled; the spec revision was refused. A "
                f"role finding a decision inconvenient is not the plan being "
                f"wrong:\n" + "\n".join(f"  - {d}" for d in dropped[:5]),
                level="warn",
                kind="ticket",
                data={"decisions": dropped},
            )

    if root is not None and revision.get("reference_files"):
        revision["reference_files"], _remapped, invented = _ground_references(
            root, revision["reference_files"]
        )
        if invented:
            store.log(
                run_id,
                f"{ticket.ticket_id}: ratify named {len(invented)} reference "
                f"file(s) this repository does not contain; dropped. A path "
                f"that cannot be opened reaches a role as silence, not as a "
                f"smaller hint.",
                level="warn",
                kind="ticket",
                data={"invented": invented},
            )
        if not revision["reference_files"]:
            revision.pop("reference_files")

    # A path outside the repository, or one that climbs out of it, is refused
    # here rather than at apply time: a scope agreed by four roles is exactly
    # the kind nobody looks at again. Skipped where there is no tree to resolve
    # against, since the check is about where a path lands on disk.
    for key in ("allowed_files", "reference_files"):
        if key not in revision or root is None:
            continue
        safe = [p for p in revision[key] if is_safe_path(root, normalize_path(p))]
        unsafe = [p for p in revision[key] if p not in safe]
        if unsafe:
            store.log(
                run_id,
                f"{ticket.ticket_id}: ratify proposed {len(unsafe)} path(s) "
                f"outside the repository in {key}; dropped: "
                f"{', '.join(unsafe[:5])}",
                level="warn",
                kind="ticket",
                data={"refused": unsafe},
            )
        if safe:
            revision[key] = safe
        else:
            revision.pop(key)

    if "criteria" in revision:
        # Refused whole, like a spec revision that drops a settled decision.
        # Putting the missing criterion back would land it beside a reworded
        # version of itself, and this pass is allowed to reword.
        gone = dropped_criteria(ticket, revision["criteria"])
        if gone:
            revision.pop("criteria")
            store.log(
                run_id,
                f"{ticket.ticket_id}: ratify dropped {len(gone)} of the plan's "
                f"criteria rather than rewording them; the criteria revision "
                f"was refused. This pass may sharpen a criterion and may add "
                f"one; a criterion it cannot see met is a blocking vote, not a "
                f"shorter list:\n" + "\n".join(f"  - {c.strip()[:160]}" for c in gone[:5]),
                level="warn",
                kind="ticket",
                data={"dropped": gone},
            )

    if "criteria" in revision:
        revision["criteria"], pinned = _drop_whole_file_claims(
            store, run_id, ticket, revision["criteria"]
        )
        for claim, path in pinned:
            store.log(
                run_id,
                f"{ticket.ticket_id}: ratify added a criterion pinning all of "
                f"{path}, which another ticket also writes; dropped. "
                f"({claim.strip()[:120]})",
                level="warn",
                kind="ticket",
            )

    if _preserve_plan_context(ticket, revision):
        store.log(
            run_id,
            f"{ticket.ticket_id}: ratify replaced the plan's context; the "
            f"plan's paragraph was put back and the revision appended to it.",
            level="warn",
            kind="ticket",
        )

    changed = [
        name for name, value in revision.items() if value != getattr(ticket, name, None)
    ]
    for name, value in revision.items():
        setattr(ticket, name, value)

    # Widening scope into a file another ticket writes is legal — a ticket is a
    # testable unit, not a file lease — but only once the two are ordered.
    for later, earlier, path in _order_shared_scope(store, run_id, ticket):
        store.log(
            run_id,
            f"{later}: now waits for {earlier} — ratify took on {path}, which "
            f"{earlier} also writes.",
            level="warn",
            kind="ticket",
        )
        if later == ticket.ticket_id and "needs" not in changed:
            changed.append("needs")

    return changed


def _revise(
    store: Store,
    run_id: int,
    ticket: Ticket,
    notes: list[dict],
    *,
    call: Caller,
    budget: int,
    sources: dict[str, str] | None,
    digest: str,
    root: Path | None,
    criteria_locked: bool = True,
) -> tuple[list[str], list[str]]:
    """One planner revision. Returns `(changed fields, responses)`.

    Best-effort, like respec: a planner that is unreachable or answers with
    nonsense costs a pass, never the ticket. The next pass re-reads the same
    ticket, which is a wasted round but not a wrong one.

    Wasted once. A revision that overran the output budget is remembered by the
    prompt that produced it, and an identical prompt is not sent again — the
    reply was deterministic in the only sense that matters here. Measured on
    PF-009 of a nine-ticket run: two calls, `prompt_tokens` 20,665 both times,
    `completion_tokens` 32,768 both times, `finish_reason: length` both times.
    The second bought nothing and cost ninety seconds of a model that had
    already answered.
    """
    prompt = ratify_revision_prompt(ticket, notes, sources=sources, learnings=digest)
    fingerprint = _prompt_digest(prompt)
    if fingerprint and fingerprint == ticket.ratify_overrun:
        store.log(
            run_id,
            f"{ticket.ticket_id}: skipped the planner revision — this exact "
            f"prompt already ran out of output room, and asking again would "
            f"spend the budget to be told so twice. Raise maxOutputTokens for "
            f"the planner, or narrow the ticket.",
            level="warn",
            kind="ticket",
        )
        return [], []

    try:
        completion = call(PLANNER, prompt, budget)
        if completion.truncated:
            ticket.ratify_overrun = fingerprint
            store.update_ticket(run_id, ticket)
            raise ValueError(
                f"planner ran out of output room after {budget:,} tokens; "
                f"raise maxOutputTokens for the planner model"
            )
        revision = parse_ratify_revision(completion.text)
    except (ProviderError, ValueError) as exc:
        store.log(
            run_id,
            f"{ticket.ticket_id}: the planner could not revise the ticket from "
            f"the objections ({exc}). The next pass sees the ticket unchanged.",
            level="warn",
            kind="ticket",
        )
        return [], []

    responses = [str(r) for r in revision.get("responses", [])]
    changed = _apply(
        store, run_id, ticket, revision, root=root, criteria_locked=criteria_locked
    )
    if changed:
        store.update_ticket(run_id, ticket)
        store.log(
            run_id,
            f"{ticket.ticket_id}: ratify revised {', '.join(changed)}.",
            kind="ticket",
        )
    return changed, responses


def _attach_responses(notes: list[dict], responses: list[str]) -> None:
    """Pair the planner's answers with the objections they answer.

    Positional, and deliberately forgiving: a planner that returns fewer
    answers than there were objections leaves the rest unanswered rather than
    shifting somebody else's answer onto them. Only notes that actually raised
    something blocking are eligible — an answer attached to a sign-off reads,
    later, as a role having objected when it did not.
    """
    pending = [note for note in notes if note.get("blocking") and not note.get("response")]
    for note, response in zip(pending, responses):
        note["response"] = response
    # More answers than objections: the surplus belongs to the last one rather
    # than being dropped, since it is usually the planner elaborating.
    if pending and len(responses) > len(pending):
        extra = "; ".join(responses[len(pending):])
        pending[-1]["response"] = f"{pending[-1]['response']} {extra}".strip()


def ratify(
    store: Store,
    run_id: int,
    ticket: Ticket,
    *,
    call: Caller,
    budget_for: Callable[[str], int],
    roles: Sequence[str],
    passes: int,
    sources: dict[str, str] | None = None,
    retrieved: str = "",
    digest: str = "",
    root: Path | None = None,
    criteria_locked: bool = True,
) -> Ratification:
    """Put one ticket to every role until it is agreed, or the passes run out.

    A pass is: every role votes, and — unless this was the last pass, or
    everyone signed — the planner revises from what they said. The revision
    never comes *after* the final vote, because a ticket that shipped text
    nobody had voted on would have the exact defect this pass exists to remove.
    """
    result = Ratification(ticket.ticket_id)
    notes: list[dict] = []
    votes: list[Vote] = []

    for pass_number in range(1, max(1, passes) + 1):
        votes = []
        for role in roles:
            vote = _vote(
                store,
                run_id,
                ticket,
                role,
                call=call,
                budget=budget_for(role),
                sources=sources,
                retrieved=retrieved,
                notes=notes,
                digest=digest,
            )
            votes.append(vote)
            notes.append(vote.as_note(pass_number))

        result.passes = pass_number
        status = resolve(votes)
        if status in (UNANIMOUS, UNAVAILABLE):
            result.status = status
            result.notes = notes
            return _settle(store, run_id, ticket, result)

        if pass_number >= max(1, passes):
            break

        changed, responses = _revise(
            store,
            run_id,
            ticket,
            notes,
            call=call,
            budget=budget_for(PLANNER),
            sources=sources,
            digest=digest,
            root=root,
            criteria_locked=criteria_locked,
        )
        _attach_responses(notes, responses)
        for name in changed:
            if name not in result.changed:
                result.changed.append(name)

    result.status = resolve(votes)
    result.notes = notes
    if result.status == BLOCKED:
        objections = [
            f"{note['role']}: {point}"
            for note in notes
            if note.get("pass") == result.passes
            for point in note.get("blocking") or []
        ]
        signed = sum(1 for vote in votes if vote.signed)
        result.blocked_note = (
            f"ratification failed after {result.passes} pass(es): "
            f"{signed} of {len(votes)} roles signed off. "
            + " | ".join(objections[:6])
        ).strip()
    return _settle(store, run_id, ticket, result)


def _settle(
    store: Store, run_id: int, ticket: Ticket, result: Ratification
) -> Ratification:
    """Write the outcome onto the ticket, and make it the contract.

    `ratified_spec` and `ratified_criteria` are the anchor from here on: respec
    reads them in preference to the plan's, so a criterion four roles agreed to
    is protected from a later revision exactly as a human's is. `original_*` is
    left as ingested, so drift is still measured against what a person wrote.

    A pass nobody could be reached for settles nothing, and writes no anchor.
    """
    ticket.ratify_status = result.status
    ticket.ratify_passes = result.passes
    ticket.ratify_notes = result.notes
    if result.status not in (UNAVAILABLE, BLOCKED):
        ticket.ratify_fingerprint = ticket.fingerprint
        ticket.ratified_spec = ticket.spec
        ticket.ratified_criteria = list(ticket.criteria)
    store.record_ratification(run_id, ticket)
    return result
