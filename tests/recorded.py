"""Replay a real run's step output against the loop, with the model scripted.

Two kinds of evidence have been available about this loop and each is blind
where the other sees. A live run produces real toolchain output and cannot be
made to fail on demand — eleven of them landed on the first attempt, so the
convergence machinery has never run outside a unit test. A unit test drives any
sequence you like and feeds the parser strings somebody typed, which is how a
whole `flake8` run came to parse as zero diagnostics with the suite green:
`signatures()` returned the empty set, both of its callers read that as *no
errors* rather than *cannot attribute*, and every fixture in the suite said
`src/a.ts(4,1): error TS2532: x`, which parses.

This is the pair of them. The model and the shell are scripted, so a ticket can
be made to fail as many times as a test wants; the failure *details* are lifted
verbatim from a recorded run by `scripts/harvest_recording.py`, so what the
parser sees is what a tool actually said.

Not a test module. `tests/test_recorded_output.py` is the one that uses it.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from forge.config import Config, LoopSettings
from forge.loop import Orchestrator
from forge.state import Store, Ticket

RECORDINGS = Path(__file__).resolve().parent / "recordings"


class Recording:
    """One run's captured steps, addressed by step id or by step name."""

    def __init__(self, name: str):
        self.name = name
        path = RECORDINGS / f"{name}.json"
        if not path.is_file():
            raise FileNotFoundError(f"no recording called {name!r} in {RECORDINGS}")
        loaded = json.loads(path.read_text(encoding="utf-8"))
        self.note: str = loaded.get("note", "")
        self.steps: list[dict] = loaded["steps"]

    def step(self, step_id: int) -> dict:
        for entry in self.steps:
            if entry["step"] == step_id:
                return entry
        raise KeyError(f"{self.name} has no step {step_id}")

    def detail(self, step_id: int) -> str:
        """The exact bytes the tool produced. Never reformatted."""
        return self.step(step_id)["detail"]

    def named(self, step_name: str) -> list[dict]:
        return [entry for entry in self.steps if entry["name"] == step_name]


def orchestrator(tickets=None, **loop_settings):
    """A real `Orchestrator` over a real `Store`, with nothing scripted yet.

    The model block points at a port nothing listens on: a test that forgets to
    replace `_call` should fail loudly rather than reach the network.
    """
    root = Path(tempfile.mkdtemp(prefix="forge-recorded-"))
    config = Config(
        root=root,
        models={
            "m": {
                "kind": "openai",
                "baseUrl": "http://127.0.0.1:1/v1",
                "model": "stub",
                "contextWindow": 8192,
                "maxOutputTokens": 1024,
            }
        },
        roles={role: "m" for role in ("planner", "executor", "tester", "reviewer")},
        commands={"lint": "", "typecheck": "", "test": ""},
        loop=LoopSettings(
            **{
                "respec_on_retry": False,
                "preflight": False,
                "retry_cycles": -1,
                **loop_settings,
            }
        ),
    )
    store = Store(config.db_path)
    run_id = store.create_run("goal")
    store.add_tickets(
        run_id, tickets or [Ticket("T-1", status="failed", attempts=3)]
    )
    return Orchestrator(config, store), store, run_id


def fail(store, run_id: int, detail: str, *, ticket_id="T-1", name="lint") -> None:
    """Record one failed step, with the recorded output as its detail.

    `_convergence` reads the step log rather than anything held in memory, so
    writing the step is the whole of what a cycle's failures are.
    """
    step = store.start_step(run_id, ticket_id, name)
    store.end_step(step, "failed", detail)


def cycle(orchestra, store, run_id: int, *details: str, ticket_id="T-1") -> str:
    """One cycle's failures, then the measurement, returning its verdict.

    The ticket is put back to `failed` afterwards because `_retry_cycle` is not
    what is under test here: the tests using this drive the measurement, and a
    requeued ticket that stays `pending` would leave the next cycle with
    nothing to measure.
    """
    for detail in details:
        fail(store, run_id, detail, ticket_id=ticket_id)
    ticket = next(t for t in store.list_tickets(run_id) if t.ticket_id == ticket_id)
    verdict = orchestra._measure_cycle(run_id, ticket)
    ticket.status = "failed"
    store.update_ticket(run_id, ticket)
    return verdict


def classes(store, run_id: int, ticket_id="T-1") -> list[str]:
    """What the ticket's last measured cycle recorded, in order."""
    ticket = next(t for t in store.list_tickets(run_id) if t.ticket_id == ticket_id)
    return sorted(ticket.cycle_classes)
