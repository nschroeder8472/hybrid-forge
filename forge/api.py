"""The tool surface: what a host model calls, as plain functions.

See docs/FORGE-AS-A-TOOL.md. The MCP server (`forge.mcp`) and the CLI mirror
(`forge tool`) are both thin over this module, so every operation here can be
tested without a model and without a transport.

Every function returns a JSON-serialisable dict sized for a host's context:
distilled failures rather than raw logs, with ids and paths to drill into
through `inspect`. A host's context is the expensive resource this project
exists to spend carefully, so nothing here returns a whole step's output unless
asked for that step by id.

Nothing in this module decides anything. Judgement is the host's; the gates
the loop enforces are enforced here exactly as they are in a run, by calling
the same code.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Callable

from . import contract as contract_rules
from . import engine, jobs, routes
from .config import Config, ConfigError
from .contract import ContractError
from .failures import (
    distill,
    environment_failure,
    files_blamed,
    reported_test_count,
    signatures,
)
from .state import (
    RUN_HOST,
    TICKET_DONE,
    TICKET_FAILED,
    TICKET_PENDING,
    TICKET_SKIPPED,
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

# `path/to/file.ext:42` — the shape an argument from the tree has to take.
_CITES_LOCATION = re.compile(r"[\w./\\-]+\.\w+:\d+")


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
        # A job whose process died says so the next time anyone opens the
        # project, and its ticket becomes workable again.
        jobs.reap(self.store)

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
        return {"roles": roles, "models": models}

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
    # contract
    # ------------------------------------------------------------------

    def contract(
        self,
        id: str = "",
        spec_file: str = "",
        reason: str = "",
        **fields: Any,
    ) -> dict[str, Any]:
        """Open a contract, or revise one.

        - `spec_file`: open every ticket in a ticket-shaped document, verbatim.
          Its words are a person's, so its numbers and criteria never move.
        - `id` naming an existing ticket in the host run, with `reason`:
          revise it. The ratchets in `forge.contract.revise` apply.
        - otherwise: open one ticket from the fields given.

        Returns the contract as the delegates will see it, including the
        widened reading scope, and any warnings. Refusals are raised.
        """
        run_id = self._host_run()
        backlog = self.store.list_tickets(run_id)
        existing = next((t for t in backlog if t.ticket_id == id), None) if id else None

        try:
            if existing is not None:
                if spec_file:
                    raise ContractError(["revise with fields, not with `spec_file`"])
                ticket, warnings = contract_rules.revise(
                    self.config, self.store, run_id, existing, fields, reason, backlog
                )
                return {"revised": self._contract_view(run_id, ticket), "warnings": warnings}

            if spec_file:
                if fields or id:
                    raise ContractError(["`spec_file` opens tickets on its own; drop the fields"])
                path = (self.config.root / spec_file).resolve()
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError as exc:
                    raise ContractError([f"cannot read {spec_file}: {exc}"]) from exc
                tickets = contract_rules.tickets_from_document(text)
                author = contract_rules.HUMAN
            else:
                if reason:
                    raise ContractError([f"no ticket {id!r} to revise; omit `reason` to open it"])
                tickets = contract_rules.tickets_from_fields({"id": id, **fields})
                author = contract_rules.HOST

            for ticket in tickets:
                contract_rules.widen_reading(self.config, ticket)
            refusals, warnings = contract_rules.check(self.config, backlog, tickets)
            if refusals:
                raise ContractError(refusals)
        except ContractError as exc:
            raise ApiError("contract refused:\n- " + "\n- ".join(exc.problems)) from exc

        for ticket in tickets:
            ticket.position = self.store.next_position(run_id)
            self.store.add_tickets(run_id, [ticket])
            contract_rules.log_change(
                self.store, run_id, ticket.ticket_id, author, "created", "",
                ticket.spec, spec_file or "opened by the host",
            )
        return {
            "opened": [self._contract_view(run_id, t) for t in tickets],
            "warnings": warnings,
        }

    def _contract_view(self, run_id: int, ticket: Ticket) -> dict[str, Any]:
        return {
            "id": ticket.ticket_id,
            "title": ticket.title,
            "kind": ticket.kind,
            "route": ticket.route,
            "status": ticket.status,
            "author": contract_rules.author_of(self.store, run_id, ticket.ticket_id),
            "spec": ticket.spec,
            "criteria": list(ticket.criteria),
            "allowed_files": list(ticket.allowed_files),
            "reference_files": list(ticket.reference_files),
            "needs": list(ticket.needs),
        }

    # ------------------------------------------------------------------
    # delegate / job
    # ------------------------------------------------------------------

    def delegate(
        self,
        id: str,
        attempts: int = 3,
        seats: dict[str, str] | None = None,
        wait: float | None = 0,
    ) -> dict[str, Any]:
        """Work one ticket with the delegate seats, in the background.

        Returns the job at once, or its result if it finishes within `wait`
        seconds. `wait=None` waits for as long as it takes, which is what a
        shell caller wants: its process cannot outlive the call anyway.
        """
        run_id = self._host_run()
        ticket = self._find_ticket(run_id, id)
        if routes.is_withheld(ticket.route):
            raise ApiError(
                f"{id} is {routes.describe(ticket.route)}: a person, or you, "
                f"implements it. Do the work, then forge_close with "
                f"outcome: discharge"
            )
        if ticket.status == TICKET_DONE:
            raise ApiError(f"{id} is done")
        running = jobs.running_for(self.store, run_id, id)
        if running is not None:
            raise ApiError(f"{id} is already being worked by job {running}")
        unmet = self.store.unmet_needs(run_id, ticket)
        if unmet:
            raise ApiError(f"{id} needs {', '.join(unmet)} to land first")
        try:
            engine.configure(self.config, attempts, ticket, dict(seats or {}))
        except ConfigError as exc:
            raise ApiError(str(exc)) from exc
        if ticket.status != TICKET_PENDING:
            # A host re-delegating a failed or blocked ticket is a retry: the
            # spent attempts roll into the base so artifacts do not collide.
            (ticket,) = self.store.reset_tickets(run_id, [id])

        job_id = jobs.create(self.store, run_id, id)
        root = self.config.root
        settings = (attempts, dict(seats or {}))

        def work(handle: Forge) -> dict[str, Any]:
            fresh = handle._find_ticket(run_id, id)
            engine.configure(handle.config, settings[0], fresh, settings[1])
            return engine.delegate(handle.config, handle.store, run_id, fresh, job_id)

        jobs.start(lambda: Forge(root), job_id, work)
        return self.job(job_id, wait=wait)

    def job(self, job: int, wait: float | None = 0, cancel: bool = False) -> dict[str, Any]:
        """A job's progress or result. `cancel` stops it after the current step."""
        if cancel:
            jobs.request_cancel(self.store, int(job))
        found = jobs.wait(self.store, int(job), 10**9 if wait is None else float(wait or 0))
        if found is None:
            raise ApiError(f"no job {job}")
        if found["state"] == jobs.RUNNING:
            found["progress"] = self._progress(found["ticket"])
        return found

    def _progress(self, ticket_id: str) -> dict[str, Any]:
        run_id = self._host_run()
        row = self.store._connection.execute(
            "SELECT name, status FROM steps WHERE run_id = ? AND ticket_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (run_id, ticket_id),
        ).fetchone()
        ticket = self._find_ticket(run_id, ticket_id)
        return {
            "attempt": ticket.attempts,
            "step": f"{row['name']} ({row['status']})" if row else "starting",
        }

    # ------------------------------------------------------------------
    # close
    # ------------------------------------------------------------------

    def close_ticket(
        self,
        id: str,
        outcome: str = "",
        argument: str = "",
        against: str = "",
        commit: bool = False,
    ) -> dict[str, Any]:
        """Settle a ticket: land, abandon, overrule, or discharge."""
        run_id = self._host_run()
        ticket = self._find_ticket(run_id, id)
        if jobs.running_for(self.store, run_id, id) is not None:
            raise ApiError(f"{id} is being worked; cancel its job first")
        handlers: dict[str, Callable[..., dict[str, Any]]] = {
            "land": self._land,
            "abandon": self._abandon,
            "overrule": self._overrule,
            "discharge": self._discharge,
        }
        if outcome not in handlers:
            raise ApiError(f"outcome must be one of {', '.join(handlers)}")
        return handlers[outcome](run_id, ticket, argument=argument, against=against,
                                 commit=commit)

    def _land(self, run_id: int, ticket: Ticket, *, commit: bool, **_: Any) -> dict[str, Any]:
        if ticket.status != TICKET_DONE:
            raise ApiError(
                f"{ticket.ticket_id} is {ticket.status}, not done. Only a ticket "
                f"that passed verification and review lands; see `overrule` for a "
                f"review you can answer from the tree"
            )
        result: dict[str, Any] = {"id": ticket.ticket_id, "status": "landed"}
        if commit and not self.config.loop.auto_commit:
            runner = self._runner()
            runner._commit(run_id, ticket)
            step = self.store._connection.execute(
                "SELECT id, status, detail FROM steps WHERE run_id = ? AND ticket_id = ? "
                "AND name = 'commit' ORDER BY id DESC LIMIT 1",
                (run_id, ticket.ticket_id),
            ).fetchone()
            result["commit"] = {"ok": step["status"] == "ok", "detail": _clip(step["detail"], 600)}
        self.store.log(run_id, f"{ticket.ticket_id}: landed by the host.", kind="ticket")
        return result

    def _abandon(self, run_id: int, ticket: Ticket, *, argument: str, **_: Any) -> dict[str, Any]:
        if ticket.status == TICKET_DONE:
            raise ApiError(
                f"{ticket.ticket_id} is done; its work is in the tree. Revert it "
                f"with git if it should not be"
            )
        ticket.status = TICKET_SKIPPED
        ticket.blocked_note = f"abandoned by the host: {argument}".strip().rstrip(":")
        self.store.update_ticket(run_id, ticket)
        contract_rules.log_change(self.store, run_id, ticket.ticket_id, contract_rules.HOST,
                                  "abandoned", "", "", argument)
        return {"id": ticket.ticket_id, "status": "abandoned",
                "salvage": str(self._abandoned(run_id, ticket))}

    def _overrule(self, run_id: int, ticket: Ticket, *, argument: str, **_: Any) -> dict[str, Any]:
        """Accept a ticket the reviewer rejected, on an argument from the tree.

        Held to the shape invariant 3 holds a GRANT to: the argument must name
        the file and line that answers the objection, or it is refused. And
        verification is not overruled — the work is put back and re-verified,
        and anything red refuses the overrule and takes the work out again.
        """
        if ticket.status != TICKET_FAILED:
            raise ApiError(
                f"{ticket.ticket_id} is {ticket.status}; only a failed ticket is overruled"
            )
        last_review = self.store._connection.execute(
            "SELECT status, detail FROM steps WHERE run_id = ? AND ticket_id = ? "
            "AND name = 'review' ORDER BY id DESC LIMIT 1",
            (run_id, ticket.ticket_id),
        ).fetchone()
        if last_review is None or last_review["status"] == "ok":
            raise ApiError(
                f"{ticket.ticket_id} did not fail on review; there is no verdict "
                f"to overrule. Verification is never overruled"
            )
        if not _CITES_LOCATION.search(argument or ""):
            raise ApiError(
                "an overrule must cite the file and line that answers the "
                "reviewer (path:line). An argument that names no evidence is "
                "recorded as nothing"
            )
        salvage = self._abandoned(run_id, ticket)
        restored = self._restore(salvage)
        if not restored:
            raise ApiError(f"nothing of {ticket.ticket_id}'s work was set aside to restore")
        before = {path: data for path, data in restored.items()}
        result = self.verify(ticket=ticket.ticket_id)
        if not result["ok"]:
            for path, data in before.items():
                target = self.config.root / path
                if data is None:
                    target.unlink(missing_ok=True)
                else:
                    target.write_bytes(data)
            raise ApiError(
                "overrule refused: the restored work does not verify now\n"
                + json.dumps([s for s in result["steps"] if not s["ok"]], indent=1)[:4000]
            )
        ticket.status = TICKET_DONE
        ticket.blocked_note = ""
        self.store.update_ticket(run_id, ticket)
        contract_rules.log_change(
            self.store, run_id, ticket.ticket_id, contract_rules.HOST, "overrule",
            distill(last_review["detail"] or "", limit=2000), argument,
            "host overruled the reviewer",
        )
        self.store.log(run_id, f"{ticket.ticket_id}: review overruled by the host.",
                       level="warn", kind="ticket", data={"argument": argument})
        return {"id": ticket.ticket_id, "status": "landed", "restored": sorted(restored),
                "overrules": self._overrule_count(run_id)}

    def _discharge(self, run_id: int, ticket: Ticket, *, against: str, argument: str,
                   **_: Any) -> dict[str, Any]:
        """Close a ticket the host implemented itself.

        Verified the way the host's own edits always are: against a baseline it
        captured before starting, with nothing introduced.
        """
        if ticket.status == TICKET_DONE:
            raise ApiError(f"{ticket.ticket_id} is already done")
        if not against:
            raise ApiError(
                "discharge needs `against`: the baseline label you captured with "
                "forge_verify before you started the work"
            )
        result = self.verify(against=against, ticket=ticket.ticket_id)
        if not result["clean"]:
            raise ApiError(
                "discharge refused: your work introduced failures\n"
                + json.dumps([s for s in result["steps"] if not s["ok"]], indent=1)[:4000]
            )
        ticket.status = TICKET_DONE
        ticket.blocked_note = "implemented by the host"
        self.store.update_ticket(run_id, ticket)
        contract_rules.log_change(self.store, run_id, ticket.ticket_id, contract_rules.HOST,
                                  "discharged", "", argument, f"verified against {against}")
        return {"id": ticket.ticket_id, "status": "discharged", "verify": result["steps"]}

    def _abandoned(self, run_id: int, ticket: Ticket) -> Path:
        return self._runner()._abandoned_dir(run_id, ticket)

    def _restore(self, salvage: Path) -> dict[str, bytes | None]:
        """Copy a ticket's set-aside files back. Returns what each held before."""
        if not salvage.is_dir():
            return {}
        previous: dict[str, bytes | None] = {}
        for source in sorted(p for p in salvage.rglob("*") if p.is_file()):
            relative = source.relative_to(salvage).as_posix()
            target = self.config.root / relative
            previous[relative] = target.read_bytes() if target.is_file() else None
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
        return previous

    def _overrule_count(self, run_id: int) -> int:
        row = self.store._connection.execute(
            "SELECT COUNT(*) AS n FROM contract_log WHERE run_id = ? AND field = 'overrule'",
            (run_id,),
        ).fetchone()
        return int(row["n"])

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


def _clip(text: str | None, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"... [{len(text) - limit} more characters]"
