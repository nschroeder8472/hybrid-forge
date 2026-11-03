"""One ticket, worked by delegate seats, for a host. See docs/FORGE-AS-A-TOOL.md.

The attempt pipeline — BUILD, APPLY, TESTS, VERIFY, REVIEW, and every gate in
LOOP-INVARIANTS between them — is `Orchestrator._work_ticket`, unchanged. This
module runs it for a single ticket with the loop's own judgement switched off:

- no sign-off vote (`ratifyPasses: 0`): the host has `forge_objections`;
- no respec, split or retry cycle: those live in `run`'s retry machinery,
  which this never enters — the host decides between calls;
- no stop-the-run on a block: the host is told and decides.

What stays on is everything mechanical, including the parts that act without
asking: a `BLOCKED:` naming a file that exists is granted once (section 6),
and a ticket that gives up is quarantined (section 14).

The facade is the seam for the planned extraction of the pipeline out of
`Orchestrator`: callers depend on `delegate`, not on the class.
"""

from __future__ import annotations

import re
import time
from typing import Any

from .artifacts import Artifacts
from .config import Config, ConfigError
from .failures import distill, signatures
from .loop import CONTROL_RUN, CONTROL_STOP, Orchestrator, Stopped
from . import llama_server
from .run_servers import RunServers
from .servers import OWNER_CLI, ServerError
from .state import (
    TICKET_IMAGE,
    TICKET_BLOCKED,
    TICKET_DONE,
    TICKET_FAILED,
    TICKET_PENDING,
    TICKET_WITHHELD,
    Store,
    Ticket,
)

# The seats a delegation uses. The planner is the host now.
DELEGATE_ROLES = ("executor", "tester", "reviewer")

_CANARY_KEY = "host_canary:{run}"

_VERIFY_NAME = re.compile(r"^(lint|typecheck|test)(\[.*\])?$")


class HostOrchestrator(Orchestrator):
    """The orchestrator, stopped by its job rather than by the run's control row."""

    def __init__(
        self, config: Config, store: Store, job_id: int, servers: RunServers | None = None
    ):
        super().__init__(config, store, servers=servers)
        self.job_id = job_id

    def _command(self) -> str:
        # The project's control row belongs to autopilot; a host cancels one
        # job. Checked where `_honor_control` is — before each model call —
        # so a cancel never lands mid-patch.
        row = self.store._connection.execute(
            "SELECT cancel FROM jobs WHERE id = ?", (self.job_id,)
        ).fetchone()
        return CONTROL_STOP if row is not None and row["cancel"] else CONTROL_RUN

    def _preflight(self, run_id: int) -> list[str]:
        """Probe the delegate seats only; the host is not a seat."""
        checked: dict[str, str] = {}
        for role in DELEGATE_ROLES:
            try:
                provider = self.config.provider_for(role)
            except ConfigError:
                return [role]
            if provider.name not in checked:
                checked[provider.name] = provider.health()
        return [name for name, report in checked.items() if report.startswith("FAIL")]


def configure(config: Config, attempts: int, ticket: Ticket, seats: dict[str, str]) -> None:
    """Switch the loop's judgement off for one delegation, on this config copy."""
    config.loop.ratify_passes = 0
    config.loop.stop_on_blocked = False
    config.loop.max_attempts = ticket.attempts + max(1, attempts)
    for role, model in seats.items():
        if role not in DELEGATE_ROLES:
            raise ConfigError(f"seat {role!r} is not a delegate seat ({', '.join(DELEGATE_ROLES)})")
        if model not in config.models:
            raise ConfigError(
                f"no model {model!r} for the {role} seat (have: {', '.join(sorted(config.models))})"
            )
        config.roles[role] = model


def _seats_need_llama(config: Config) -> bool:
    """Whether any delegate seat is served by the `llama-server` forge runs.

    Asked per seat, not per declared model: the machine profile merges every
    model it knows into the config, and starting a server because some model
    on this machine could use it is how a test suite came to load a 25 GB
    checkpoint onto the card (tic-tac-toe run, 2026-11-02).
    """
    settings = llama_server.settings_for(config)
    if settings is None:
        return False
    for role in DELEGATE_ROLES:
        name = config.roles.get(role, "")
        if name in config.models and llama_server.is_managed(
            config.model_block(name), settings.port
        ):
            return True
    return False


