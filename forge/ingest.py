"""Turn an outside document into a backlog.

The loop's input does not have to come from a planning session in this tool. A
spec written in the Claude desktop app, a design doc from a chat on the web, a
plan Claude Code produced in an ordinary session, a PRD a human wrote by hand —
any of them can seed a run.

Two paths, and the order matters:

1. **The document already reads as a plan.** If it contains ticket-shaped
   sections, they are parsed directly. No model runs, nothing is rephrased,
   and the acceptance criteria the author wrote are the ones the executor is
   judged against. Preferring this path is not an optimization — re-planning a
   document that was already planned is how a carefully specified ticket
   quietly turns into a different ticket.

2. **The document is freeform.** Then the planner role converts it into
   tickets, and the result is written back as markdown for review before the
   loop touches it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .patch import normalize_path
from .providers import Message, Provider
from .state import TICKET_FEATURE, Ticket

# A ticket section: "## IM-014: Add PNG export" or "# IM-014 — Add PNG export".
_TICKET_HEADER = re.compile(
    r"^#{1,3}\s*(?P<id>[A-Z][A-Z0-9]*-\d+)\s*[:—\-]?\s*(?P<title>.*)$",
    re.MULTILINE,
)

_FIELD = {
    "spec": re.compile(r"^#{1,4}\s*Spec\s*$", re.MULTILINE | re.IGNORECASE),
    "allowed": re.compile(r"^#{1,4}\s*Allowed files\s*$", re.MULTILINE | re.IGNORECASE),
    # Matched loosely because this is the heading `write_tickets` emits, and it
    # emits it with a parenthetical: "## Reference files (read-only)". Without
    # a pattern here the section is not a boundary at all, so its bullets fall
    # into whichever known section precedes them — usually `Allowed files`,
    # which silently promotes a read-only file to a writable one.
    "reference": re.compile(r"^#{1,4}\s*Reference files.*$", re.MULTILINE | re.IGNORECASE),
    "criteria": re.compile(
        r"^#{1,4}\s*Acceptance criteria\s*$", re.MULTILINE | re.IGNORECASE
    ),
    "context": re.compile(r"^#{1,4}\s*Context.*$", re.MULTILINE | re.IGNORECASE),
}

# "**Kind:** bug" — a ticket the loop must reproduce before it may fix it. Only
# written when it is not the ordinary kind, so a plan a human wrote by hand
# does not have to say "feature" on every section.
_KIND = re.compile(r"^\*\*Kind:\*\*\s*(?P<kind>bug|feature)", re.MULTILINE | re.IGNORECASE)

_ROUTE = re.compile(r"^\*\*Route:\*\*\s*(?P<route>delegate|claude-only)", re.MULTILINE | re.IGNORECASE)

# "**Needs:** TT-003, TT-004" — the tickets that must land before this one.
_NEEDS = re.compile(r"^\*\*Needs:\*\*\s*(?P<needs>.+)$", re.MULTILINE | re.IGNORECASE)

PLANNER_SYSTEM = """You are the planner in a plan-and-execute pipeline.

You are given a specification written elsewhere. Convert it into an ordered
backlog of implementation tickets. You are not being asked to redesign the
work, question the goal, or add features — only to break what is written into
units an executor model can implement one at a time.

Rules:
- Each ticket must be independently verifiable and confined to a known set of
  files. Name the exact paths; guessing is worse than a slightly wider list.
- Acceptance criteria are assertions that would FAIL if the behavior were
  wrong. "Returns Err(ParseError) for input missing a closing brace" is a
  criterion. "Handles malformed input gracefully" is not.
- Write every instruction as the behavior you want, not the behavior you
  forbid. A local executor follows "reproduce the existing `shout` function
  exactly as it appears in the file, character for character" far more
  reliably than "do not modify `shout`" — a negative constraint describes
  everything except what to do, and the model has to infer the target from its
  absence. Reserve prohibitions for scope, where the allowed-files list
  enforces them mechanically rather than by persuasion.
- Mark a ticket "claude-only" when it touches authentication, authorization,
  secrets, concurrency, shared mutable state, migrations, public API surface,
  cryptography, or payment flows — or when the right approach is still an open
  question. Everything else is "delegate".
- Order tickets so that each one can assume the previous ones landed.
- The executor has no filesystem. It sees only what the ticket carries, and it
  returns whole files as text. Any file it must read to get an export name, a
  signature, an enum order, or a type right belongs in `reference_files` — it
  is pasted into the prompt read-only. A ticket that says "read src/api.rs"
  without listing it there is asking for something the executor cannot do, and
  it will guess instead.
