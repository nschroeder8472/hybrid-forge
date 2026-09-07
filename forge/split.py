"""Decomposing a ticket the loop cannot finish, with scope conserved.

§6 of docs/ADAPTIVE-TICKET-LOOP.md. The loop has three remedies for a ticket
that will not land — another attempt, a respec, a park — and all three keep it
whole. Decomposition is the fourth, and the reason it is safe where respec is
not fits in one line:

    the union of the children's acceptance criteria must cover the parent's

Respec conserves scope by judgement: a model rewrites a contract and a ratchet
refuses the removals it can see. A split conserves it by construction, because
every parent criterion is claimed by a child before any child is created and a
criterion nobody claims fails the split outright. It is the criteria ratchet's
rule applied across a decomposition rather than within one ticket, which is why
the checking lives beside `respec._merge_criteria`'s reasoning and not in a new
kind of gate.

**The trigger ships off.** `loop.volumeThreshold` defaults to `0`. Against the
only run whose numbers exist, the drafted threshold of eight distinct classes
decomposes the two tickets that went on to pass and leaves the one genuinely
unsatisfiable ticket alone — the same shape as the `flatCycles` finding, and
the same conclusion. The mechanism is built and the brake is not armed, so the
first person to arm it does so against a number they can see rather than
against a default nobody measured.

Nothing in this module calls a model, writes to a database or reads a config.
It parses a proposal, checks it, and builds tickets.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .state import TICKET_BUG, TICKET_PENDING, Ticket


class SplitRefused(Exception):
    """The proposal is not a decomposition of this ticket.

    Raised rather than returned because every caller does the same thing with
    it — escalate the parent, unsplit — and a refusal that can be ignored by
    forgetting to check a boolean is a refusal that will be.
    """


@dataclass
class Child:
    """One proposed child, before it becomes a `Ticket`."""

    ticket_id: str
    title: str
    spec: str
    criteria: list[str]
    covers: list[int]
    allowed_files: list[str]
    needs: list[str]


def parse_split(text: str) -> list[Child]:
    """Read a planner's decomposition, or raise `SplitRefused`.

    JSON, because this is the one planner reply that is a *structure* rather
    than prose: which child covers which parent criterion is an index into a
    list, and asking a model to write that in sentences and parsing it back is
    how an off-by-one becomes a dropped obligation.

    Every field is required. A child with no `covers` is not a child of this
    parent, and a permissive parser that defaults it to the empty list turns
    the invariant below into a check that passes because it was asked nothing.
    """
    block = _json_block(text)
    if block is None:
        raise SplitRefused("no JSON object in the reply")
    try:
        data = json.loads(block)
    except ValueError as exc:
        raise SplitRefused(f"the reply is not valid JSON ({exc})") from exc

    raw = data.get("children")
    if not isinstance(raw, list) or not raw:
        raise SplitRefused("the reply proposes no children")

    children: list[Child] = []
    for index, entry in enumerate(raw, start=1):
        if not isinstance(entry, dict):
            raise SplitRefused(f"child {index} is not an object")
        covers = entry.get("covers")
        if not isinstance(covers, list) or not covers:
            raise SplitRefused(f"child {index} claims no parent criteria")
        try:
            claimed = [int(value) for value in covers]
        except (TypeError, ValueError) as exc:
            raise SplitRefused(
                f"child {index} names a parent criterion that is not a number"
            ) from exc
        criteria = [
            str(item).strip()
            for item in (entry.get("criteria") or [])
            if str(item).strip()
        ]
        if not criteria:
            raise SplitRefused(f"child {index} has no acceptance criteria")
        children.append(
            Child(
                ticket_id=str(entry.get("id") or "").strip(),
                title=str(entry.get("title") or "").strip(),
                spec=str(entry.get("spec") or "").strip(),
                criteria=criteria,
                covers=claimed,
                allowed_files=[
                    str(path).strip()
                    for path in (entry.get("allowed_files") or [])
                    if str(path).strip()
                ],
                needs=[
                    str(dep).strip()
                    for dep in (entry.get("needs") or [])
                    if str(dep).strip()
                ],
            )
        )
    return children


_FENCE = re.compile(r"```(?:json)?\s*(.+?)```", re.DOTALL)


def _json_block(text: str) -> str | None:
    """The JSON object in a reply, fenced or bare."""
    fenced = _FENCE.search(text or "")
    if fenced:
        return fenced.group(1).strip()
    start = (text or "").find("{")
    end = (text or "").rfind("}")
    if start < 0 or end <= start:
        return None
    return text[start : end + 1]


def check(parent: Ticket, children: list[Child], *, max_children: int) -> None:
    """The invariant, and the floor and ceiling around it.

    Covers `contract_criteria` rather than the current list, so a parent that
    invented criteria for itself along the way cannot hand them down as
    obligations — the split is a decomposition of what somebody signed, not of
    what the loop last wrote.

    A criterion may be covered by several children or by exactly one. Never by
    zero: that is the case this whole mechanism exists to make impossible, and
    it is the one a model asked to "break this into smaller pieces" produces
    by leaving out the hard part.
    """
    contract = parent.contract_criteria
    if not contract:
        raise SplitRefused(
            "the parent has no ratified or plan-authored criteria, so there is "
            "nothing a split could be checked against"
        )
    if len(children) > max_children:
        raise SplitRefused(
            f"{len(children)} children proposed against a ceiling of "
            f"{max_children}; a ticket that needs more than that was mis-scoped "
            f"at plan time, and splitting it hides that"
        )
    if len(children) < 2:
        raise SplitRefused(
            "a split into one child is a respec wearing a different name"
        )

    valid = range(1, len(contract) + 1)
    claimed: set[int] = set()
    for child in children:
        for index in child.covers:
            if index not in valid:
                raise SplitRefused(
                    f"{child.ticket_id or 'a child'} claims parent criterion "
                    f"{index}, and the parent has {len(contract)}"
                )
            claimed.add(index)

    uncovered = sorted(set(valid) - claimed)
    if uncovered:
        missing = "; ".join(contract[index - 1] for index in uncovered)
        raise SplitRefused(
            f"{len(uncovered)} parent criterion/criteria are covered by no "
            f"child: {missing}"
        )

    seen: set[str] = set()
    for child in children:
        if not child.ticket_id:
            raise SplitRefused("a child has no id")
        if child.ticket_id in seen:
            raise SplitRefused(f"two children share the id {child.ticket_id}")
        seen.add(child.ticket_id)
        if not child.spec.strip():
            raise SplitRefused(f"{child.ticket_id} has no spec")
        if not child.allowed_files:
            raise SplitRefused(
                f"{child.ticket_id} may write nothing, so it cannot land work"
            )
        outside = sorted(set(child.allowed_files) - set(parent.allowed_files))
        if outside:
            raise SplitRefused(
                f"{child.ticket_id} claims files the parent may not write: "
                + ", ".join(outside)
            )


def splittable(parent: Ticket, *, max_depth: int) -> str:
    """Why this ticket must not be split, or "" if it may be.

    A sentence rather than a boolean, because every one of these ends up in a
    log line a person reads to find out why a stalled ticket was parked rather
    than decomposed.
    """
    if parent.kind == TICKET_BUG:
        # Its contract was written before the fix, by a tester, as a failing
        # test. The party being judged does not get to restate it in smaller
        # pieces, and the reproduction is the one artifact the loop never
        # reclaims.
        return "a bug ticket is not split: its contract is a reproduction"
    if parent.split_depth >= max_depth:
        return (
            f"already {parent.split_depth} split(s) deep, at a limit of "
            f"{max_depth}; a child that fails at the limit fails its parent"
        )
    if parent.route != "delegate":
        return "the ticket is withheld from the executor"
    return ""


def children_of(parent: Ticket, children: list[Child]) -> list[Ticket]:
    """The proposal as tickets, ready to be added to the run.

    Four things are inherited rather than taken fresh, and each is a bug that
    would otherwise be written once per child:

    - `baseline_tree`, because a child taking its own baseline is judged
      against a tree its parent already reddened, and the invariant about *red
      the backlog did not start with* is then computed against the wrong tree.
    - `context` and the parent's own spec as reference material, so a child
      reads the thing it is a sixth of.
    - `learned`, so what the parent's attempts established is not rediscovered
      once per child. Each child takes a snapshot at creation; siblings do not
      yet share a live tier, which is what §3.2 of the specification still
      owes.
    - `route`, which is `delegate` by the time anything gets here — a withheld
      ticket is refused by `splittable`.

    Tests are deliberately *not* inherited. `freezeTests` fingerprints the
    criteria, the spec, the scope and the test command; a child differs in
    three of the four, so its fingerprint differs and its tests are written
    fresh against its own contract.
    """
    contract = parent.contract_criteria
    ordered: list[Ticket] = []
    for offset, child in enumerate(children):
        covered = "\n".join(
            f"- {contract[index - 1]}" for index in sorted(set(child.covers))
        )
        ordered.append(
            Ticket(
                ticket_id=child.ticket_id,
                title=child.title or child.ticket_id,
                status=TICKET_PENDING,
                position=parent.position * 100 + offset + 1,
                spec=child.spec,
                criteria=list(child.criteria),
                allowed_files=list(child.allowed_files),
                reference_files=list(parent.reference_files),
                needs=list(child.needs),
                context=parent.context,
                kind=parent.kind,
                route=parent.route,
                baseline_tree=parent.baseline_tree,
                learned=[dict(entry) for entry in parent.learned],
                parent_ticket_id=parent.ticket_id,
                covers=sorted(set(child.covers)),
                split_depth=parent.split_depth + 1,
                original_spec=child.spec,
                original_criteria=list(child.criteria),
                original_context=(
                    f"{parent.context}\n\n"
                    f"This ticket is part of {parent.ticket_id}, which was split "
                    f"because it could not be finished whole. What "
                    f"{parent.ticket_id} was asked for:\n\n{parent.spec}\n\n"
                    f"The parent criteria this ticket is responsible for:\n"
                    f"{covered}"
                ).strip(),
            )
        )
    return ordered