def servers_for(config: Config, *, images: bool) -> RunServers | None:
    """Start the servers forge manages on this machine, if they are not up.

    The same servers `forge go` starts — `llama-server` when the machine
    profile opts in, ComfyUI when there is image work — adopted when already
    running, and owned as `cli` so they outlive this call. Raises
    `ServerError` when one cannot start.
    """
    local = RunServers.for_config(
        config, images=images, owner=OWNER_CLI, llama=_seats_need_llama(config)
    )
    if local is not None:
        local.start()
    return local


def delegate(config: Config, store: Store, run_id: int, ticket: Ticket, job_id: int) -> dict:
    """Work `ticket` and return what a host needs to decide its next move."""
    started = time.time()
    first_step = _last_step_id(store)
    try:
        local = servers_for(config, images=ticket.kind == TICKET_IMAGE)
    except ServerError as exc:
        return {"status": "halted", "reason": f"a server forge manages could not start: {exc}"}
    runner = HostOrchestrator(config, store, job_id, servers=local)
    runner.artifacts = Artifacts(config.config_dir, run_id)

    unreachable = runner._preflight(run_id)
    if unreachable:
        return {
            "status": "halted",
            "reason": f"delegate seat(s) did not answer: {', '.join(unreachable)}. "
            f"Nothing was spent. forge_status with probe: true says more.",
        }

    canary_key = _CANARY_KEY.format(run=run_id)
    if store.get_control(canary_key) != "ok":
        problems = runner._canary(run_id)
        if problems:
            return {
                "status": "halted",
                "reason": "the test command does not read the languages this "
                "project writes, so nothing a delegate writes would be checked: "
                + "; ".join(problems),
            }
        store.set_control(canary_key, "ok")

    try:
        runner._work_ticket(run_id, ticket)
    except Stopped:
        # The step the cancel interrupted never ended; close it, or the ledger
        # shows it running for ever (tic-tac-toe run, 2026-11-02, TTT-5).
        with store._write() as connection:
            connection.execute(
                "UPDATE steps SET status = 'cancelled', ended_at = ? WHERE run_id = ? "
                "AND ticket_id = ? AND status = 'running' AND id > ?",
                (time.time(), run_id, ticket.ticket_id, first_step),
            )
        ticket.status = TICKET_PENDING
        ticket.blocked_note = "cancelled by the host"
        store.update_ticket(run_id, ticket)
        store.log(run_id, f"{ticket.ticket_id}: cancelled by the host.", kind="ticket")
        return _result(runner, store, run_id, ticket, first_step, started, "cancelled")

    if runner._toolchain:
        step, command, reason = runner._toolchain
        ticket.status = TICKET_PENDING
        store.update_ticket(run_id, ticket)
        result = _result(runner, store, run_id, ticket, first_step, started, "halted")
        result["reason"] = (
            f"`{command}` ({step}) never reached the code: {reason}. Fix the "
            f"machine or the command; no ticket can fix this"
        )
        return result

    status = {
        TICKET_DONE: "landed",
        TICKET_FAILED: "failed",
        TICKET_BLOCKED: "halted" if runner._halt else "blocked",
        TICKET_WITHHELD: "withheld",
    }.get(ticket.status, ticket.status)
    return _result(runner, store, run_id, ticket, first_step, started, status)