- **Never paraphrase a table.** When the specification states a legend, an
  alphabet, an error-message list, a status-code mapping or any other lookup
  table, copy it into the ticket's spec verbatim — every row, spelled exactly
  as written. Summarising one is not compression, it is deletion: the executor
  cannot recover a row it was never shown, and a rule like "reject bad input
  with the exact error strings" names no strings at all. One specification
  listed eighteen legal characters and seven exact error messages; the ticket
  said "with exact error strings", and the implementation shipped four of the
  eighteen and invented every message.

Reply with a single JSON object and nothing else:

{"tickets": [{"id": "AB-001", "title": "...", "route": "delegate",
              "spec": "...", "allowed_files": ["..."],
              "reference_files": ["..."],
              "criteria": ["...", "..."], "context": ""}]}
"""


def looks_like_plan(text: str) -> bool:
    """True when the document already contains ticket-shaped sections."""
    return len(_TICKET_HEADER.findall(text)) >= 1 and bool(_FIELD["spec"].search(text))


def _section(body: str, start: re.Match[str] | None, next_starts: list[int]) -> str:
    if start is None:
        return ""
    begin = start.end()
    ends = [position for position in next_starts if position > begin]
    return body[begin : min(ends)].strip() if ends else body[begin:].strip()


def _unwrap_code_span(item: str) -> str:
    """Drop the backticks around a bullet that is nothing but a code span.

    A file path is usually written `` `src/piece.rs` `` and wants the backticks
    gone. A criterion usually *opens and closes* with code spans of its own —
    "`piece::cells(kind, rotation)` returns 4 offsets for every `kind` in
    `0..7`" — and stripping the outer character off each end takes the opening
    backtick of the first span and the closing backtick of the last, leaving
    unbalanced markdown in every prompt that renders it. Worse, it invites the
    planner to "reword" the criterion at respec time by repairing the
    punctuation, which the provenance check then reads as an attempt to change
    a criterion a human wrote.

    So the pair comes off only when there is exactly one span: no backtick
    survives between the two being removed.
    """
    if len(item) > 1 and item.startswith("`") and item.endswith("`"):
        inner = item[1:-1]
        if "`" not in inner:
            return inner.strip()
    return item


def _bullets(text: str) -> list[str]:
    items = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(("- ", "* ", "+ ")):
            items.append(_unwrap_code_span(line[2:].strip()))
    return items


def _ids(text: str) -> list[str]:
    """Ticket ids out of a comma- or space-separated list."""
    return [match.group(0) for match in re.finditer(r"[A-Z][A-Z0-9]*-\d+", text.upper())]


def _reaches(start: str, goal: str, edges: dict[str, list[str]]) -> bool:
    """Whether `goal` is already reachable from `start` along declared edges."""
    seen: set[str] = set()
    stack = [start]
    while stack:
        node = stack.pop()
        if node == goal:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(edges.get(node, ()))
    return False


def derive_needs(tickets: list[Ticket]) -> list[tuple[str, str, str]]:
    """Add ordering edges between tickets that write the same file.

    Two tickets sharing `src/lib.rs` is not a conflict to refuse — a ticket is
    a testable unit, and units share files. What they need is an order, so the
    later one sees the earlier one's work instead of racing it.

    Position order decides, because that is the sequence the plan's author
    already wrote the tickets in. A declared edge always wins: derivation adds
    an edge only where the graph does not already connect the pair in either
    direction, so it can never contradict a human or introduce a cycle into an
    acyclic graph.

    Returns the edges it added as `(ticket, needs, because_of_file)`, for
    reporting — an edge nobody typed should be visible before the run starts.
    """
    by_position = sorted(tickets, key=lambda t: (t.position, t.ticket_id))
    edges = {ticket.ticket_id: list(ticket.needs) for ticket in tickets}
    index = {ticket.ticket_id: ticket for ticket in tickets}

    writers: dict[str, list[str]] = {}
    for ticket in by_position:
        for path in ticket.allowed_files:
            writers.setdefault(normalize_path(path), []).append(ticket.ticket_id)

    added: list[tuple[str, str, str]] = []
    for path, owners in sorted(writers.items()):
        if len(owners) < 2:
            continue
        for earlier, later in zip(owners, owners[1:]):
            if _reaches(later, earlier, edges) or _reaches(earlier, later, edges):
                continue
            edges[later].append(earlier)
            index[later].needs = edges[later]
            added.append((later, earlier, path))
    return added


# Phrases that pin a whole file rather than assert something about it. Each is
# a claim a later ticket writing the same file must falsify in order to do its
# own job. The tester is already forbidden from writing assertions of this
# shape, for exactly this reason (see prompts.py) — this applies the same rule
# to the criteria the plan states, which is where the tester gets them from.
_WHOLE_FILE_CLAIM = re.compile(
    r"\b(exactly|only these|nothing else|no other|and no more|"
    r"must not (?:contain|declare|export|include)|"
    r"does not (?:contain|declare|export|include))\b",
    re.IGNORECASE,
)

# `src/lib.rs`, "src/lib.rs", src/lib.rs — a path mentioned inside a criterion.
_PATH_IN_TEXT = re.compile(r"[\w./\\-]+\.[A-Za-z0-9]+")


def shared_file_conflicts(tickets: list[Ticket]) -> list[str]:
    """Criteria that cannot survive another ticket writing the same file.

    Two tickets owning `src/lib.rs` is ordinary and now legal — they get an
    ordering edge. What is not legal is one of them asserting the file's whole
    contents, because the other's job is to add to it. Ordering does not save
    the pair: the first ticket passes, the second does its work, and the
    first's criterion is then false forever — verification is whole-project and
    permanent, so its own test fails every ticket that follows.

    Checked only for files more than one ticket writes. A sole owner may pin
    its file as tightly as it likes; nothing is coming to contradict it.
    """
    writers: dict[str, list[str]] = {}
    for ticket in tickets:
        for path in ticket.allowed_files:
            writers.setdefault(normalize_path(path), []).append(ticket.ticket_id)

    problems: list[str] = []
    for ticket_id, where, claim, path in whole_file_claims(tickets):
        others = [t for t in writers[path] if t != ticket_id]
        problems.append(
            f"{ticket_id} ({where}): {claim.strip()!r}\n"
            f"      {path} is also written by {', '.join(others)}, so a "
            f"whole-file claim about it cannot stay true.\n"
            f"      State what the file must declare, not what it must "
            f"contain and nothing more."
        )
    return list(dict.fromkeys(problems))


def whole_file_claims(tickets: list[Ticket]) -> list[tuple[str, str, str, str]]:
    """Every pinning claim about a file more than one ticket writes.

    Returns `(ticket_id, where, claim, path)`. Separate from the reporting
    above so a caller holding one revision can ask which of its criteria are
    the offending ones rather than parsing them back out of a message.
    """
    writers: dict[str, list[str]] = {}
    for ticket in tickets:
        for path in ticket.allowed_files:
            writers.setdefault(normalize_path(path), []).append(ticket.ticket_id)
    shared = {path for path, owners in writers.items() if len(owners) > 1}
    if not shared:
        return []

    found: list[tuple[str, str, str, str]] = []
    for ticket in tickets:
        for where, claim in _claims(ticket):
            if not _WHOLE_FILE_CLAIM.search(claim):
                continue
            named = {
                normalize_path(match.group(0))
                for match in _PATH_IN_TEXT.finditer(claim)
            }
            for path in sorted(named & shared):
                found.append((ticket.ticket_id, where, claim, path))
    return found


def _claims(ticket: Ticket) -> list[tuple[str, str]]:
    """Every self-contained statement the ticket makes, with where it came from.

    The spec is scanned as well as the criteria, and it is the half that
    matters: a plan states "src/lib.rs must end up containing exactly these
    three lines" in its prose, and the tester turns that into an assertion
    downstream. Checking only the criteria bullets misses the sentence the
    criteria were derived from.

    Split into sentences rather than searched whole, so a whole-file phrase in
    one paragraph and a path in another do not combine into a false report.
    """
    units: list[tuple[str, str]] = [("criterion", c) for c in ticket.criteria]
    for line in ticket.spec.splitlines():
        for sentence in re.split(r"(?<=[.:;])\s+", line):
            if sentence.strip():
                units.append(("spec", sentence))
    return units


# A heading — markdown or a bold line standing alone — that opens a run of
# decisions rather than requirements.
_DECISION_HEADING = re.compile(
    r"(?:design\s+)?decisions?\b|already\s+(?:made|decided|settled)|"
    r"do\s+not\s+revisit|non-?negotiable",
    re.IGNORECASE,
)

_HEADING_LINE = re.compile(r"^\s*(?:#{1,6}\s+\S|\*\*[^*]+\*\*\s*:?\s*$)")

# "Decision: the PRNG is xorshift32", "- **Decision:** ..." — a single line
# marked on its own, for a spec with no room for a section.
_DECISION_LINE = re.compile(r"^\s*(?:[-*+]\s*)?\**\s*decisions?\b\**\s*:", re.IGNORECASE)


def _sentences(line: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[.:;])\s+", line) if part.strip()]


def plan_decisions(text: str) -> list[str]:
    """Sentences the plan marked as decided rather than required.

    A plan can put load-bearing choices in prose — "randomness is a xorshift32
    seeded from JavaScript" — under a heading that says they are settled. The
    criteria ratchet does not cover them: they are not criteria, so respec may
    revise them away, and it did. One ticket's spec went from naming xorshift32
    to "an internal deterministic PRNG", an LCG shipped, every criterion passed
    and the reviewer was right to accept it. The decision a human wrote down
    was gone and nothing downstream could tell.

    Recognised two ways, because a plan may have room for a section or only for
    a line: everything under a heading about decisions, and any single line that
    marks itself. Anything else in the spec stays freely revisable — this
    protects what was labelled, not prose in general.
    """
    found: list[str] = []
    in_block = False
    for line in text.splitlines():
        if _DECISION_LINE.match(line):
            found.extend(_sentences(line))
            continue
        if _HEADING_LINE.match(line):
            in_block = bool(_DECISION_HEADING.search(line))
            continue
        if in_block and line.strip():
            found.extend(_sentences(line))
    return list(dict.fromkeys(found))


def graph_problems(tickets: list[Ticket]) -> list[str]:
    """Everything wrong with the dependency graph, in human terms.

    Checked at ingest because that is the last moment a fix is free. A
    dangling id or a cycle discovered mid-run costs a scheduler that cannot
    explain why nothing is eligible.
    """
    known = {ticket.ticket_id for ticket in tickets}
    problems: list[str] = []

    for ticket in tickets:
        for dep in ticket.needs:
            if dep == ticket.ticket_id:
                problems.append(f"{ticket.ticket_id} needs itself")
            elif dep not in known:
                problems.append(f"{ticket.ticket_id} needs {dep}, which is not in this backlog")

    # Depth-first cycle detection, reporting the path so the fix is obvious.
    edges = {t.ticket_id: [d for d in t.needs if d in known] for t in tickets}
    state: dict[str, int] = {}

    def walk(node: str, path: list[str]) -> None:
        state[node] = 1
        for dep in edges.get(node, ()):
            if state.get(dep) == 1:
                loop = path[path.index(dep) :] + [dep] if dep in path else [node, dep]
                problems.append("dependency cycle: " + " -> ".join(loop))
            elif state.get(dep, 0) == 0:
                walk(dep, path + [dep])
        state[node] = 2

    for ticket in tickets:
        if state.get(ticket.ticket_id, 0) == 0:
            walk(ticket.ticket_id, [ticket.ticket_id])

    # Same pair can surface from either end of a cycle; report each once.
    return list(dict.fromkeys(problems))


# Below this many tickets, "nothing depends on anything" carries no
# information: two or three new modules with no declared order is an ordinary
# small plan. The shape only becomes diagnosable once there are enough tickets
# that at least one real dependency is near-certain — and the run this exists
# for had fifteen.
_SHAPE_FLOOR = 4


def undeclared_order(root: Path, tickets: list[Ticket]) -> str:
    """Why this backlog looks like a file list rather than a plan, or "".

    A backlog where nothing depends on anything is either genuinely parallel or
    a planner that decomposed by file instead of by unit of work. Told apart by
    what the tickets are *doing*: a batch of independent fixes to files that
    already exist is ordinary and stays quiet, while a set of new modules being
    built together almost always has one that defines what the others use.

    That second shape is what shipped the defect. Fifteen tickets, fifteen
    files that did not exist, `needs: []` on every one of them. Nothing
    sequenced the shared type ahead of its consumers and no ticket owned it, so
    each module in turn reached for it, invented its own name for it — `types`,
    `geometry`, `model/rect`, `models/level_model` — and imported a file
    nothing would ever write.

    A warning rather than a refusal, and the reason is that the evidence is
    circumstantial. It is the one check in this family reasoning from the
    *absence* of something rather than the presence of it: genuinely parallel
    greenfield backlogs exist, they are just rare, and being told about a real
    one costs a reader five seconds.

    `derive_needs` has already run, so a shared writable file has been ordered
    and is not what this is about. What is left is the dependency nobody could
    see from the paths alone.
    """
    if len(tickets) < _SHAPE_FLOOR:
        return ""
    if any(ticket.needs for ticket in tickets):
        return ""

    concrete = [
        path
        for ticket in tickets
        for path in ticket.allowed_files
        if not any(character in path for character in "*?[")
    ]
    if not concrete:
        return ""
    new = [path for path in concrete if not (root / path).exists()]
    # Most of the work has to be greenfield. A backlog of independent fixes to
    # code that already exists is exactly the case this must not fire on.
    if len(new) * 2 <= len(concrete):
        return ""

    return (
        f"{len(tickets)} tickets, {len(new)} files that do not exist yet, and "
        f"not one `needs` between them. That is a plan in which nothing is "
        f"built on anything — possible, and rare.\n"
        f"  If any of these modules shares a type, a constant or a helper with "
        f"another, say which one writes it first. A backlog of this shape "
        f"once had every ticket reach for the same shared module, invent a "
        f"different name for it, and import a file no ticket was ever going "
        f"to create."
    )


def parse_plan(text: str) -> list[Ticket]:
    """Parse a document that already contains tickets.

    Unknown sections are ignored rather than rejected — a plan written for a
    human reader often carries notes and rationale the loop does not need.
    """
    headers = list(_TICKET_HEADER.finditer(text))
    tickets: list[Ticket] = []

    for index, header in enumerate(headers):
        start = header.end()
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        body = text[start:end]

        found = {key: pattern.search(body) for key, pattern in _FIELD.items()}
        boundaries = [match.start() for match in found.values() if match]

        route_match = _ROUTE.search(body)
        route = (route_match.group("route").lower() if route_match else "delegate")

        needs_match = _NEEDS.search(body)
        needs = _ids(needs_match.group("needs")) if needs_match else []

        kind_match = _KIND.search(body)
        kind = kind_match.group("kind").lower() if kind_match else TICKET_FEATURE

        tickets.append(
            Ticket(
                ticket_id=header.group("id"),
                title=header.group("title").strip(),
                route=route,
                kind=kind,
                position=index,
                spec=_section(body, found["spec"], boundaries),
                allowed_files=_bullets(_section(body, found["allowed"], boundaries)),
                reference_files=_bullets(
                    _section(body, found["reference"], boundaries)
                ),
                criteria=_bullets(_section(body, found["criteria"], boundaries)),
                needs=needs,
                context=_section(body, found["context"], boundaries),
            )
        )
    return tickets


def plan_with_model(
    provider: Provider, text: str, *, max_tokens: int | None = None
) -> list[Ticket]:
    """Have the planner role convert a freeform document into tickets."""
    # A whole backlog is the longest single reply the planner ever produces, so
    # give it everything the model can emit rather than a fixed guess.
    budget = max_tokens or max(8192, provider.capabilities().max_output_tokens)
    completion = provider.complete(
        [
            Message(role="system", content=PLANNER_SYSTEM),
            Message(role="user", content=f"Specification:\n\n{text}"),
        ],
        max_tokens=budget,
        temperature=0.1,
    )
    # Distinguish "the model wrote nonsense" from "the model was still writing",
    # which the JSON error below cannot tell apart on its own.
    if completion.truncated:
        raise ValueError(
            f"planner ran out of output room after {budget:,} tokens with "
            f"{len(completion.text):,} characters written; raise maxOutputTokens "
            f"for the planner model or split the specification"
        )
    return tickets_from_json(completion.text)


def tickets_from_json(text: str) -> list[Ticket]:
    """Parse the planner's JSON reply, tolerating a fenced block around it."""
    candidate = text.strip()
    fence = re.search(r"```(?:json)?\s*\n(.*?)\n```", candidate, re.DOTALL)
    if fence:
        candidate = fence.group(1)
    else:
        # Fall back to the outermost braces, for a reply with prose around it.
        first, last = candidate.find("{"), candidate.rfind("}")
        if first != -1 and last > first:
            candidate = candidate[first : last + 1]

    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ValueError(f"planner did not return usable JSON: {text[:400]}") from exc

    raw_tickets = data.get("tickets") if isinstance(data, dict) else data
    if not isinstance(raw_tickets, list) or not raw_tickets:
        raise ValueError("planner returned no tickets")

    tickets = []
    for position, item in enumerate(raw_tickets):
        tickets.append(
            Ticket(
                ticket_id=str(item.get("id") or f"T-{position + 1:03d}"),
                title=str(item.get("title", "")),
                route=str(item.get("route", "delegate")).lower(),
                position=position,
                spec=str(item.get("spec", "")),
                allowed_files=[str(p) for p in item.get("allowed_files", [])],
                reference_files=[str(p) for p in item.get("reference_files", [])],
                criteria=[str(c) for c in item.get("criteria", [])],
                needs=[str(n).strip() for n in item.get("needs", []) if str(n).strip()],
                context=str(item.get("context", "")),
            )
        )
    return tickets


