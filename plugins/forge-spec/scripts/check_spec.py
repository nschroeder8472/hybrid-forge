"""Dry-run a spec through the ingest parser, without creating a run.

`forge ingest` is the real check, but it writes tickets and opens a run in the
database — which makes it the wrong tool for the question "did I write this
correctly?", asked five times while drafting. This asks exactly that and writes
nothing.

Everything it reports comes from `forge.ingest` itself, so it cannot drift from
what ingest will actually do. What it adds on top are the authoring traps the
parser does not consider errors: a bullet whose continuation is not indented
loses its tail, a section the parser does not recognize folds its bullets into
the section above it, and a criterion restating what the harness already runs
buys nothing while costing a test that shells out to run it. All of them are
silent, and all of them change what the executor is told to do.

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
        untestable_scope,
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

# The route spelling that names the party who decided instead of the objection.
RETIRED_ROUTE = re.compile(r"^\*\*Route:\*\*\s*claude-only\b", re.IGNORECASE)

# A criterion about the project's own commands rather than about its code.
HARNESS_CRITERION = re.compile(
    r"\b(?:npm|npx|yarn|pnpm|bun|cargo|go|make|just|uv|poetry|pip|python|pytest|"
    r"tox|dotnet|gradle|gradlew|mvn|swift|mix|composer|deno)\b"
    r"[^.]{0,140}?\bexits?\b[^.]{0,40}?\b0\b",
    re.IGNORECASE,
)

# A test file is written by the tester and read by nobody, so one that no other
# ticket names is the ordinary case rather than a loose end.
TEST_PATH = re.compile(r"(^|/)(tests?|spec)/|[._-](test|spec)\.[a-z0-9]+$")

# Files that are terminal by convention: an entry point is imported by the
# runtime rather than by a module, and a config file is read by a tool. Neither
# is waiting for a call site, so neither is evidence of anything.
TERMINAL_PATH = re.compile(
    r"(^|/)(index|main|app|cli|__main__|mod|lib)\.[a-z0-9]+$"
    r"|(^|/)[^/]*\.config\.[a-z0-9]+$"
    r"|(^|/)(tsconfig[^/]*|package|package-lock|deno|composer|cargo|pyproject|go)"
    r"\.(json|jsonc|toml|mod)$"
    r"|\.(html|css|md|ya?ml|ini|cfg|txt)$",
    re.IGNORECASE,
)


# An entry point: the file a person starts, as opposed to a module something
# imports. Narrower than TERMINAL_PATH, which also covers config.
RUNNABLE_PATH = re.compile(
    r"(^|/)(index\.html?|main|app|cli|server|__main__|bin)\.[a-z0-9]+$"
    r"|(^|/)(index|main)\.html?$",
    re.IGNORECASE,
)


def normalized(path: str) -> str:
    return path.replace("\\", "/").strip().lower()


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


def lazy_continuations(text: str) -> list[str]:
    """Bullets continued on an unindented line — that line is dropped.

    A wrapped bullet is fine as long as its continuation is indented, which is
    what every formatter produces. Flush-left is the ambiguous case, and the
    parser resolves it the safe way: "- **Decision:** the store is SQLite"
    followed by an unindented "The board is ten columns wide" is one author
    writing two things, and joining them would put a sentence nobody marked
    under a protection nobody asked for. The cost is that a criterion wrapped
    flush-left still reaches the tester as its first line only.

    Indent the continuation by two spaces and it joins.
    """
    lines = text.splitlines()
    found = []
    for index, line in enumerate(lines[:-1]):
        if not BULLET.match(line):
            continue
        nxt = lines[index + 1]
        if not nxt.strip() or BULLET.match(nxt) or HEADING.match(nxt):
            continue
        if not nxt[:1].isspace():
            found.append(line.strip())
    return found


def retired_routes(text: str) -> list[str]:
    """`Route:` lines using the old spelling, which records no reason.

    `claude-only` still withholds the ticket — nothing about that changed — but
    it names the party who decided rather than the objection, and it reads as
    `withheld:unspecified` everywhere it is displayed. A reader six weeks later
    cannot reconstruct which category it fell under.
    """
    return [
        line.strip() for line in text.splitlines() if RETIRED_ROUTE.match(line.strip())
    ]


def harness_criteria(tickets: list) -> list[str]:
    """Criteria asserting that the project's own commands exit 0.

    The harness runs lint, typecheck, the build and the suite before anything
    is judged, and review happens only on a tree where they passed, so a
    criterion repeating that is settled by the run itself.

    It is not inert, either. The tester's job is to turn every criterion into an
    assertion — so one backlog that put "npm run test exits 0" on all five of
    its tickets got a suite that shelled out to run all four commands, invoked
    itself behind an environment-variable guard to avoid recursing, took ten
    times as long, and measured a different suite from the one that runs.
    """
    found = []
    for ticket in tickets:
        for criterion in ticket.criteria:
            if HARNESS_CRITERION.search(criterion):
                found.append(f"{ticket.ticket_id}: {criterion}")
    return found


def unread_products(tickets: list) -> list[str]:
    """Files a ticket writes that no other ticket writes, reads, or names.

    Not an error — a leaf module is ordinary and most backlogs have one. Worth
    printing because the failing shape is indistinguishable from inside the
    spec: a module something was supposed to call, where no ticket was ever
    given the file holding the call.

    One backlog shipped a coordinate ruler that way. Its ticket said "the shell
    paints it", the shell's file belonged to a ticket that had already landed,
    and the sentence naming the integration sat in the only ticket that could
    not act on it. Every check passed and nothing ever imported the result.
    """
    seen: dict[str, set[str]] = {}
    for ticket in tickets:
        for path in list(ticket.allowed_files) + list(ticket.reference_files):
            seen.setdefault(normalized(path), set()).add(ticket.ticket_id)
    found = []
    for ticket in tickets:
        for path in ticket.allowed_files:
            key = normalized(path)
            if TEST_PATH.search(key) or TERMINAL_PATH.search(key):
                continue
            if seen.get(key, set()) - {ticket.ticket_id}:
                continue
            found.append(f"{ticket.ticket_id}: {path}")
    return found


def entry_points(tickets: list) -> list[str]:
    """Entry points this backlog writes — files a person starts.

    Printed as a reminder rather than a warning, because the thing it asks about
    lives in prose the parser cannot read. When a backlog produces something
    runnable, three requirements are routinely left out of the spec and all
    three fail silently: how it starts, the control that gets data into it, and
    what the person sees. One backlog shipped an editor with a hidden file input
    and nothing to open it — 146 tests green, four commands clean, and a page
    that could not be used at all.
    """
    found = []
    for ticket in tickets:
        for path in ticket.allowed_files:
            if RUNNABLE_PATH.search(normalized(path)):
                found.append(f"{ticket.ticket_id}: {path}")
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
    warn += untestable_scope(tickets)
    for ticket in tickets:
        for criterion in ticket.criteria:
            if VAGUE.search(criterion):
                warn.append(f"{ticket.ticket_id}: vague criterion -- {criterion!r}")
    for bullet in lazy_continuations(text):
        warn.append(
            f"bullet continued on an unindented line, tail is dropped -- {bullet!r}"
        )
    for heading in unrecognized_headings(text):
        warn.append(f"unrecognized heading, folds into the section above -- {heading!r}")
    for line in retired_routes(text):
        warn.append(f"retired route spelling, records no reason -- {line!r}")
    for item in harness_criteria(tickets):
        warn.append(f"criterion the harness already settles -- {item}")

    derived = derive_needs(tickets)
    if derived:
        print("\nOrdering edges ingest will add (shared files):")
        for later, earlier, path_name in derived:
            print(f"  {later} after {earlier}  ({path_name})")

    runnable = entry_points(tickets)
    if runnable:
        print("\nThis backlog writes an entry point:")
        for item in runnable:
            print(f"  > {item}")
        print("  Check some ticket owns each of: the command that starts it,")
        print("  the control a person uses to give it something to work on,")
        print("  what the readout says, and what shows before anything loads.")

    loose = unread_products(tickets)
    if loose:
        print("\nWritten here, named by no other ticket:")
        for item in loose:
            print(f"  . {item}")
        print("  A leaf module is fine. A module something was meant to call is")
        print("  not -- check that some ticket owns the file holding the call.")

    # `respec._DECISION_FLOOR`: a decision short enough to turn up inside an
    # unrelated sentence by accident proves nothing, so the ratchet ignores it.
    # Counting those here would report protection the run does not have.
    decisions = [item for item in plan_decisions(text) if len(item) >= 24]
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
