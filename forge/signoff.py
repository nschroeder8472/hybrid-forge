"""What the sign-off pass has actually been doing.

`loop.ratifyPasses` defaults to 2 because a ticket nobody can build is
expensive — two of them cost 650 attempts and 16.6M tokens on the run that
argument comes from. That is a judgement about the cost of the failure, not a
measurement of the fix, and docs/RATIFY.md has owed the measurement since it
was written.

Two counters, both over data the loop already records, and neither of them
changes anything:

- **Participation** (§8.2 of docs/ADAPTIVE-TICKET-LOOP.md). Per role: tickets
  voted on, times signed, times blocking, points raised. A role that has never
  blocked over `loop.participationWindow` tickets is surfaced. From inside the
  loop that is indistinguishable from genuine agreement, which is why it needs
  a counter rather than a rule.
- **Efficacy** (§8.3). Per ticket, how much of what went wrong afterwards was
  visible at sign-off time. A failure in a file the ticket already had in
  scope is one the sign-off pass could have asked about, and a high share of
  those means the pass is answering the wrong question — the remedy being a
  prompt change ("what would you reject in an implementation of this spec")
  with a number behind it, which is the only kind worth making.

What "visible" can mean here is bounded by what is recorded. A failure class
carries the file it is about, and `allowed_files` plus `reference_files` are
the files the ticket was scoped to when the roles signed; a criterion is prose
and nothing mechanical can say whether an objection was reachable from it. So
this counts file visibility and says so, rather than estimating the rest.

Read-only. Nothing in this module writes to the database or to a ticket.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .failures import locations
from .state import Store, Ticket


@dataclass
class Participation:
    """One role's record across the tickets it voted on."""

    role: str
    tickets: int = 0
    votes: int = 0
    signed: int = 0
    blocking: int = 0
    points: int = 0

    @property
    def block_rate(self) -> float:
        """Share of this role's votes that carried a blocking objection."""
        return self.blocking / self.votes if self.votes else 0.0


@dataclass
class Efficacy:
    """One ticket's failures, split by whether sign-off could have seen them."""

    ticket_id: str
    classes: int = 0
    in_scope: int = 0
    points: int = 0
    points_in_scope: int = 0

    @property
    def visible(self) -> int:
        return self.in_scope + self.points_in_scope

    @property
    def total(self) -> int:
        return self.classes + self.points

    @property
    def share(self) -> float:
        return self.visible / self.total if self.total else 0.0


@dataclass
class Report:
    """Everything `forge signoff` prints, computed and never acted on."""

    participation: list[Participation] = field(default_factory=list)
    efficacy: list[Efficacy] = field(default_factory=list)
    ratified: int = 0
    window: int = 20

    @property
    def rubber_stamps(self) -> list[Participation]:
        """Roles that have never blocked, once there is enough to say so.

        Silent below the window on purpose. A role that has signed three
        tickets and blocked none of them is a role that has seen three good
        tickets, and naming it teaches a reader to ignore this line.
        """
        if self.ratified < self.window:
            return []
        return [entry for entry in self.participation if entry.votes and not entry.blocking]


def participation(tickets: list[Ticket]) -> list[Participation]:
    """Per role, what it did in every sign-off pass it took part in."""
    found: dict[str, Participation] = {}
    for ticket in tickets:
        roles_here: set[str] = set()
        for note in ticket.ratify_notes:
            role = str(note.get("role") or "")
            if not role:
                continue
            entry = found.setdefault(role, Participation(role=role))
            entry.votes += 1
            if note.get("signed"):
                entry.signed += 1
            blocking = [
                text for text in (note.get("blocking") or []) if _said_something(text)
            ]
            suggestions = [
                text
                for text in (note.get("suggestions") or [])
                if _said_something(text)
            ]
            if blocking:
                entry.blocking += 1
            entry.points += len(blocking) + len(suggestions)
            roles_here.add(role)
        for role in roles_here:
            found[role].tickets += 1
    return sorted(found.values(), key=lambda entry: entry.role)


# What a role writes when it has nothing to say. Both spellings appear in
# recorded runs, and counting them as points would make every role look
# engaged.
_NOTHING = {"none", "nothing", "n/a", "na", "-", ""}