def _result(
    runner: Orchestrator,
    store: Store,
    run_id: int,
    ticket: Ticket,
    first_step: int,
    started: float,
    status: str,
) -> dict[str, Any]:
    steps = store._connection.execute(
        "SELECT id, name, status, detail FROM steps WHERE run_id = ? AND id > ? "
        "AND ticket_id = ? ORDER BY id",
        (run_id, first_step, ticket.ticket_id),
    ).fetchall()

    result: dict[str, Any] = {
        "status": status,
        "ticket": ticket.ticket_id,
        "attempts": ticket.attempts,
    }
    if ticket.blocked_note and status != "landed":
        result["note"] = distill(ticket.blocked_note, limit=2000)

    diff = runner._diff(ticket.baseline_tree, ticket.allowed_files) if ticket.baseline_tree else ""
    if diff.strip():
        result["diff_stat"] = _diff_stat(diff)
        if runner.artifacts.enabled:
            directory = runner.artifacts._attempt_dir(ticket.ticket_id, ticket.attempt_number)
            try:
                directory.mkdir(parents=True, exist_ok=True)
                path = directory / "host-diff.patch"
                path.write_text(diff, encoding="utf-8")
                result["diff_path"] = str(path)
            except OSError:
                pass  # the diff is evidence, not the result

    # The newest outcome of each verify step, failures distilled.
    names = {row["name"] for row in steps}
    verify: dict[str, dict[str, Any]] = {}
    review: dict[str, Any] | None = None
    for row in steps:
        name = row["name"]
        if _VERIFY_NAME.match(name):
            entry: dict[str, Any] = {"step": name, "ok": row["status"] == "ok",
                                     "step_id": row["id"]}
            if row["status"] != "ok":
                entry["errors"] = sorted(signatures(row["detail"] or ""))[:20]
                entry["failure"] = distill(row["detail"] or "", limit=2000)
            verify[name] = entry
        elif name == "review":
            detail = (row["detail"] or "").strip()
            review = {
                "verdict": "accept" if row["status"] == "ok" else "reject",
                "seat": _review_seat(runner.config, ticket, names),
                "step_id": row["id"],
                "reasons": distill(detail, limit=1500),
            }
    if verify:
        result["verify"] = list(verify.values())
    if review is not None:
        result["review"] = review

    result["usage"] = _usage_since(store, started)
    result["artifacts"] = str(
        runner.config.config_dir / "artifacts" / f"run-{run_id}" / ticket.ticket_id
    )
    result["steps"] = [{"id": r["id"], "name": r["name"], "status": r["status"]}
                       for r in steps][-15:]
    return result


def _review_seat(config: Config, ticket: Ticket, step_names: set[str]) -> str:
    """Which model read the work. A rendering is read by `loop.viewModel`."""
    rendered = ticket.kind == TICKET_IMAGE or any(n.startswith("capture") for n in step_names)
    if rendered:
        seat = config.loop.view_role or "reviewer"
        return config.loop.view_model or config.roles.get(seat, "")
    return config.roles.get("reviewer", "")


def _diff_stat(diff: str) -> str:
    files: dict[str, list[int]] = {}
    current = ""
    for line in diff.splitlines():
        if line.startswith("+++ "):
            current = line[4:].removeprefix("b/")
            files.setdefault(current, [0, 0])
        elif line.startswith("--- ") or not current:
            continue
        elif line.startswith("+"):
            files[current][0] += 1
        elif line.startswith("-"):
            files[current][1] += 1
    added = sum(a for a, _ in files.values())
    removed = sum(r for _, r in files.values())
    names = ", ".join(sorted(files)[:8])
    return f"{len(files)} file(s), +{added} -{removed}: {names}"


def _usage_since(store: Store, since: float) -> dict[str, dict[str, Any]]:
    rows = store._connection.execute(
        "SELECT model, COUNT(*) AS calls, SUM(prompt_tokens) AS input, "
        "SUM(completion_tokens) AS output, SUM(cost_usd) AS cost "
        "FROM usage WHERE ts >= ? GROUP BY model",
        (since,),
    ).fetchall()
    return {
        r["model"]: {"calls": r["calls"], "in": r["input"] or 0, "out": r["output"] or 0,
                     **({"usd": round(r["cost"], 4)} if r["cost"] else {})}
        for r in rows
    }


def _last_step_id(store: Store) -> int:
    row = store._connection.execute("SELECT MAX(id) AS last FROM steps").fetchone()
    return int(row["last"] or 0)
