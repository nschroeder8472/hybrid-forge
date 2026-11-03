"""Contracts a host opens and revises: validation, provenance and ratchets.

A ticket is a contract between whoever wrote it and the delegates that build
it. In host mode the host writes and revises contracts directly — the authority
docs/OVERSEER.md argued for — and this module is what keeps that authority
inside the rules the loop already holds its own planner to:

- every check `forge ingest` and `/forge-spec-check` run, as refusals or
  warnings rather than prose;
- provenance: a contract opened from a document a person wrote is `human`, one
  the host wrote is `host`, and the difference decides what may move
  (LOOP-INVARIANTS section 2);
- the ratchets respec is held to, applied to whoever revises:
  numbers a human wrote never move, a waiver is never written into a contract,
  and once any attempt exists a criterion may be sharpened and never dropped.

Nothing here calls a model.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Sequence

from . import evidence, routes, toolchain
from .config import Config
from .ingest import (
    HARNESS_CRITERION,
    VAGUE,
    derive_needs,
    graph_problems,
    looks_like_plan,
    parse_plan,
    shared_file_conflicts,
    undeclared_order,
    unexampled_tests,
    untestable_scope,
)
from .patch import matches_any
from .respec import _moved_numbers, _refuse_verification_waivers, dropped_criteria
from .state import (
    TICKET_BUG,
    TICKET_DONE,
    TICKET_FEATURE,
    TICKET_IMAGE,
    TICKET_RUNNING,
    Store,
    Ticket,
)

HUMAN = "human"
HOST = "host"

KINDS = {"code": TICKET_FEATURE, "feature": TICKET_FEATURE, "bug": TICKET_BUG,
         "image": TICKET_IMAGE}

# Fields a revision may carry. `title` is presentation; the rest are contract.
REVISABLE = ("title", "spec", "criteria", "allowed_files", "reference_files",
             "needs", "route", "context")


class ContractError(Exception):
    """The contract was refused. `problems` says why, one line each."""

    def __init__(self, problems: Sequence[str]):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


# ----------------------------------------------------------------------
# opening
# ----------------------------------------------------------------------


def tickets_from_fields(fields: dict[str, Any]) -> list[Ticket]:
    """One ticket from the host's own fields."""
    ticket_id = str(fields.get("id") or "").strip()
    if not ticket_id:
        raise ContractError(["a new contract needs an `id`"])
    kind = KINDS.get(str(fields.get("kind") or "code").lower())
    if kind is None:
        raise ContractError([f"kind must be one of {', '.join(sorted(KINDS))}"])
    route, note = routes.normalise(str(fields.get("route") or ""))
    if note:
        raise ContractError([f"{ticket_id}: {note}"])
    spec = str(fields.get("spec") or "").strip()
    criteria = [str(c).strip() for c in fields.get("criteria") or [] if str(c).strip()]
    return [
        Ticket(
            ticket_id,
            title=str(fields.get("title") or ticket_id),
            route=route,
            kind=kind,
            spec=spec,
            allowed_files=_paths(fields.get("allowed_files")),
            reference_files=_paths(fields.get("reference_files")),
            criteria=criteria,
            needs=[str(n) for n in fields.get("needs") or []],
            context=str(fields.get("context") or ""),
            original_spec=spec,
            original_criteria=list(criteria),
            original_context=str(fields.get("context") or ""),
        )
    ]


def tickets_from_document(text: str) -> list[Ticket]:
    """Tickets parsed verbatim from a ticket-shaped document.

    Never planned: a freeform document is the host's to turn into contracts,
    and sending it through a planner model would put a weaker reader between
    the author's words and the executor — the failure `forge ingest`'s parsed
    path exists to avoid.
    """
    if not looks_like_plan(text):
        raise ContractError(
            ["the document is not ticket-shaped ('## ID: title' sections with "
             "'### Spec'); write the contracts with fields instead"]
        )
    tickets = parse_plan(text)
    if not tickets:
        raise ContractError(["no tickets parsed from the document"])
    return tickets