def ingest(
    source: str,
    *,
    provider: Provider | None = None,
    force_plan: bool = False,
) -> tuple[list[Ticket], str, list[tuple[str, str, str]]]:
    """Convert a document into tickets. Returns (tickets, how, derived_edges).

    `how` names which path was taken so the caller can say so — a user who
    handed over a carefully written plan should be told whether it was used
    verbatim or re-planned.

    The dependency graph is completed and checked here, in the one place both
    paths converge. Ingest is the last moment a bad graph is free to fix: a
    cycle found mid-run costs a scheduler that can only report that nothing is
    eligible, without being able to say why.
    """
    tickets, how = _parse_or_plan(source, provider=provider, force_plan=force_plan)
    derived = derive_needs(tickets)

    problems = graph_problems(tickets)
    if problems:
        raise ValueError(
            "the backlog's dependencies do not resolve:\n  "
            + "\n  ".join(problems)
        )

    conflicts = shared_file_conflicts(tickets)
    if conflicts:
        raise ValueError(
            "these criteria cannot all hold at once:\n  "
            + "\n  ".join(conflicts)
        )
    return tickets, how, derived


def _parse_or_plan(
    source: str,
    *,
    provider: Provider | None = None,
    force_plan: bool = False,
) -> tuple[list[Ticket], str]:
    if not force_plan and looks_like_plan(source):
        tickets = parse_plan(source)
        if tickets:
            return tickets, "parsed"

    if provider is None:
        raise ValueError(
            "document is not in ticket form and no planner model was supplied. "
            "Either format it with '## ID: title' sections and a '### Spec' block, "
            "or run ingest with a configured planner role."
        )
    return plan_with_model(provider, source), "planned"


