"""Model calls a host asks for that are not a delegation: review, objections, offload.

Each goes through the orchestrator's own `_call` — so the budget gate, rate
limits, usage accounting and provider recovery apply exactly as in a run —
and each is recorded as a step a host can read back with `forge_inspect`.

- `review`: an independent read of a diff against a contract. For work the
  host did itself, so it is never the only reader of what it wrote.
- `objections`: the sign-off question, asked of the delegate seats, with the
  answers returned as advice. Nothing is parked on them; that was the failure
  the lock ticket showed (docs/CREATIVE-TOOLS.md).
- `offload`: bulk text work with no files written — summaries, fixture data,
  drafts — optionally held to a JSON schema.
"""

from __future__ import annotations

import json
import re
from typing import Any

from . import ratify as ratification
from .config import Config, ConfigError
from .engine import DELEGATE_ROLES, HostOrchestrator, servers_for
from .failures import clip
from .patch import is_safe_path
from .prompts import parse_verdict, review_prompt
from .providers import Completion, Message, ProviderError
from .respec import sources_for
from .state import Store, Ticket

_STEP_LIMIT = 20_000
_FILE_LIMIT = 24_000
_FENCE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)


def _runner(config: Config, store: Store, job_id: int) -> HostOrchestrator:
    # The managed servers, as a delegation starts them. A ServerError is left
    # to the job, which reports it without a traceback.
    return HostOrchestrator(config, store, job_id, servers=servers_for(config, images=False))


# ----------------------------------------------------------------------
# review
# ----------------------------------------------------------------------


def review(config: Config, store: Store, run_id: int, ticket: Ticket, job_id: int) -> dict:
    """The reviewer seat's verdict on the ticket's scope as it stands, against HEAD."""
    runner = _runner(config, store, job_id)
    head = runner._git("rev-parse", "HEAD^{tree}")
    since = head.stdout.strip() if head.returncode == 0 else ""
    diff = runner._diff(since, ticket.allowed_files)
    if not diff.strip():
        return {"status": "nothing to review",
                "reason": "the ticket's files do not differ from HEAD"}
    messages = review_prompt(
        ticket,
        diff,
        toolchain=runner._toolchain_for(ticket),
        repository_map=runner._repo_map(),
        can_read=False,
    )
    step_id = store.start_step(run_id, ticket.ticket_id, "host-review")
    try:
        completion = runner._call(
            run_id, "reviewer", messages,
            max_tokens=runner._output_budget("reviewer"), temperature=0.0,
        )
    except ProviderError as exc:
        store.end_step(step_id, "failed", str(exc))
        return {"status": "unavailable", "reason": str(exc)}
    if completion.truncated:
        # An approval is the absence of REJECT; a cut-off verdict must not be one.
        store.end_step(step_id, "failed", clip(completion.text, _STEP_LIMIT))
        return {"status": "incomplete", "verdict": "reject",
                "reason": "the reviewer hit its output limit before finishing"}
    approved, verdict = parse_verdict(completion.text)
    store.end_step(step_id, "ok" if approved else "failed", clip(verdict, _STEP_LIMIT))
    return {
        "status": "reviewed",
        "verdict": "accept" if approved else "reject",
        "seat": config.roles.get("reviewer", ""),
        "reasons": clip(verdict, 3000),
        "step_id": step_id,
    }


# ----------------------------------------------------------------------
# objections
# ----------------------------------------------------------------------


def objections(
    config: Config, store: Store, run_id: int, ticket: Ticket, job_id: int
) -> dict:
    """Ask each delegate seat whether it can do its part as written."""
    runner = _runner(config, store, job_id)
    sources = sources_for(config.root, ticket)

    replies: dict[str, list[str]] = {}

    def call(
        role: str, messages: list[Message], max_tokens: int, *, thinking: bool = True
    ) -> Completion:
        completion = runner._call(run_id, role, messages, max_tokens=max_tokens,
                                  temperature=0.0, thinking=thinking)
        replies.setdefault(role, []).append(completion.text)
        return completion

    answers = []
    for role in DELEGATE_ROLES:
        vote = ratification._vote(
            store, run_id, ticket, role,
            call=call,
            budget=runner._output_budget(role),
            sources=sources,
            retrieved="",
            notes=(),
            digest="",
        )
        answer: dict[str, Any] = {
            "seat": role,
            "model": config.roles.get(role, ""),
            "signed": vote.signed,
        }
        if vote.blocking:
            answer["objections"] = list(vote.blocking)
        if vote.suggestions:
            answer["suggestions"] = list(vote.suggestions)
        if vote.error:
            answer["error"] = vote.error
        # What the seat actually said, every reply, so an objection — or an
        # answer that could not be read — can be checked rather than weighed.
        said = "\n\n--- asked again ---\n\n".join(replies.get(role, []))
        if not said.strip() and not vote.signed and not vote.error:
            # Nothing to read is not an objection to weigh; say which it was.
            answer["objections"] = [
                f"the seat returned an empty reply {len(replies.get(role, []))} "
                f"time(s); this is no objection to the contract, but a seat that "
                f"answers nothing is worth watching"
            ]
        step_id = store.start_step(run_id, ticket.ticket_id, f"host-objection-{role}")
        store.end_step(step_id, "ok" if vote.signed else "failed", clip(said, _STEP_LIMIT))
        answer["step_id"] = step_id
        answers.append(answer)
    return {
        "status": "asked",
        "ticket": ticket.ticket_id,
        "answers": answers,
        "note": "Advice, not a vote: check each objection against the tree "
        "before acting on it.",
    }


