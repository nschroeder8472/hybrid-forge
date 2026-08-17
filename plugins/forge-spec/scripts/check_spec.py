"""Dry-run a spec through the ingest parser, without creating a run.

`forge ingest` is the real check, but it writes tickets and opens a run in the
database — which makes it the wrong tool for the question "did I write this
correctly?", asked five times while drafting. This asks exactly that and writes
nothing.

Everything it reports comes from `forge.ingest` itself, so it cannot drift from
what ingest will actually do. What it adds on top are the authoring traps the
parser does not consider errors: a criterion wrapped onto a second line loses
its tail, and a section the parser does not recognize folds its bullets into
the section above it. Both are silent, and both change what the executor is
told to do.

    python check_spec.py <spec.md>

Exit 1 when ingest would refuse the document or fall through to the planner.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    from forge.ingest import (
        _FIELD,
        _TICKET_HEADER,
        derive_needs,
        graph_problems,
        looks_like_plan,
        parse_plan,
        plan_decisions,
        shared_file_conflicts,
    )
except ImportError:
    sys.exit(
        "error: hybrid-forge is not installed in this Python.\n"
        "  pipx install hybrid-forge   (or: pip install -e . from a clone)"
    )

# A criterion that hedges instead of asserting. Not an error — a prompt.
VAGUE = re.compile(
    r"\b(gracefully|properly|correctly|as expected|appropriate(?:ly)?|"
    r"reasonable|sensible|works|handles?)\b",
    re.IGNORECASE,
)

BULLET = re.compile(r"^\s*[-*+]\s+\S")
HEADING = re.compile(r"^#{1,6}\s+(?P<text>.+?)\s*$")


def unrecognized_headings(text: str) -> list[str]:
    """Headings inside a ticket that the parser ignores.

    Only inside one: everything before the first ticket header is preamble the
    parser never reads, so a document title or an out-of-scope note there costs
    nothing. The same heading between two parsed sections is different — its
    bullets join the section above it.

    Only when it holds bullets, too. Absorbed prose is harmless — a `Notes`
    paragraph landing at the end of `Context` reaches the executor as more
    context and nothing changes. An absorbed *list* is the damaging case: those
    items become allowed files, or criteria, depending on what precedes them.
    """
    known = list(_FIELD.values())
    found = []
    in_ticket = False
    pending: str | None = None
    for line in text.splitlines():
        if _TICKET_HEADER.match(line):
            in_ticket, pending = True, None
            continue
        match = HEADING.match(line)
        if match:
            pending = None
            if in_ticket and not any(pattern.match(line) for pattern in known):
                pending = match.group("text")
            continue
        if pending and BULLET.match(line):
            found.append(pending)
            pending = None
    return found


def wrapped_bullets(text: str) -> list[str]:
    """Bullets whose next line continues them — the continuation is dropped."""
    lines = text.splitlines()
    found = []
    for index, line in enumerate(lines[:-1]):
        if not BULLET.match(line):
            continue
        nxt = lines[index + 1]
        if nxt.strip() and not BULLET.match(nxt) and not HEADING.match(nxt):
            found.append(line.strip())
    return found


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.exit("usage: check_spec.py <spec.md>")
    path = Path(argv[1])
    if not path.is_file():
        sys.exit(f"error: no such file: {path}")
    text = path.read_text(encoding="utf-8")

    if not looks_like_plan(text):
        print("PLANNED -- ingest would hand this to the planner model.")
        print("  A document is parsed verbatim only when it has at least one")
        print("  ticket header (`## AB-001: title`) and a `## Spec` heading.")
        print(f"  Ticket headers found: {len(_TICKET_HEADER.findall(text))}")
        print(f"  Spec headings found:  {len(_FIELD['spec'].findall(text))}")
        return 1

    tickets = parse_plan(text)
    print(f"PARSED -- {len(tickets)} ticket(s), used verbatim.\n")
    for ticket in tickets:
        print(f"  {ticket.ticket_id}  [{ticket.route}/{ticket.kind}]  {ticket.title}")
        print(f"      writes {len(ticket.allowed_files)}, reads {len(ticket.reference_files)}, "
              f"{len(ticket.criteria)} criteria, needs {ticket.needs or '-'}")

    fatal: list[str] = []
    fatal += graph_problems(tickets)
    fatal += shared_file_conflicts(tickets)
    for ticket in tickets:
        if not ticket.spec.strip():
            fatal.append(f"{ticket.ticket_id}: empty spec")
        if not ticket.criteria:
            fatal.append(f"{ticket.ticket_id}: no acceptance criteria")
        if ticket.route == "delegate" and not ticket.allowed_files:
            fatal.append(f"{ticket.ticket_id}: delegated with no allowed files")

    warn: list[str] = []
    for ticket in tickets:
        for criterion in ticket.criteria:
            if VAGUE.search(criterion):
                warn.append(f"{ticket.ticket_id}: vague criterion -- {criterion!r}")
    for bullet in wrapped_bullets(text):
        warn.append(f"wrapped bullet, tail is dropped -- {bullet!r}")
    for heading in unrecognized_headings(text):
        warn.append(f"unrecognized heading, folds into the section above -- {heading!r}")

    derived = derive_needs(tickets)
    if derived:
        print("\nOrdering edges ingest will add (shared files):")
        for later, earlier, path_name in derived:
            print(f"  {later} after {earlier}  ({path_name})")

    decisions = plan_decisions(text)
    print(f"\nProtected decisions: {len(decisions)}")
    for decision in decisions[:5]:
        print(f"  - {decision}")

    if warn:
        print(f"\nWarnings ({len(warn)}):")
        for item in warn:
            print(f"  ! {item}")

    if fatal:
        print(f"\nIngest would REFUSE this backlog ({len(fatal)}):")
        for item in fatal:
            print(f"  x {item}")
        return 1

    print("\nOK -- ingest would accept this backlog.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