def _said_something(text: object) -> bool:
    return str(text or "").strip().strip(".").lower() not in _NOTHING


def efficacy(store: Store, run_id: int, tickets: list[Ticket]) -> list[Efficacy]:
    """Per ticket, how much of what failed afterwards named a file in scope.

    A class or a review point that names a file the ticket was already scoped
    to is one the sign-off pass had the material to ask about. One that names a
    file nobody had listed is not — it is something the work discovered, and no
    reading of the spec would have produced it.
    """
    rows: list[Efficacy] = []
    for ticket in tickets:
        if not ticket.ratify_status:
            continue
        scope = {path for path in ticket.allowed_files}
        scope.update(ticket.reference_files)
        row = Efficacy(ticket_id=ticket.ticket_id)
        for entry in store.ticket_classes(run_id, ticket.ticket_id):
            row.classes += 1
            if _names_scope(entry["name"], scope):
                row.in_scope += 1
        for entry in store.ticket_points(run_id, ticket.ticket_id):
            row.points += 1
            if _names_scope(entry["name"], scope):
                row.points_in_scope += 1
        if row.total:
            rows.append(row)
    return rows


def _names_scope(name: str, scope: set[str]) -> bool:
    """Does this class name a file the ticket already had?

    Compared on the path as the class carries it and on its tail, because a
    class is built from what a tool printed — `./tests/test_a.py`,
    `tests\\test_a.py` and `tests/test_a.py` are one file — while scope is
    written the way the plan wrote it.
    """
    cited = set(locations(name))
    tail = name.rsplit(" in ", 1)[-1].strip().replace("\\", "/") if " in " in name else ""
    if tail:
        cited.add(tail)
    for path in cited:
        cleaned = path.lstrip("./")
        for owned in scope:
            here = owned.replace("\\", "/").lstrip("./")
            if cleaned == here or cleaned.endswith("/" + here) or here.endswith(
                "/" + cleaned
            ):
                return True
    return False


def report(store: Store, run_id: int, window: int = 20) -> Report:
    """The whole thing, for one run."""
    tickets = store.list_tickets(run_id)
    ratified = [ticket for ticket in tickets if ticket.ratify_status]
    return Report(
        participation=participation(ratified),
        efficacy=efficacy(store, run_id, ratified),
        ratified=len(ratified),
        window=window,
    )


def render(result: Report) -> str:
    """The report as a person reads it."""
    lines: list[str] = []
    if not result.ratified:
        return (
            "No ticket in this run has been through a sign-off pass. Either "
            "loop.ratifyPasses is 0, or the run has not reached one yet."
        )

    lines.append(f"Sign-off over {result.ratified} ratified ticket(s).")
    lines.append("")
    lines.append("Participation, per role:")
    lines.append(
        f"  {'role':<10}{'tickets':>8}{'votes':>7}{'signed':>8}"
        f"{'blocking':>10}{'points':>8}"
    )
    for entry in result.participation:
        lines.append(
            f"  {entry.role:<10}{entry.tickets:>8}{entry.votes:>7}"
            f"{entry.signed:>8}{entry.blocking:>10}{entry.points:>8}"
        )

    for entry in result.rubber_stamps:
        lines.append("")
        lines.append(
            f"  {entry.role} has blocked nothing across {entry.votes} vote(s). "
            f"That is what a role that agrees looks like and also what a role "
            f"that is not reading looks like; the two are told apart by a "
            f"person, not by this report."
        )

    if result.efficacy:
        visible = sum(row.visible for row in result.efficacy)
        total = sum(row.total for row in result.efficacy)
        lines.append("")
        lines.append(
            f"Sign-off efficacy: {visible} of {total} later failure(s) named a "
            f"file the ticket already had in scope "
            f"({100 * visible / total:.0f}%)."
        )
        lines.append("  Per ticket:")
        for row in sorted(result.efficacy, key=lambda row: -row.share):
            lines.append(
                f"    {row.ticket_id:<12}{row.visible:>4} of {row.total:<4} "
                f"({100 * row.share:.0f}%)"
            )
        lines.append(
            "  A high share means the pass had the material to raise these "
            "objections and did not. Nothing here changes what the loop does."
        )
    return "\n".join(lines)
