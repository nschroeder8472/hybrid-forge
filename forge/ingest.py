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

from .providers import Message, Provider
from .state import Ticket

# A ticket section: "## IM-014: Add PNG export" or "# IM-014 — Add PNG export".
_TICKET_HEADER = re.compile(
    r"^#{1,3}\s*(?P<id>[A-Z][A-Z0-9]*-\d+)\s*[:—\-]?\s*(?P<title>.*)$",
    re.MULTILINE,
)

_FIELD = {
    "spec": re.compile(r"^#{1,4}\s*Spec\s*$", re.MULTILINE | re.IGNORECASE),
    "allowed": re.compile(r"^#{1,4}\s*Allowed files\s*$", re.MULTILINE | re.IGNORECASE),
    "criteria": re.compile(
        r"^#{1,4}\s*Acceptance criteria\s*$", re.MULTILINE | re.IGNORECASE
    ),
    "context": re.compile(r"^#{1,4}\s*Context.*$", re.MULTILINE | re.IGNORECASE),
}

_ROUTE = re.compile(r"^\*\*Route:\*\*\s*(?P<route>delegate|claude-only)", re.MULTILINE | re.IGNORECASE)

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
- Mark a ticket "claude-only" when it touches authentication, authorization,
  secrets, concurrency, shared mutable state, migrations, public API surface,
  cryptography, or payment flows — or when the right approach is still an open
  question. Everything else is "delegate".
- Order tickets so that each one can assume the previous ones landed.

Reply with a single JSON object and nothing else:

{"tickets": [{"id": "AB-001", "title": "...", "route": "delegate",
              "spec": "...", "allowed_files": ["..."],
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


def _bullets(text: str) -> list[str]:
    items = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(("- ", "* ", "+ ")):
            items.append(line[2:].strip().strip("`"))
    return items


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

        tickets.append(
            Ticket(
                ticket_id=header.group("id"),
                title=header.group("title").strip(),
                route=route,
                position=index,
                spec=_section(body, found["spec"], boundaries),
                allowed_files=_bullets(_section(body, found["allowed"], boundaries)),
                criteria=_bullets(_section(body, found["criteria"], boundaries)),
                context=_section(body, found["context"], boundaries),
            )
        )
    return tickets


def plan_with_model(provider: Provider, text: str, *, max_tokens: int = 8192) -> list[Ticket]:
    """Have the planner role convert a freeform document into tickets."""
    completion = provider.complete(
        [
            Message(role="system", content=PLANNER_SYSTEM),
            Message(role="user", content=f"Specification:\n\n{text}"),
        ],
        max_tokens=max_tokens,
        temperature=0.1,
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
                criteria=[str(c) for c in item.get("criteria", [])],
                context=str(item.get("context", "")),
            )
        )
    return tickets


def ingest(
    source: str,
    *,
    provider: Provider | None = None,
    force_plan: bool = False,
) -> tuple[list[Ticket], str]:
    """Convert a document into tickets. Returns (tickets, how).

    `how` names which path was taken so the caller can say so — a user who
    handed over a carefully written plan should be told whether it was used
    verbatim or re-planned.
    """
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
        "",
        "## Spec",
        "",
        ticket.spec.strip() or "_(none)_",
        "",
        "## Allowed files",
        "",
    ]
    lines += [f"- `{path}`" for path in ticket.allowed_files] or ["_(none listed)_"]
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