def check(
    config: Config, existing: Sequence[Ticket], tickets: list[Ticket]
) -> tuple[list[str], list[str]]:
    """Refusals and warnings for `tickets` joining a backlog of `existing`.

    Refusals are the checks `forge ingest` exits on; warnings are the ones it
    prints. Both are returned as lines a host can act on.
    """
    refusals: list[str] = []
    warnings: list[str] = []
    known = {t.ticket_id for t in existing}
    for ticket in tickets:
        if ticket.ticket_id in known:
            refusals.append(
                f"{ticket.ticket_id} already exists in this run; revise it instead"
            )
        if not routes.is_withheld(ticket.route):
            if not ticket.allowed_files:
                refusals.append(f"{ticket.ticket_id}: delegated with no allowed files")
            if not ticket.spec:
                refusals.append(f"{ticket.ticket_id}: no spec")
            if not ticket.criteria:
                refusals.append(
                    f"{ticket.ticket_id}: no acceptance criteria; the tester "
                    f"encodes these, and without them nothing judges the work"
                )
            hit = [p for p in ticket.allowed_files if matches_any(p, config.never_delegate)]
            if hit:
                refusals.append(
                    f"{ticket.ticket_id}: writes {', '.join(hit)}, which this "
                    f"project never delegates (neverDelegate). Route it "
                    f"withheld:<reason> and implement it yourself"
                )
        for criterion in ticket.criteria:
            if HARNESS_CRITERION.search(criterion):
                warnings.append(
                    f"{ticket.ticket_id}: criterion the harness already settles "
                    f"(it runs the verify commands itself; the tester will "
                    f"otherwise encode this as tests that shell out) -- {criterion!r}"
                )
            elif VAGUE.search(criterion):
                warnings.append(
                    f"{ticket.ticket_id}: vague criterion, a tester cannot assert "
                    f"it -- {criterion!r}"
                )

    backlog = list(existing) + tickets
    derive_needs(backlog)
    refusals += graph_problems(backlog)
    refusals += shared_file_conflicts(backlog)
    refusals += _workspace_problems(config, tickets)

    warnings += toolchain.manifest_gaps(config, backlog)
    warnings += untestable_scope(tickets)
    warnings += unexampled_tests(tickets)
    shape = undeclared_order(config.root, backlog)
    if shape:
        warnings.append(shape)
    return refusals, warnings


def widen_reading(config: Config, ticket: Ticket) -> None:
    """What the delegates will be shown, widened the way ingest widens it.

    Reading scope is not writing scope (LOOP-INVARIANTS section 1), and a file
    the ticket's own words name is a file it may read (section 10).
    """
    named = evidence.named_paths(
        config.root, ticket.spec, *ticket.criteria
    )
    ticket.reference_files = evidence.reading_scope(
        config.root, ticket.allowed_files, ticket.reference_files, named
    )


def _workspace_problems(config: Config, tickets: Sequence[Ticket]) -> list[str]:
    from .cli import _workspace_problems as problems

    return problems(config, list(tickets))


# ----------------------------------------------------------------------
# revising
# ----------------------------------------------------------------------


def author_of(store: Store, run_id: int, ticket_id: str) -> str:
    row = store._connection.execute(
        "SELECT author FROM contract_log WHERE run_id = ? AND ticket_id = ? "
        "AND field = 'created' ORDER BY id LIMIT 1",
        (run_id, ticket_id),
    ).fetchone()
    return row["author"] if row else HUMAN


def has_attempted(ticket: Ticket) -> bool:
    return ticket.attempt_number > 0


