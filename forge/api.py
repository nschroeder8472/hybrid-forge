"""One project, opened for the smith's tools: status, the ledger, verification.

See docs/THE-FORGE.md. The tools in `forge.smith` (anvil, gauge, mold) and
the MCP server over them are thin over this module, so each can be tested
without a model and without a transport.

Every function returns a JSON-serialisable dict sized for a model's context:
distilled failures rather than raw logs, with step ids to drill into. Nothing
in this module decides anything; it measures and attributes, by calling the
same code the loop does.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .config import Config, ConfigError
from .failures import (
    distill,
    environment_failure,
    files_blamed,
    reported_test_count,
    signatures,
)
from .state import (
    RUN_DONE,
    RUN_FAILED,
    RUN_HOST,
    RUN_IDLE,
    Store,
    Ticket,
)

# Output caps. Generous enough that a host rarely needs a second call, small
# enough that a forgetful one cannot fill its window with a test log.
STEP_DETAIL_LIMIT = 20_000
FAILURE_LIMIT = 3_000
NOTE_LIMIT = 400

# Baselines a host captured, by label, live in the control table: one row each,
# `baseline:<label>` -> {"at", "steps": {name: [signature, ...]}, "counts"}.
_BASELINE_PREFIX = "baseline:"


class ApiError(Exception):
    """A request the tool refuses. The message is written for the host."""


class Forge:
    """One project, opened for tool calls."""

    def __init__(self, root: str | Path = "."):
        try:
            self.config = Config.load(str(root))
        except ConfigError as exc:
            raise ApiError(f"no usable forge config at {root}: {exc}") from exc
        self.store = Store(self.config.db_path)

    def close(self) -> None:
        self.store.close()

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------

    def status(self, probe: bool = False) -> dict[str, Any]:
        """The project at a glance: seats, verify plan, the current run, baselines.

        `probe` asks every configured model whether it answers, which costs a
        network round trip each and, for a local model, may cost a load. Off
        by default: the first call a host makes should be cheap.
        """
        from .ui import server as ui_server

        state = ui_server.snapshot(self.store, self.config)
        run = state["run"]
        result: dict[str, Any] = {
            "project": str(self.config.root),
            "seats": self._seats(probe),
            "verify": [
                {"step": step.name, "command": step.command,
                 "workspace": step.workspace.prefix or "."}
                for step in self._runner()._verify_plan()
            ],
            "baselines": sorted(self._baselines()),
            "run": None,
        }
        if run is not None:
            result["run"] = {
                "id": run["id"],
                "status": run["status"],
                "goal": _clip(run["goal"], NOTE_LIMIT),
                "note": _clip(run["note"], NOTE_LIMIT),
                "counts": state["counts"],
                "tickets": [
                    {
                        "id": t["id"],
                        "title": t["title"],
                        "status": t["status"],
                        "route": t["route"],
                        "attempts": t["attempts"],
                        **({"note": _clip(t["note"], NOTE_LIMIT)} if t["note"] else {}),
                    }
                    for t in state["tickets"]
                ],
                "control": state["control"],
            }
        result["usage"] = [
            {"model": row["model"], "calls": row["calls"], "tokens": row["display"]}
            for row in state["usage"]
        ]
        return result

    def _seats(self, probe: bool) -> dict[str, Any]:
        roles = dict(self.config.roles)
        models: dict[str, Any] = {}
        for name in sorted(self.config.models):
            block = self.config.model_block(name)
            entry: dict[str, Any] = {"kind": block.get("kind", "")}
            if block.get("model"):
                entry["model"] = block["model"]
            if probe:
                entry["health"] = self._probe(name, block)
            models[name] = entry
        images: dict[str, Any] = {}
        for name in sorted(self.config.image_models):
            block = self.config.image_model_block(name)
            images[name] = {
                "kind": block.get("kind", ""),
                "workflows": sorted((block.get("workflows") or {})),
            }
        seats: dict[str, Any] = {"roles": roles, "models": models}
        if images:
            seats["image_models"] = images
            loop = self.config.loop
            seats["image_seats"] = {
                "render": loop.image_model, "edit": loop.edit_model,
                "view": loop.view_model or roles.get(loop.view_role, ""),
            }
        return seats

    @staticmethod
    def _probe(name: str, block: dict[str, Any]) -> str:
        from .providers import build_provider

        try:
            return str(build_provider(name, block).health())
        except Exception as exc:  # noqa: BLE001 - a probe reports, it never raises
            return f"FAIL {exc}"

    # ------------------------------------------------------------------
    # inspect
    # ------------------------------------------------------------------

    def inspect(
        self,
        ticket: str = "",
        step: int = 0,
        run: int = 0,
        events: int = 0,
        limit: int = STEP_DETAIL_LIMIT,
    ) -> dict[str, Any]:
        """Read the ledger. Read-only.

        - `step`: one step's whole output (clipped at `limit`).
        - `ticket`: the ticket's contract, history and steps, without outputs.
        - `events`: the newest N events of the run.
        - nothing: the list of runs.

        `run` selects which run `ticket` and `events` read; default is the run
        `status` shows.
        """
        if step:
            return self._step(int(step), limit)
        if ticket and not run:
            host = self._existing_host_run()
            if host is not None and any(
                t.ticket_id == ticket for t in self.store.list_tickets(host)
            ):
                return self._ticket(host, ticket)
        run_id = int(run) or self._current_run_id()
        if ticket:
            return self._ticket(run_id, ticket)
        if events:
            return {"run": run_id, "events": self._events(run_id, int(events))}
        return {
            "runs": [
                {"id": r["id"], "status": r["status"], "goal": _clip(r["goal"], 120)}
                for r in self.store.list_runs()
            ]
        }

    def _current_run_id(self) -> int:
        from .ui import server as ui_server

        run = ui_server._live_run(self.store) or self.store.latest_run()
        if run is None:
            raise ApiError("there are no runs in this project yet")
        return int(run["id"])

    def _step(self, step_id: int, limit: int) -> dict[str, Any]:
        row = self.store._connection.execute(
            "SELECT * FROM steps WHERE id = ?", (step_id,)
        ).fetchone()
        if row is None:
            raise ApiError(f"no step {step_id}")
        detail = row["detail"] or ""
        return {
            "id": row["id"],
            "run": row["run_id"],
            "ticket": row["ticket_id"],
            "name": row["name"],
            "status": row["status"],
            "seconds": (
                round(row["ended_at"] - row["started_at"], 1)
                if row["ended_at"] else None
            ),
            "detail": _clip(detail, limit),
            "clipped": len(detail) > limit,
        }

    def _ticket(self, run_id: int, ticket_id: str) -> dict[str, Any]:
        found = self._find_ticket(run_id, ticket_id)
        steps = self.store._connection.execute(
            "SELECT id, name, status, started_at, ended_at FROM steps "
            "WHERE run_id = ? AND ticket_id = ? ORDER BY id",
            (run_id, ticket_id),
        ).fetchall()
        return {
            "run": run_id,
            "id": found.ticket_id,
            "title": found.title,
            "kind": found.kind,
            "status": found.status,
            "route": found.route,
            "attempts": found.attempts,
            "spec": found.spec,
            "criteria": list(found.criteria),
            "allowed_files": list(found.allowed_files),
            "reference_files": list(found.reference_files),
            "needs": list(found.needs),
            "note": found.blocked_note,
            "original_spec": found.original_spec if found.drifted else "",
            "original_criteria": (
                list(found.original_criteria) if found.drifted else []
            ),
            "human_notes": [n.get("text", "") for n in found.human_note],
            "steps": [
                {"id": s["id"], "name": s["name"], "status": s["status"]}
                for s in steps
            ],
            "artifacts": str(self._artifact_dir(run_id, ticket_id)),
        }

    def _find_ticket(self, run_id: int, ticket_id: str) -> Ticket:
        for ticket in self.store.list_tickets(run_id):
            if ticket.ticket_id == ticket_id:
                return ticket
        raise ApiError(f"run {run_id} has no ticket {ticket_id!r}")

    def _artifact_dir(self, run_id: int, ticket_id: str) -> Path:
        return self.config.config_dir / "artifacts" / f"run-{run_id}" / ticket_id

    def _events(self, run_id: int, count: int) -> list[dict[str, Any]]:
        rows = self.store._connection.execute(
            "SELECT * FROM events WHERE run_id = ? ORDER BY id DESC LIMIT ?",
            (run_id, max(1, min(count, 200))),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "level": r["level"],
                "kind": r["kind"],
                "message": _clip(r["message"], 1000),
            }
            for r in reversed(rows)
        ]

    # ------------------------------------------------------------------
    # verify
    # ------------------------------------------------------------------

    def verify(
        self,
        capture: str = "",
        against: str = "",
        ticket: str = "",
    ) -> dict[str, Any]:
        """Run the project's verify commands now, with attribution.

        - `capture`: record what is failing now under this label, so a later
          call can be compared against it. Take one before editing.
        - `against`: compare against a captured label. A failure whose
          signature was already in the baseline is reported as `already`;
          everything else is `introduced`. With no baseline, every failure is
          `introduced` — the safe direction to be wrong in.
        - `ticket`: narrow the plan to the build that ticket writes to, as the
          loop does for a ticket's own verify steps.

        This is how the standards reach work that never passed through a
        delegate: a host editing files itself is held to the same commands and
        the same attribution (LOOP-INVARIANTS §4, §5, §11, §13).
        """
        if capture and against:
            raise ApiError("pass `capture` or `against`, not both")
        baseline: dict[str, Any] = {}
        if against:
            baseline = self._baselines().get(against) or {}
            if not baseline:
                raise ApiError(
                    f"no baseline {against!r}; have: {sorted(self._baselines()) or 'none'}"
                )

        run_id = self._host_run()
        runner = self._runner()
        scoped = self._find_ticket(run_id, ticket) if ticket else None

        steps: list[dict[str, Any]] = []
        captured: dict[str, list[str]] = {}
        counts: dict[str, int] = {}
        for step in runner._verify_plan(scoped):
            result = runner._shell(
                run_id, f"host-{step.name}", step.command, workspace=step.workspace
            )
            found = signatures(result.detail)
            counted = reported_test_count(result.detail)
            if counted is not None:
                counts[step.name] = counted
            entry: dict[str, Any] = {
                "step": step.name,
                "ok": result.ok,
                "step_id": result.step_id,
            }
            if counted is not None:
                entry["tests"] = counted
            if not result.ok:
                captured[step.name] = sorted(found)
                already = set(baseline.get("steps", {}).get(step.name, []))
                introduced = found - already
                entry["introduced"] = sorted(introduced)[:30]
                entry["already"] = sorted(found & already)[:30]
                entry["blamed_files"] = sorted(
                    files_blamed(result.detail, exclude=found & already)
                )[:30]
                if not found:
                    # LOOP-INVARIANTS §13: no diagnostic and a launcher's own
                    # words means the command never reached the code.
                    reason = environment_failure(result.detail)
                    if reason:
                        entry["environment"] = reason
                    else:
                        entry["unattributed"] = True
                entry["failure"] = distill(result.detail, limit=FAILURE_LIMIT)
            steps.append(entry)

        if capture:
            self.store.set_control(
                _BASELINE_PREFIX + capture,
                json.dumps({"at": time.time(), "steps": captured, "counts": counts}),
            )

        introduced_any = any(
            s.get("introduced") or s.get("unattributed") or s.get("environment")
            for s in steps if not s["ok"]
        )
        return {
            "ok": all(s["ok"] for s in steps),
            "clean": not introduced_any,
            "captured": capture or None,
            "against": against or None,
            "steps": steps,
        }

    def _baselines(self) -> dict[str, dict[str, Any]]:
        rows = self.store._connection.execute(
            "SELECT key, value FROM control WHERE key LIKE ?",
            (_BASELINE_PREFIX + "%",),
        ).fetchall()
        found: dict[str, dict[str, Any]] = {}
        for row in rows:
            try:
                found[row["key"][len(_BASELINE_PREFIX):]] = json.loads(row["value"])
            except ValueError:
                continue
        return found

    # ------------------------------------------------------------------
    # plumbing
    # ------------------------------------------------------------------

    def _runner(self) -> Any:
        """The orchestrator's machinery without its providers.

        Built through `__new__`, as `cli._verify_tree` does: verification and
        planning the verify steps need the config and the store, and nothing
        here calls a model.
        """
        from .loop import Orchestrator

        runner = Orchestrator.__new__(Orchestrator)
        runner.config = self.config
        runner.store = self.store
        runner._toolchain = None
        return runner

    def _existing_host_run(self) -> int | None:
        recorded = self.store.get_control(HOST_RUN_KEY, "")
        if recorded.isdigit() and self.store.get_run(int(recorded)) is not None:
            return int(recorded)
        return None

    def _host_run(self) -> int:
        """The run a host's own steps are recorded against.

        Steps belong to a run. A host working outside any backlog still gets
        its verify output recorded, so `inspect(step=...)` can read it back and
        the dashboard shows it. One host run per project, reused.
        """
        existing = self._existing_host_run()
        if existing is not None:
            return existing
        run_id = self.store.create_run("host session", source="host")
        self.store.set_run_status(run_id, RUN_HOST)
        self.store.set_control(HOST_RUN_KEY, str(run_id))
        return run_id


HOST_RUN_KEY = "host_run"


def _reopen_run(store: Store, run_id: int, note: str) -> None:
    """Put a finished run back in `forge go`'s queue, as `forge retry` does.

    A run can finish `done` while holding a withheld ticket; requeueing that
    ticket without reopening the run leaves it where `resumable_runs` never
    looks (tic-tac-toe run, 2026-11-02, DOC-1).
    """
    run = store.get_run(run_id)
    if run is not None and run["status"] in (RUN_DONE, RUN_FAILED):
        store.set_run_status(run_id, RUN_IDLE, note=note)


def _clip(text: str | None, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"... [{len(text) - limit} more characters]"
