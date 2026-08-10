"""The orchestrator — the daemon that owns the loop.

No model decides what happens next. This state machine does, and it reads its
next move from SQLite rather than from a conversation. That is what makes the
run survive a filled context window, a killed process, a rebooted host, or an
exhausted usage window: none of those were holding the plan.

Per ticket:

    BUILD    executor writes the implementation against the spec
    APPLY    edits land on disk, anything outside scope is rejected
    TESTS    tester encodes the ticket's criteria (never its own)
    VERIFY   lint / typecheck / test — tooling, before any model reviews
    REVIEW   reviewer reads the diff against the spec, not against "tests pass"
    RECORD   durable outcomes to project memory (opt-in, usually nothing)
    COMMIT   optional

A failure at VERIFY loops back to BUILD with the error output attached, up to
`maxAttempts`. A `BLOCKED:` from the executor never loops — an underspecified
spec does not improve by being asked again.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .artifacts import Artifacts
from .budget import BudgetGate, Wait
from .config import Config
from .memory import MemoryClient, MemoryRefused, MemoryUnavailable, ticket_query
from .patch import apply_edits, enforce_scope, matches_any, parse_output
from .providers import (
    Completion,
    ContextOverflow,
    Message,
    Provider,
    ProviderError,
    RateLimited,
)
from .prompts import (
    CONTEXT_HEADING,
    build_prompt,
    parse_record,
    parse_verdict,
    record_prompt,
    review_prompt,
    tests_prompt,
)
from .state import (
    RUN_BLOCKED,
    RUN_DONE,
    RUN_FAILED,
    RUN_PAUSED,
    RUN_RUNNING,
    RUN_STOPPED,
    RUN_WAITING_BUDGET,
    TICKET_BLOCKED,
    TICKET_DONE,
    TICKET_FAILED,
    TICKET_RUNNING,
    TICKET_SKIPPED,
    Store,
    Ticket,
)

# Consecutive memory failures before the loop stops trying for this run.
MEMORY_FAILURE_LIMIT = 3

CONTROL_KEY = "command"
CONTROL_RUN = "run"
CONTROL_PAUSE = "pause"
CONTROL_STOP = "stop"


@dataclass
class StepResult:
    ok: bool
    detail: str = ""
    blocked: bool = False


class Stopped(Exception):
    """Raised internally when the control channel asks the loop to stop."""


class Orchestrator:
    def __init__(self, config: Config, store: Store):
        self.config = config
        self.store = store
        self.gate = BudgetGate(store, config.rate_limit_policies())
        self.memory = MemoryClient.from_config(config.memory, room=config.room)
        self.started_at = time.time()
        # Bound to a run id in run(); until then nothing is recorded, which is
        # what a bare Orchestrator in a test should do.
        self.artifacts = Artifacts(config.config_dir, 0, enabled=False)
        # Set once a memory failure has been reported, so a server that is down
        # for a twenty-ticket run logs once rather than twenty times.
        self._memory_warned = False
        self._memory_failures = 0

    # ------------------------------------------------------------------
    # Project memory
    # ------------------------------------------------------------------

    def _retrieve_context(self, run_id: int, ticket: Ticket) -> str:
        """Fetch prior decisions relevant to this ticket.

        Always best-effort. A memory server that is down, slow, or exposing an
        unrecognized tool surface degrades the run to "no context" — it never
        ends it, because losing an overnight run over a memory outage would be
        a far worse failure than building without project history.
        """
        if self.memory is None:
            return ""

        query = ticket_query(ticket.title, ticket.spec, ticket.allowed_files)
        try:
            retrieved = self.memory.search(query)
        except MemoryUnavailable as exc:
            self._memory_failures += 1
            if not self._memory_warned:
                self._memory_warned = True
                self.store.log(
                    run_id,
                    f"Project memory unavailable; continuing without it. {exc}",
                    level="warn",
                    kind="memory",
                )
            # Give up on memory for the rest of the run rather than paying a
            # connection timeout on every remaining ticket. A run that must
            # choose between context and progress chooses progress.
            if self._memory_failures >= MEMORY_FAILURE_LIMIT:
                self.memory = None
                self.store.log(
                    run_id,
                    f"Disabling project memory for this run after "
                    f"{self._memory_failures} consecutive failures.",
                    level="warn",
                    kind="memory",
                )
            return ""

        self._memory_failures = 0
        if retrieved:
            self.store.log(
                run_id,
                f"{ticket.ticket_id}: retrieved project context "
                f"({len(retrieved)} chars).",
                kind="memory",
                data={"query": query[:500], "excerpt": retrieved[:1000]},
            )
        return retrieved

    def _record_outcome(
        self,
        run_id: int,
        ticket: Ticket,
        *,
        diff: str,
        review: str,
        corrections: str,
        retrieved: str,
    ) -> None:
        """Write a durable outcome to project memory, if there is one.

        Runs only after a ticket has passed verification and review — a
        conclusion drawn from unverified work is not a memory, it is a rumour
        that future tickets will read as fact.

        Most tickets should record nothing, and the recorder is told so. This
        step exists for the minority that settle a decision or produce a
        correction worth generalizing.
        """
        if self.memory is None or not self.memory.settings.write:
            return

        step_id = self.store.start_step(run_id, ticket.ticket_id, "record")
        try:
            completion = self._call(
                run_id,
                self.config.record_role,
                record_prompt(
                    ticket,
                    diff=diff,
                    review=review,
                    attempts=ticket.attempts,
                    corrections=corrections,
                    retrieved=retrieved,
                ),
                max_tokens=1024,
                temperature=0.0,
            )
        except ProviderError as exc:
            # Never fail a verified ticket over the memory step. The work is
            # already done and reviewed; losing the note is the smaller loss.
            self.store.end_step(step_id, "failed", str(exc))
            self.store.log(
                run_id,
                f"{ticket.ticket_id}: could not evaluate outcome for memory ({exc}).",
                level="warn",
                kind="memory",
            )
            return

        title, entry = parse_record(completion.text)
        if not entry:
            self.store.end_step(step_id, "ok", "nothing durable to record")
            return

        try:
            result = self.memory.remember(entry, title=title)
        except MemoryRefused as exc:
            # Loud on purpose: the usual cause is a credential appearing in the
            # diff, which is worth a human look even though the run continues.
            self.store.end_step(step_id, "failed", str(exc))
            self.store.log(
                run_id,
                f"{ticket.ticket_id}: REFUSED to write memory — {exc}",
                level="error",
                kind="memory",
            )
            return
        except MemoryUnavailable as exc:
            self.store.end_step(step_id, "failed", str(exc))
            self.store.log(
                run_id,
                f"{ticket.ticket_id}: could not write memory ({exc}).",
                level="warn",
                kind="memory",
            )
            return

        detail = "\n".join([result, "", f"TITLE: {title}", entry])
        self.store.end_step(step_id, "ok", detail)
        self.store.log(
            run_id,
            f"{ticket.ticket_id}: {result} — {title or entry[:60]}",
            kind="memory",
            data={"title": title, "entry": entry, "result": result},
        )

    # ------------------------------------------------------------------
    # Control channel
    # ------------------------------------------------------------------

    def _command(self) -> str:
        return self.store.get_control(CONTROL_KEY, CONTROL_RUN)

    def _honor_control(self, run_id: int) -> None:
        """Block while paused; raise when stopped.

        Checked between steps rather than mid-call, so a pause never leaves a
        half-applied patch on disk.
        """
        announced = False
        while True:
            command = self._command()
            if command == CONTROL_STOP:
                raise Stopped()
            if command != CONTROL_PAUSE:
                if announced:
                    self.store.set_run_status(run_id, RUN_RUNNING)
                    self.store.log(run_id, "Resumed.", kind="control")
                return
            if not announced:
                self.store.set_run_status(run_id, RUN_PAUSED)
                self.store.log(run_id, "Paused; waiting for resume.", kind="control")
                announced = True
            time.sleep(self.config.loop.poll_seconds)

    def _check_runtime(self, run_id: int) -> None:
        limit = self.config.loop.max_runtime_seconds
        if limit and (time.time() - self.started_at) > limit:
            self.store.log(
                run_id, f"Runtime cap of {limit}s reached; stopping.", kind="control"
            )
            raise Stopped()

    # ------------------------------------------------------------------
    # Model calls
    # ------------------------------------------------------------------

    def _call(
        self,
        run_id: int,
        role: str,
        messages: list[Message],
        *,
        max_tokens: int,
        temperature: float = 0.2,
    ) -> Completion:
        """One model call, with the budget gate wrapped around it.

        Waiting happens here rather than at the call site so every role gets
        the same treatment: a limited planner parks the run exactly like a
        limited executor does.
        """
        provider = self.config.provider_for(role)
        model_name = self.config.model_name_for(role)

        messages = self.gate.fit(
            provider,
            messages,
            max_output=max_tokens,
            # Only the context block is droppable, and it is identified by the
            # same constant that writes it — a literal here would silently stop
            # matching the day the heading is reworded.
            droppable=lambda m: m.role == "user" and m.content.startswith(CONTEXT_HEADING),
        )

        while True:
            self._honor_control(run_id)
            self._check_runtime(run_id)

            wait = self.gate.check_rate_limit(model_name)
            if wait is not None:
                self._sleep_for_window(run_id, wait)
                continue

            try:
                completion = provider.complete(
                    messages, max_tokens=max_tokens, temperature=temperature
                )
            except RateLimited as exc:
                reset_at = exc.reset_at or (time.time() + exc.seconds_remaining)
                self.gate.park(model_name, reset_at)
                self._sleep_for_window(
                    run_id,
                    Wait(
                        seconds=max(1.0, reset_at - time.time()),
                        reason=f"{model_name}: {exc}",
                    ),
                )
                continue

            self.gate.record(
                model_name, completion.usage.prompt_tokens, completion.usage.completion_tokens
            )
            if completion.truncated:
                self.store.log(
                    run_id,
                    f"{model_name} hit its output limit mid-response; "
                    "the result may be incomplete.",
                    level="warn",
                    kind="usage",
                )
            return completion

    def _sleep_for_window(self, run_id: int, wait: Wait) -> None:
        """Park the run until a usage window reopens.

        Deliberately not a failure: the dashboard shows WAITING_BUDGET with the
        reason and the reopen time, and the loop wakes up on its own. Sleeping
        in short slices keeps pause and stop responsive while parked.
        """
        self.store.set_run_status(run_id, RUN_WAITING_BUDGET, wait.reason)
        self.store.log(
            run_id,
            f"Waiting {int(wait.seconds)}s — {wait.reason}",
            level="warn",
            kind="budget",
            data={"until": wait.until, "reason": wait.reason},
        )
        deadline = wait.until
        while time.time() < deadline:
            if self._command() == CONTROL_STOP:
                raise Stopped()
            time.sleep(min(self.config.loop.poll_seconds, max(0.1, deadline - time.time())))
        self.store.set_run_status(run_id, RUN_RUNNING)

    # ------------------------------------------------------------------
    # Shell steps
    # ------------------------------------------------------------------

    def _shell(self, run_id: int, name: str, command: str) -> StepResult:
        if not command.strip():
            return StepResult(ok=True, detail=f"no {name} command configured; skipped")

        step_id = self.store.start_step(run_id, "", name)
        try:
            result = subprocess.run(  # noqa: S602 - user-authored command from their own config
                command,
                shell=True,
                cwd=self.config.root,
                capture_output=True,
                text=True,
                # A test suite that prints a non-ASCII character must not crash
                # the daemon decoding its own verify step. `replace` keeps the
                # output readable enough to feed back to the executor, which is
                # all this text is for.
                encoding="utf-8",
                errors="replace",
                timeout=1800,
                check=False,
            )
        except subprocess.TimeoutExpired:
            self.store.end_step(step_id, "failed", f"{name} timed out after 1800s")
            return StepResult(ok=False, detail=f"{name} timed out")

        output = f"{result.stdout}\n{result.stderr}".strip()
        ok = result.returncode == 0
        self.store.end_step(step_id, "ok" if ok else "failed", output)
        return StepResult(ok=ok, detail=output)

    # ------------------------------------------------------------------
    # The loop
    # ------------------------------------------------------------------

    def run(self, run_id: int) -> str:
        """Drive a run to a terminal state. Returns that state."""
        self.store.set_control(CONTROL_KEY, CONTROL_RUN)
        self.store.set_run_status(run_id, RUN_RUNNING)
        self.store.log(run_id, "Loop started.", kind="lifecycle")

        self.artifacts = Artifacts(self.config.config_dir, run_id)
        if self.artifacts.failure:
            # Reported, not raised: losing the record is not losing the work.
            self.store.log(
                run_id,
                f"Step artifacts disabled — {self.artifacts.failure}",
                level="warn",
                kind="lifecycle",
            )

        try:
            while True:
                self._honor_control(run_id)
                self._check_runtime(run_id)

                ticket = self.store.next_ticket(run_id)
                if ticket is None:
                    return self._finish(run_id)

                self._work_ticket(run_id, ticket)

        except Stopped:
            self.store.set_run_status(run_id, RUN_STOPPED, "stopped by request")
            self.store.log(run_id, "Loop stopped.", level="warn", kind="lifecycle")
            return RUN_STOPPED
        except Exception as exc:  # noqa: BLE001 - the daemon must record why it died
            self.store.set_run_status(run_id, RUN_FAILED, str(exc))
            self.store.log(
                run_id, f"Loop failed: {exc}", level="error", kind="lifecycle"
            )
            return RUN_FAILED

    def _finish(self, run_id: int) -> str:
        counts = self.store.ticket_counts(run_id)
        blocked = counts.get(TICKET_BLOCKED, 0) + counts.get(TICKET_FAILED, 0)
        if blocked:
            self.store.set_run_status(
                run_id, RUN_BLOCKED, f"{blocked} ticket(s) need a human"
            )
            self.store.log(
                run_id,
                f"Backlog exhausted with {blocked} ticket(s) blocked.",
                level="warn",
                kind="lifecycle",
            )
            return RUN_BLOCKED

        self.store.set_run_status(run_id, RUN_DONE, "all tickets complete")
        self.store.log(run_id, "All tickets complete.", kind="lifecycle")
        return RUN_DONE

    def _work_ticket(self, run_id: int, ticket: Ticket) -> None:
        # Triage is a hard gate, not a preference. A claude-only ticket is one
        # the plan judged unsafe to hand to the executor, and the loop is not
        # entitled to overrule that because the backlog would otherwise stall.
        if ticket.route != "delegate":
            ticket.status = TICKET_SKIPPED
            ticket.blocked_note = "routed claude-only; implement this one directly"
            self.store.update_ticket(run_id, ticket)
            self.store.log(
                run_id,
                f"{ticket.ticket_id}: claude-only, left for a human.",
                level="warn",
                kind="ticket",
            )
            return

        offending = [p for p in ticket.allowed_files if matches_any(p, self.config.never_delegate)]
        if offending:
            ticket.status = TICKET_BLOCKED
            ticket.blocked_note = f"allowed files match neverDelegate: {', '.join(offending)}"
            self.store.update_ticket(run_id, ticket)
            self.store.log(
                run_id,
                f"{ticket.ticket_id}: blocked — {ticket.blocked_note}",
                level="warn",
                kind="ticket",
            )
            return

        ticket.status = TICKET_RUNNING
        self.store.update_ticket(run_id, ticket)
        self.store.log(run_id, f"{ticket.ticket_id}: starting.", kind="ticket")

        # Retrieved once per ticket: memory does not change between attempts,
        # and re-querying on every retry would spend calls for the same answer.
        retrieved = self._retrieve_context(run_id, ticket)
        failure_context = ""

        # The tree before this ticket touched anything, so review sees this
        # ticket's changes and not the uncommitted work of every ticket before
        # it. Taken per ticket rather than per attempt on purpose: a retry is
        # judged on everything the ticket has accumulated, which is what will
        # actually be committed.
        baseline = self._snapshot()

        while ticket.attempts < self.config.loop.max_attempts:
            ticket.attempts += 1
            self.store.update_ticket(run_id, ticket)

            outcome = self._attempt(run_id, ticket, failure_context, retrieved, baseline)

            if outcome.blocked:
                ticket.status = TICKET_BLOCKED
                ticket.blocked_note = outcome.detail
                self.store.update_ticket(run_id, ticket)
                self.store.log(
                    run_id,
                    f"{ticket.ticket_id}: BLOCKED — {outcome.detail[:400]}",
                    level="warn",
                    kind="ticket",
                )
                if self.config.loop.stop_on_blocked:
                    raise Stopped()
                return

            if outcome.ok:
                ticket.status = TICKET_DONE
                self.store.update_ticket(run_id, ticket)
                self.store.log(
                    run_id,
                    f"{ticket.ticket_id}: done in {ticket.attempts} attempt(s).",
                    kind="ticket",
                )
                return

            failure_context = outcome.detail
            self.store.log(
                run_id,
                f"{ticket.ticket_id}: attempt {ticket.attempts} failed; re-delegating.",
                level="warn",
                kind="ticket",
            )

        ticket.status = TICKET_FAILED
        ticket.blocked_note = f"exhausted {self.config.loop.max_attempts} attempts"
        self.store.update_ticket(run_id, ticket)
        self.store.log(
            run_id,
            f"{ticket.ticket_id}: gave up after {ticket.attempts} attempts.",
            level="error",
            kind="ticket",
        )
        if self.config.loop.stop_on_blocked:
            raise Stopped()

    def _attempt(
        self,
        run_id: int,
        ticket: Ticket,
        failure_context: str,
        retrieved: str = "",
        baseline: str = "",
    ) -> StepResult:
        # --- BUILD ---------------------------------------------------
        step_id = self.store.start_step(run_id, ticket.ticket_id, "build")
        try:
            completion = self._call(
                run_id,
                "executor",
                build_prompt(ticket, failure_context, retrieved),
                max_tokens=self._output_budget("executor"),
            )
        except ContextOverflow as exc:
            self.store.end_step(step_id, "failed", str(exc))
            return StepResult(ok=False, blocked=True, detail=str(exc))
        except ProviderError as exc:
            self.store.end_step(step_id, "failed", str(exc))
            self._record_step(ticket, "build", "failed", {"error": str(exc)})
            return StepResult(ok=False, detail=f"executor unavailable: {exc}")

        self._record_call(ticket, "build", "executor", completion)

        # A response cut off at the output limit parses cleanly — the fence the
        # model opened is simply never closed, or closes around half a
        # function. Applying it writes a file that is syntactically wrong for a
        # reason no reviewer would guess from the diff, so nothing is written
        # and the attempt is spent instead.
        if completion.truncated:
            detail = (
                "Your previous response was cut off at the output limit, so no "
                "files were written. Emit the same implementation in fewer "
                "output tokens — fewer files per response, no restated context "
                "— or reply BLOCKED: if the ticket cannot be implemented "
                "within that budget."
            )
            self.store.end_step(step_id, "failed", completion.text[:20000])
            return StepResult(ok=False, detail=detail)

        self.store.end_step(step_id, "ok", completion.text[:20000])

        parsed = parse_output(completion.text)
        if parsed.is_blocked:
            return StepResult(ok=False, blocked=True, detail=parsed.blocked_reason)
        if parsed.is_empty:
            return StepResult(ok=False, detail="executor returned no file edits")

        # --- APPLY ---------------------------------------------------
        scoped = enforce_scope(parsed, ticket.allowed_files, self.config.never_delegate)
        if scoped.rejected:
            self.store.log(
                run_id,
                f"{ticket.ticket_id}: rejected out-of-scope edits: "
                f"{'; '.join(scoped.rejected)}",
                level="warn",
                kind="scope",
            )
        if not scoped.edits:
            return StepResult(
                ok=False,
                detail="every edit fell outside the ticket's allowed files: "
                + "; ".join(scoped.rejected),
            )

        step_id = self.store.start_step(run_id, ticket.ticket_id, "apply")
        try:
            written = apply_edits(self.config.root, scoped.edits)
        except ValueError as exc:
            self.store.end_step(step_id, "failed", str(exc))
            return StepResult(ok=False, blocked=True, detail=str(exc))
        self.store.end_step(step_id, "ok", "\n".join(written))
        self._record_step(
            ticket,
            "apply",
            "ok",
            {"written": written, "rejected": scoped.rejected},
        )

        # --- TESTS ---------------------------------------------------
        # The criteria come from the ticket, never from the executor's own
        # suggestion. A model that writes both the code and the assertion it is
        # judged against will encode its bugs as passing tests.
        if ticket.criteria:
            step_id = self.store.start_step(run_id, ticket.ticket_id, "tests")
            try:
                completion = self._call(
                    run_id,
                    "tester",
                    tests_prompt(
                        ticket,
                        written,
                        test_command=self.config.commands.get("test", ""),
                        example_test=self._example_test(written),
                        # The executor already gets this. Without it here, a
                        # tester that wrote one wrong assertion rewrites the
                        # same one every attempt, and the ticket burns its
                        # retries asking the executor to fix code that was
                        # never broken — and that it cannot fix anyway, since
                        # the test file is outside its allowed scope.
                        failure_context=failure_context,
                    ),
                    max_tokens=self._output_budget("tester"),
                    temperature=0.1,
                )
                # Half a test file is worse than no test file: it fails verify
                # on a syntax error, which reads as the implementation being
                # broken. Discard it and let review check the criteria instead
                # — the same trade the handler below already makes.
                if completion.truncated:
                    raise ValueError("tester hit its output limit; partial tests discarded")
                test_parsed = enforce_scope(
                    parse_output(completion.text),
                    ticket.allowed_files + ["**/test_*.py", "**/*_test.*", "**/*.test.*", "tests/**"],
                    self.config.never_delegate,
                )
                if test_parsed.edits:
                    apply_edits(self.config.root, test_parsed.edits)
                self.store.end_step(
                    step_id, "ok", "\n".join(e.path for e in test_parsed.edits)
                )
                self._record_call(
                    ticket,
                    "tests",
                    "tester",
                    completion,
                    extra={
                        "written": [e.path for e in test_parsed.edits],
                        "rejected": test_parsed.rejected,
                    },
                )
            except (ProviderError, ValueError) as exc:
                # A missing test is a weaker result, not a failed ticket — the
                # criteria are still checked by review.
                self.store.end_step(step_id, "failed", str(exc))
                self._record_step(ticket, "tests", "failed", {"error": str(exc)})
                self.store.log(
                    run_id,
                    f"{ticket.ticket_id}: test authoring failed ({exc}); continuing to verify.",
                    level="warn",
                    kind="ticket",
                )

        # --- VERIFY --------------------------------------------------
        for name in ("lint", "typecheck", "test"):
            command = self.config.commands.get(name, "")
            result = self._shell(run_id, name, command)
            self._record_step(
                ticket,
                f"verify-{name}",
                "ok" if result.ok else "failed",
                {"command": command},
                raw=result.detail,
            )
            if not result.ok:
                return StepResult(
                    ok=False,
                    detail=f"{name} failed:\n{result.detail[-4000:]}",
                )

        # --- REVIEW --------------------------------------------------
        diff = self._diff(baseline)
        step_id = self.store.start_step(run_id, ticket.ticket_id, "review")
        try:
            completion = self._call(
                run_id,
                "reviewer",
                review_prompt(ticket, diff, retrieved),
                max_tokens=self._output_budget("reviewer"),
                temperature=0.0,
            )
        except ProviderError as exc:
            self.store.end_step(step_id, "failed", str(exc))
            return StepResult(ok=False, detail=f"reviewer unavailable: {exc}")

        # An approval is inferred from the absence of REJECT, so a verdict that
        # stops mid-sentence passes the ticket by default. Truncation must not
        # be the cheapest route to approval.
        if completion.truncated:
            self.store.end_step(step_id, "failed", completion.text[:20000])
            return StepResult(
                ok=False,
                detail="reviewer hit its output limit; the verdict was incomplete "
                "and was not treated as approval",
            )

        approved, verdict = parse_verdict(completion.text)
        self.store.end_step(step_id, "ok" if approved else "failed", verdict[:20000])
        self._record_call(
            ticket,
            "review",
            "reviewer",
            completion,
            status="ok" if approved else "failed",
            extra={"approved": approved},
        )

        if not approved:
            return StepResult(ok=False, detail=f"review rejected the diff:\n{verdict}")

        self.store.log(
            run_id,
            f"{ticket.ticket_id}: review passed.",
            kind="review",
            data={"verdict": verdict[:2000]},
        )

        # --- RECORD --------------------------------------------------
        # After review, never before: a conclusion drawn from unverified work
        # would be read back by future tickets as established fact.
        self._record_outcome(
            run_id,
            ticket,
            diff=diff,
            review=verdict,
            corrections=failure_context,
            retrieved=retrieved,
        )

        # --- COMMIT --------------------------------------------------
        if self.config.loop.auto_commit:
            self._commit(run_id, ticket)

        return StepResult(ok=True, detail=verdict)

    # ------------------------------------------------------------------

    def _record_step(
        self,
        ticket: Ticket,
        name: str,
        status: str,
        payload: dict[str, Any] | None = None,
        *,
        raw: str = "",
    ) -> None:
        self.artifacts.record(
            ticket.ticket_id,
            ticket.attempts,
            name,
            {"status": status, **(payload or {})},
            raw=raw,
        )

    def _record_call(
        self,
        ticket: Ticket,
        name: str,
        role: str,
        completion: Completion,
        *,
        status: str = "ok",
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Record a model call — cost, how it ended, and what it said.

        `finish_reason` and `truncated` are here rather than only in the log
        because "the model ran out of output budget" and "the model chose to
        stop" produce identical-looking files on disk, and telling them apart
        after the fact is most of diagnosing a bad ticket.
        """
        self._record_step(
            ticket,
            name,
            status,
            {
                "role": role,
                "model": completion.model or self.config.model_name_for(role),
                "finish_reason": completion.finish_reason,
                "truncated": completion.truncated,
                "usage": {
                    "prompt_tokens": completion.usage.prompt_tokens,
                    "completion_tokens": completion.usage.completion_tokens,
                    "estimated": completion.usage.estimated,
                },
                **(extra or {}),
            },
            raw=completion.text,
        )

    # Where tests live, in rough order of how conventional the location is.
    _TEST_GLOBS = ("tests/**/*", "test/**/*", "**/test_*.*", "**/*_test.*", "**/*.test.*")
    # Enough to establish framework, imports, and assertion style; not so much
    # that a large suite crowds the criteria out of the tester's window.
    _EXAMPLE_TEST_CHARS = 2000

    def _example_test(self, exclude: list[str]) -> tuple[str, str] | None:
        """An existing test file for the tester to imitate, if the repo has one.

        Framework is not a preference here, it is a hard constraint: a pytest
        file under `unittest discover` collects zero tests. One real example
        settles it more reliably than any instruction.

        Files this ticket just wrote are excluded — handing back the tester's
        own previous attempt would launder a wrong guess into a convention.
        """
        written = {p.replace("\\", "/") for p in exclude}
        for pattern in self._TEST_GLOBS:
            for path in sorted(self.config.root.glob(pattern)):
                if not path.is_file() or path.suffix in ("", ".pyc"):
                    continue
                relative = path.relative_to(self.config.root).as_posix()
                if relative in written:
                    continue
                try:
                    content = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if not content.strip():
                    continue
                return relative, content[: self._EXAMPLE_TEST_CHARS]
        return None

    def _output_budget(self, role: str) -> int:
        provider = self.config.provider_for(role)
        return provider.capabilities().max_output_tokens

    # A scratch index, so snapshotting never disturbs whatever the user has
    # staged. Inside .git/ so it is invisible to the working tree and to any
    # diff computed from it.
    _SNAPSHOT_INDEX = "forge-snapshot-index"

    def _git(self, *args: str, index: str = "") -> subprocess.CompletedProcess[str]:
        env = None
        if index:
            env = {**os.environ, "GIT_INDEX_FILE": index}
        return subprocess.run(
            ["git", *args],
            cwd=self.config.root,
            capture_output=True,
            text=True,
            # A diff of source code routinely carries non-ASCII. Decoding it
            # with the host locale would fail on exactly the tickets that touch
            # user-facing strings.
            encoding="utf-8",
            errors="replace",
            check=False,
            env=env,
        )

    def _snapshot(self) -> str:
        """Content hash of the working tree right now, as a git tree object.

        Written through a throwaway index so the user's staged changes are
        never touched, and recorded as a tree rather than a commit so nothing
        lands in history or on a ref. `git add -A` brings in untracked files —
        a ticket that creates a file must show up in the diff — and honours
        `.gitignore`, which is what keeps build output and this project's own
        artifacts out of what the reviewer reads.

        Returns "" when git is unavailable or the snapshot fails; callers fall
        back to the whole-tree diff rather than reviewing nothing.
        """
        index = str(self.config.root / ".git" / self._SNAPSHOT_INDEX)
        try:
            Path(index).unlink(missing_ok=True)
            if self._git("add", "-A", index=index).returncode != 0:
                return ""
            result = self._git("write-tree", index=index)
            return result.stdout.strip() if result.returncode == 0 else ""
        except (FileNotFoundError, OSError):
            return ""

    def _diff(self, since: str = "") -> str:
        """The changeset a reviewer should judge.

        Scoped to one ticket when `since` is a tree captured before it started.
        That distinction is load-bearing: `autoCommit` is off by default, so a
        verified ticket's work stays in the working tree, and a whole-tree diff
        shows every earlier ticket's changes to the reviewer of the current
        one. It then correctly rejects them as outside this ticket's allowed
        files — the executor is blamed for work it did not do, and a backlog
        fails from its second ticket onward.

        Anything the ticket did not touch appears in both trees and cancels
        out, which also keeps stale build output and leftovers from a failed
        earlier attempt out of the reviewer's view.
        """
        try:
            if since:
                current = self._snapshot()
                if current:
                    result = self._git("diff-tree", "-p", since, current)
                    if result.returncode == 0:
                        return result.stdout

            # No baseline, or git refused one: fall back to the whole tree.
            # `git add -N` registers new files with the index without staging
            # their contents, so a ticket that creates a file still appears.
            self._git("add", "-N", ".")
            return self._git("diff").stdout
        except FileNotFoundError:
            return "(git not available; diff unavailable)"

    def _commit(self, run_id: int, ticket: Ticket) -> None:
        message = f"{ticket.ticket_id}: {ticket.title}".strip().rstrip(":")
        step_id = self.store.start_step(run_id, ticket.ticket_id, "commit")
        result = subprocess.run(
            ["git", "commit", "-am", message],
            cwd=self.config.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        ok = result.returncode == 0
        self.store.end_step(
            step_id, "ok" if ok else "failed", f"{result.stdout}\n{result.stderr}".strip()
        )