def render_ticket(ticket: Ticket) -> str:
    """Write a ticket back as reviewable markdown.

    Tickets stay as files, not just database rows: they are the artifact a
    human reads to decide whether the plan is right before the loop spends
    hours acting on it.
    """
    lines = [
        f"# {ticket.ticket_id}: {ticket.title}".rstrip(),
        "",
        f"**Route:** {ticket.route}",
    ]
    # Emitted only when it is not the ordinary kind. A bug ticket is read
    # differently by the loop — it has to reproduce the fault first — and a
    # ticket file that does not say so is a file that lies about what will
    # happen to it.
    if ticket.kind != TICKET_FEATURE:
        lines.append(f"**Kind:** {ticket.kind}")
    # Emitted whenever present, including when ingest derived it: an edge a
    # human never typed is exactly the one worth showing them.
    if ticket.needs:
        lines.append(f"**Needs:** {', '.join(ticket.needs)}")
    lines += [
        "",
        "## Spec",
        "",
        ticket.spec.strip() or "_(none)_",
        "",
        "## Allowed files",
        "",
    ]
    lines += [f"- `{path}`" for path in ticket.allowed_files] or ["_(none listed)_"]
    if ticket.reference_files:
        lines += ["", "## Reference files (read-only)", ""]
        lines += [f"- `{path}`" for path in ticket.reference_files]
    lines += ["", "## Acceptance criteria", ""]
    lines += [f"- {c}" for c in ticket.criteria] or ["_(none listed)_"]
    if ticket.context.strip():
        lines += ["", "## Context to pass through", "", ticket.context.strip()]
    return "\n".join(lines) + "\n"


def write_tickets(directory: Path, tickets: list[Ticket]) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for ticket in tickets:
        path = directory / f"{ticket.ticket_id}.md"
        path.write_text(render_ticket(ticket), encoding="utf-8")
        written.append(path)
    return written