def revise(
    config: Config,
    store: Store,
    run_id: int,
    ticket: Ticket,
    changes: dict[str, Any],
    reason: str,
    backlog: Sequence[Ticket],
) -> tuple[Ticket, list[str]]:
    """Apply a host's revision, or refuse it. Returns the ticket and warnings.

    Refused whole on any ratchet: a revision reasoned from a moved number or a
    dropped criterion has already reasoned from its absence, and keeping the
    rest of it would produce a contract that contradicts itself — the same
    rule respec follows for a dropped decision.
    """
    if not reason.strip():
        raise ContractError(["a revision needs a `reason`; it is recorded with the change"])
    if ticket.status == TICKET_RUNNING:
        raise ContractError(
            [f"{ticket.ticket_id} is being worked; cancel its job or wait for it"]
        )
    if ticket.status == TICKET_DONE:
        raise ContractError(
            [f"{ticket.ticket_id} is done; open a new contract for further work"]
        )
    unknown = sorted(set(changes) - set(REVISABLE))
    if unknown:
        raise ContractError([f"cannot revise {', '.join(unknown)}"])

    author = author_of(store, run_id, ticket.ticket_id)
    revision: dict[str, Any] = {}
    for name, value in changes.items():
        if name in ("criteria",):
            revision[name] = [str(c).strip() for c in value or [] if str(c).strip()]
        elif name in ("allowed_files", "reference_files"):
            revision[name] = _paths(value)
        elif name == "needs":
            revision[name] = [str(n) for n in value or []]
        elif name == "route":
            route, note = routes.normalise(str(value or ""))
            if note:
                raise ContractError([f"{ticket.ticket_id}: {note}"])
            revision[name] = route
        else:
            revision[name] = str(value or "")

    refusals: list[str] = []

    # A waiver is refused from everyone, before and after any attempt:
    # amnesty is measured, never asserted (LOOP-INVARIANTS section 11).
    probe = dict(revision)
    for name, waiver in _refuse_verification_waivers(ticket, probe):
        refusals.append(
            f"the revised {name} excuses a failing check ({waiver}). What "
            f"pre-dates a ticket is measured by the baseline, per error; a "
            f"contract that asserts it in prose teaches every attempt to look "
            f"past the diagnostic that says what broke"
        )

    # The anchor moves with a host's own contract until something has been
    # built against it; after that it is a contract on the record.
    frozen = author == HUMAN or has_attempted(ticket)
    if frozen and "spec" in revision:
        moved = _moved_numbers(ticket, revision["spec"])
        if moved:
            refusals.append(
                f"the revised spec no longer states {', '.join(moved)}, which the "
                f"{'human' if author == HUMAN else 'contract as first attempted'} "
                f"stated. A revision may say the spec better; it may not change "
                f"what the numbers are. If a number is wrong, that is a person's "
                f"call"
            )
    if frozen and "criteria" in revision:
        dropped = dropped_criteria(ticket, revision["criteria"])
        if dropped:
            refusals.append(
                "the revised criteria drop what the contract asserted: "
                + "; ".join(dropped)
                + ". A criterion may be made more precise and never easier"
            )
    if refusals:
        raise ContractError(refusals)

    old = {name: getattr(ticket, name) for name in revision}
    for name, value in revision.items():
        setattr(ticket, name, value)
    if not frozen:
        # The host is still drafting its own contract.
        ticket.original_spec = ticket.spec
        ticket.original_criteria = list(ticket.criteria)
        ticket.original_context = ticket.context

    if {"spec", "criteria", "allowed_files", "reference_files"} & set(revision):
        widen_reading(config, ticket)

    others = [t for t in backlog if t.ticket_id != ticket.ticket_id]
    problems, warnings = check(config, others, [ticket])
    if problems:
        for name, value in old.items():
            setattr(ticket, name, value)
        raise ContractError(problems)

    store.update_ticket(run_id, ticket)
    for name, value in revision.items():
        if old[name] != value:
            log_change(store, run_id, ticket.ticket_id, HOST, name, old[name], value, reason)
    return ticket, warnings


def log_change(
    store: Store,
    run_id: int,
    ticket_id: str,
    author: str,
    field: str,
    old: Any,
    new: Any,
    reason: str,
) -> None:
    with store._write() as connection:
        connection.execute(
            "INSERT INTO contract_log (run_id, ticket_id, author, field, old, new, "
            "reason, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, ticket_id, author, field, _text(old), _text(new), reason, time.time()),
        )


def history(store: Store, run_id: int, ticket_id: str) -> list[dict[str, Any]]:
    rows = store._connection.execute(
        "SELECT * FROM contract_log WHERE run_id = ? AND ticket_id = ? ORDER BY id",
        (run_id, ticket_id),
    ).fetchall()
    return [
        {"author": r["author"], "field": r["field"], "old": r["old"],
         "new": r["new"], "reason": r["reason"], "at": r["at"]}
        for r in rows
    ]


def _text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value)


def _paths(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        value = [value]
    return [Path(str(p)).as_posix().strip() for p in value if str(p).strip()]