# ----------------------------------------------------------------------
# offload
# ----------------------------------------------------------------------


def offload(
    config: Config,
    store: Store,
    run_id: int,
    job_id: int,
    *,
    prompt: str,
    seat: str = "executor",
    files: list[str] | None = None,
    schema: dict[str, Any] | None = None,
    max_tokens: int = 0,
) -> dict:
    """One bulk generation by a delegate model; nothing is written to disk."""
    runner = _runner(config, store, job_id)
    role, model = _seat(config, seat)

    parts = [prompt.strip()]
    for path in files or []:
        if not is_safe_path(config.root, path):
            raise ConfigError(f"{path} is outside the project")
        try:
            text = (config.root / path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ConfigError(f"cannot read {path}: {exc}") from exc
        parts.append(f"--- {path} ---\n{clip(text, _FILE_LIMIT)}")
    if schema is not None:
        parts.append(
            "Reply with one JSON value that satisfies this JSON schema, in a "
            "```json fenced block, and nothing else:\n" + json.dumps(schema)
        )
    messages = [
        Message("system", "You are a careful assistant doing one bounded task. "
                "Answer exactly what is asked."),
        Message("user", "\n\n".join(parts)),
    ]
    budget = max_tokens or min(8192, runner._output_budget(role))
    step_id = store.start_step(run_id, "", "host-offload")

    completion = runner._call(run_id, role, messages, max_tokens=budget,
                              temperature=0.2, model=model)
    result: dict[str, Any] = {"seat": model or config.roles.get(role, ""),
                              "step_id": step_id}
    if schema is None:
        store.end_step(step_id, "ok", clip(completion.text, _STEP_LIMIT))
        result["text"] = completion.text
        result["truncated"] = completion.truncated
        return result

    value, problems = _read_json(completion.text, schema)
    if problems:
        # Asked once more with the exact complaint. Recover-before-refuse has
        # already run inside `_read_json`.
        retry = [*messages, Message("assistant", completion.text),
                 Message("user", "That reply does not satisfy the schema:\n- "
                         + "\n- ".join(problems) + "\nReply again with the JSON only.")]
        completion = runner._call(run_id, role, retry, max_tokens=budget,
                                  temperature=0.0, model=model)
        value, problems = _read_json(completion.text, schema)
    store.end_step(step_id, "failed" if problems else "ok", clip(completion.text, _STEP_LIMIT))
    if problems:
        result["problems"] = problems
        result["text"] = clip(completion.text, 4000)
    else:
        result["value"] = value
    return result


def _seat(config: Config, seat: str) -> tuple[str, str]:
    """`(role, model)` for a seat name or a model name."""
    if seat in DELEGATE_ROLES:
        return seat, ""
    if seat in config.models:
        return "executor", seat
    raise ConfigError(
        f"no seat or model {seat!r} (seats: {', '.join(DELEGATE_ROLES)}; "
        f"models: {', '.join(sorted(config.models))})"
    )


def _read_json(text: str, schema: dict[str, Any]) -> tuple[Any, list[str]]:
    """Parse the reply's JSON and check it against `schema`.

    Recover the content before refusing the format (LOOP-INVARIANTS section 7):
    a fenced block, then the whole reply, then the outermost bracketed span.
    """
    candidates = [m.group(1) for m in _FENCE.finditer(text)] + [text]
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if 0 <= start < end:
            candidates.append(text[start:end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        return value, validate(value, schema)
    return None, ["the reply contains no parseable JSON"]


def validate(value: Any, schema: dict[str, Any], where: str = "$") -> list[str]:
    """A small JSON Schema subset: type, enum, required, properties, items.

    Enough to hold bulk output to a shape. Anything it does not understand it
    does not check, rather than refusing a reply over a keyword it cannot read.
    """
    problems: list[str] = []
    expected = schema.get("type")
    types: dict[str, type | tuple[type, ...]] = {
        "object": dict, "array": list, "string": str, "boolean": bool,
        "integer": int, "number": (int, float), "null": type(None),
    }
    if isinstance(expected, str) and expected in types:
        ok = isinstance(value, types[expected])
        if expected in ("integer", "number") and isinstance(value, bool):
            ok = False
        if not ok:
            return [f"{where} should be {expected}"]
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{where} should be one of {schema['enum']}")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                problems.append(f"{where}.{key} is required")
        for key, sub in (schema.get("properties") or {}).items():
            if key in value and isinstance(sub, dict):
                problems += validate(value[key], sub, f"{where}.{key}")
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            problems += validate(item, schema["items"], f"{where}[{index}]")
    return problems[:20]
