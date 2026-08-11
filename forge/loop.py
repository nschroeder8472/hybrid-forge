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
import re
import subprocess
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from .artifacts import Artifacts
from .budget import BudgetGate, Wait
from .config import Config
from .failures import distill, signatures
from .memory import MemoryClient, MemoryRefused, MemoryUnavailable, ticket_query
from .patch import (
    apply_edits,
    duplicate_paths,
    enforce_scope,
    is_safe_path,
    matches_any,
    normalize_path,
    parse_output,
)
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

            self.gate.record(model_name, completion.usage)
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

    # The verify steps, in the order a failure is cheapest to diagnose.
    _VERIFY_STEPS = ("lint", "typecheck", "test")

    def _baseline_failures(self, run_id: int, ticket: Ticket) -> dict[str, set[str]]:
        """Which verify steps were already failing before this ticket started.

        Verification is whole-project — `cargo clippy --all-targets`, `pytest`,
        `go test ./...` — so it reports every ticket's breakage to whichever
        ticket happens to run next. Without a baseline the loop hands the
        executor an error in a file its ticket does not list, tells it to fix
        the cause, and spends all three attempts on work it has no authority to
        do. Then respec reads the same error as evidence the *spec* is wrong and
        writes a criterion about somebody else's file, which makes the two
        tickets permanently contradict each other.

        Recording what was already broken is what breaks that chain: a failure
        present before the ticket ran is reported as pre-existing and does not
        count against it.
        """
        known: dict[str, set[str]] = {}
        for name in self._VERIFY_STEPS:
            command = self.config.commands.get(name, "")
            if not command.strip():
                continue
            result = self._shell(run_id, f"baseline-{name}", command)
            if result.ok:
                continue
            found = signatures(result.detail)
            if not found:
                # Unparseable output cannot be compared against anything later.
                # Leaving it out means the ticket is judged on this step
                # normally, which is the safe direction to be wrong in.
                continue
            known[name] = found
            self.store.log(
                run_id,
                f"{ticket.ticket_id}: {name} was already failing before this "
                f"ticket started ({len(found)} error(s)); it will not be "
                "blamed for them.",
                level="warn",
                kind="verify",
                data={"step": name, "signatures": sorted(found)[:20]},
            )
        return known

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

        # Every ticket passed, but a ticket passes on the errors *it* caused,
        # not on the state of the tree — a failure that pre-dated a ticket is
        # deliberately not counted against it, which is what stops one
        # abandoned file from failing an entire backlog. The cost of that is
        # that nobody owns a breakage nobody introduced, so the run has to
        # check for one itself rather than report a green backlog over a red
        # build.
        for name in self._VERIFY_STEPS:
            command = self.config.commands.get(name, "")
            result = self._shell(run_id, f"final-{name}", command)
            if result.ok:
                continue
            note = f"backlog complete but {name} still fails"
            self.store.set_run_status(run_id, RUN_BLOCKED, note)
            self.store.log(
                run_id,
                f"{note}. Nothing in the backlog introduced this, so no ticket "
                f"was blamed for it and no ticket had it in scope to fix:\n"
                f"{distill(result.detail, limit=2000)}",
                level="error",
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

        # Taken once per ticket, for the same reason as the snapshot: this is
        # the state the ticket inherited, and every attempt is judged against
        # it. Re-running it per attempt would also fold the ticket's own
        # half-finished work into what counts as "already broken".
        pre_existing = (
            self._baseline_failures(run_id, ticket)
            if self.config.loop.baseline_verify
            else {}
        )

        # Test files this ticket created, and whether it created them rather
        # than overwriting something that was already there. Unverified ones
        # are removed if the ticket never passes.
        authored: dict[str, bool] = {}
        # Everything that has already failed on this ticket, and every verdict
        # the reviewer has already given it. `failure_context` alone carries
        # only the newest one, which is what lets an executor oscillate — fix A
        # breaks B, fix B breaks A — for its whole retry budget without
        # anything noticing the cycle.
        history: list[str] = []
        rejections: list[str] = []

        while ticket.attempts < self.config.loop.max_attempts:
            ticket.attempts += 1
            self.store.update_ticket(run_id, ticket)

            outcome = self._attempt(
                run_id, ticket, failure_context, retrieved, baseline,
                pre_existing=pre_existing, authored=authored,
                prior_failures=history[-self._PRIOR_FAILURES:],
                rejections=rejections,
            )

            if outcome.blocked:
                self._discard_tests(run_id, ticket, authored)
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

            # Newest last, so the executor reads them in the order they
            # happened and the oldest is the first to fall off the window.
            history.append(f"Attempt {ticket.attempts}: {outcome.detail}")
            failure_context = outcome.detail
            self.store.log(
                run_id,
                f"{ticket.ticket_id}: attempt {ticket.attempts} failed; re-delegating.",
                level="warn",
                kind="ticket",
            )

        ticket.status = TICKET_FAILED
        self._discard_tests(run_id, ticket, authored)
        # Keep why it failed, not just that it did. "exhausted 3 attempts" is
        # the one fact already visible from the attempt count, and discarding
        # the last verification failure throws away the only thing that could
        # tell a human — or a respec — what the spec got wrong.
        summary = f"exhausted {self.config.loop.max_attempts} attempts"
        if failure_context:
            summary += f"; last failure: {distill(failure_context, limit=1500)}"
        ticket.blocked_note = summary
        self.store.update_ticket(run_id, ticket)
        self.store.log(
            run_id,
            f"{ticket.ticket_id}: gave up after {ticket.attempts} attempts.",
            level="error",
            kind="ticket",
        )
        if self.config.loop.stop_on_blocked:
            raise Stopped()

    # Per-file ceiling for READ-ONLY context. Big enough for a normal source
    # file, small enough that a vendored blob or a lockfile cannot eat the
    # context window the spec needs. Losing the tail of a reference file costs
    # accuracy; the model is not being asked to reproduce it.
    _SOURCE_LIMIT = 24_000
    # A file the ticket may WRITE is never truncated, at any size. The executor
    # returns whole files, and it is told to preserve every line it was not
    # asked to change — so showing it three quarters of a file and asking for
    # the complete one deletes the rest, with a successful apply, a plausible
    # diff, and nothing anywhere saying a quarter of the file just went away.
    #
    # Past this ceiling the file cannot be round-tripped by any model, so the
    # ticket blocks instead. Saying "split this ticket" is a worse outcome than
    # succeeding and a far better one than silent data loss.
    _WRITABLE_CEILING = 200_000
    # Earlier failures carried forward alongside the newest one. Two is enough
    # to expose an A-then-B-then-A cycle without crowding the spec out of the
    # window; the full history is in the step log either way.
    _PRIOR_FAILURES = 2

    def _scope_guidance(
        self, ticket: Ticket, rejected: list[str], *, total_loss: bool
    ) -> str:
        """Tell the executor what was dropped and what it can do about it.

        Scope is enforced mechanically and is not up for negotiation — a model
        must not be able to argue its way into writing a file the ticket did
        not authorize, which is the whole point of `neverDelegate`. But
        rejecting silently is what makes the loop unrecoverable: the executor
        cannot tell a dropped edit from one it never made, so it repeats it.

        Naming the paths, and naming `BLOCKED:` as the way to ask for scope,
        turns a dead end into something the planner can fix at respec time.
        """
        lines = [
            "Some of your edits were not written, because the ticket does not "
            "authorize those files:",
            "",
        ]
        lines += [f"- {entry}" for entry in rejected]
        lines += [
            "",
            "Files this ticket may write:",
        ]
        lines += [f"- {path}" for path in ticket.allowed_files] or ["- (none)"]
        lines += [
            "",
            "Scope is enforced before anything reaches disk, so re-sending the "
            "same file will be dropped again.",
        ]
        if any("neverDelegate" in entry for entry in rejected):
            lines.append(
                "A neverDelegate path is prohibited at the project level and "
                "will never be granted to this ticket. Implement what you can "
                "without it, or reply BLOCKED:."
            )
        lines.append(
            "If the ticket genuinely cannot be completed within that list, "
            "reply with a single line starting `BLOCKED:` naming the file you "
            "need and why. That reaches a human and can widen the ticket; "
            "silently working around it cannot."
        )
        if total_loss:
            lines.insert(0, "Nothing was written: every edit fell outside scope.\n")
        return "\n".join(lines)

    def _sources_for(
        self,
        ticket: Ticket,
        extra: list[str] | None = None,
        *,
        whole: Sequence[str] = (),
    ) -> tuple[dict[str, str], list[str]]:
        """Read the files a role needs to see. Never raises.

        Writable files are included so an edit preserves what it is not
        changing; reference files are included because neither the executor nor
        the tester has a filesystem, and both will otherwise invent the API
        they are working against. `extra` carries the files the executor just
        wrote, so the tester asserts against the real implementation rather
        than the one it imagines.

        `whole` names the paths this caller will ask the model to reproduce.
        Those are never abridged — see `_WRITABLE_CEILING`. Everything else is
        read-only context and is clipped at `_SOURCE_LIMIT`.

        Returns `(sources, oversized)`. `oversized` names files the caller must
        not proceed with: too large to reproduce, and too dangerous to show in
        part.
        """
        sources: dict[str, str] = {}
        oversized: list[str] = []
        must_be_whole = {normalize_path(path) for path in whole}
        wanted = list(ticket.allowed_files) + list(ticket.reference_files)
        wanted += list(extra or [])
        for path in wanted:
            # A glob in allowed_files is a scope rule, not a readable file.
            if any(ch in path for ch in "*?["):
                continue
            if not is_safe_path(self.config.root, path):
                continue
            candidate = (self.config.root / path).resolve()
            try:
                if not candidate.is_file():
                    continue
                text = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            if normalize_path(path) in must_be_whole:
                if len(text) > self._WRITABLE_CEILING:
                    oversized.append(f"{path} ({len(text):,} characters)")
                    continue
                sources[path] = text
                continue

            if len(text) > self._SOURCE_LIMIT:
                text = (
                    text[: self._SOURCE_LIMIT]
                    + f"\n[truncated at {self._SOURCE_LIMIT} characters — this "
                    "file is reference only, do not return it]\n"
                )
            sources[path] = text
        return sources, oversized

    def _attempt(
        self,
        run_id: int,
        ticket: Ticket,
        failure_context: str,
        retrieved: str = "",
        baseline: str = "",
        *,
        pre_existing: dict[str, set[str]] | None = None,
        authored: dict[str, bool] | None = None,
        prior_failures: Sequence[str] = (),
        rejections: list[str] | None = None,
    ) -> StepResult:
        # --- BUILD ---------------------------------------------------
        # The files this ticket may write must reach the executor whole; it is
        # about to send them back as whole files.
        sources, oversized = self._sources_for(ticket, whole=ticket.allowed_files)
        if oversized:
            detail = (
                "This ticket cannot be delegated as written. It authorises "
                "files too large to rewrite in full:\n"
                + "\n".join(f"- {entry}" for entry in oversized)
                + "\n\nThe executor returns whole files, so it would have to be "
                "shown a partial copy and asked to reproduce all of it — which "
                "deletes whatever it was not shown. Split the ticket so it "
                "writes smaller files, or narrow its scope."
            )
            self.store.log(
                run_id,
                f"{ticket.ticket_id}: blocked — {detail.splitlines()[0]}",
                level="warn",
                kind="ticket",
            )
            return StepResult(ok=False, blocked=True, detail=detail)

        step_id = self.store.start_step(run_id, ticket.ticket_id, "build")
        try:
            completion = self._call(
                run_id,
                "executor",
                build_prompt(
                    ticket,
                    failure_context,
                    retrieved,
                    sources,
                    prior_failures=prior_failures,
                ),
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

        # Two blocks for one path means the response did not parse into the
        # files it describes, and applying it would write the wrong one last.
        # Nothing goes to disk until the executor sends a coherent answer.
        repeated = duplicate_paths(parsed)
        if repeated:
            return StepResult(
                ok=False,
                detail=(
                    "Your response contained more than one block for the same "
                    "file, so nothing was written:\n"
                    + "\n".join(f"- {path}" for path in repeated)
                    + "\n\nThe usual cause is a fence, not a mistake in the "
                    "code. A file whose own contents contain ``` closes its "
                    "wrapping fence early, and the rest of that file is then "
                    "read as further files named after whatever paths appear "
                    "in its prose. Wrap any such file in a longer fence — four "
                    "backticks or five — and emit each file exactly once."
                ),
            )

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
                detail=self._scope_guidance(ticket, scoped.rejected, total_loss=True),
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
        # A partial rejection used to be logged and then dropped. The executor
        # saw a successful apply, lint failed on the piece that never landed,
        # and it had no way to connect the two — so it re-sent the same edit
        # every attempt. Carry the rejection forward as verification evidence.
        scope_note = (
            self._scope_guidance(ticket, scoped.rejected, total_loss=False)
            if scoped.rejected
            else ""
        )

        # --- TESTS ---------------------------------------------------
        # The criteria come from the ticket, never from the executor's own
        # suggestion. A model that writes both the code and the assertion it is
        # judged against will encode its bugs as passing tests.
        example = self._example_test(written)
        test_path, no_tests_because = self._test_target(ticket, written, example)
        if test_path:
            # On a retry the ticket's own test file is already on disk, and a
            # fixed path makes that the common case rather than the rare one.
            # Handing it back as "the convention this repo follows" would
            # launder the previous attempt's mistakes into a rule.
            example = self._example_test(written + [test_path])
        if ticket.criteria and no_tests_because:
            # Not a failure. The criteria are still checked at review, which is
            # the right place for "the build script takes a --release flag" or
            # "the page has a canvas element" — neither is something this
            # project's test command can collect, and forcing a file into it
            # only adds a target that later tickets have to keep green.
            self.store.log(
                run_id,
                f"{ticket.ticket_id}: no tests authored — {no_tests_because}. "
                "Review will check the criteria instead.",
                kind="ticket",
            )
        if ticket.criteria and test_path:
            step_id = self.store.start_step(run_id, ticket.ticket_id, "tests")
            existed = (self.config.root / test_path).exists()
            try:
                completion = self._call(
                    run_id,
                    "tester",
                    tests_prompt(
                        ticket,
                        written,
                        # One fixed path per ticket. A tester free to name its
                        # own file renames it on every retry and leaves the
                        # previous one running forever.
                        test_path=test_path,
                        test_command=self.config.commands.get("test", ""),
                        example_test=example,
                        # The executor already gets this. Without it here, a
                        # tester that wrote one wrong assertion rewrites the
                        # same one every attempt, and the ticket burns its
                        # retries asking the executor to fix code that was
                        # never broken — and that it cannot fix anyway, since
                        # the test file is outside its allowed scope.
                        failure_context=failure_context,
                        # The tester has no checkout either. Left to guess at
                        # the API it just wrote assertions for, it reaches for
                        # private fields and wrong argument types — which does
                        # not fail a test, it fails to compile, and then every
                        # later ticket's verify step fails on a file that has
                        # nothing to do with it. All read-only here: the tester
                        # writes its own file and returns none of these.
                        sources=self._sources_for(ticket, extra=written)[0],
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
                # Exactly one path, and not the ticket's source files either.
                # The previous allowlist was every test-shaped path in the
                # repository, which let one ticket's tester scatter six files
                # across three attempts and let it overwrite the very
                # implementation it was supposed to be judging.
                test_parsed = enforce_scope(
                    parse_output(completion.text),
                    [test_path],
                    self.config.never_delegate,
                )
                if test_parsed.rejected:
                    self.store.log(
                        run_id,
                        f"{ticket.ticket_id}: tester tried to write outside "
                        f"{test_path}: {'; '.join(test_parsed.rejected)}",
                        level="warn",
                        kind="scope",
                    )
                if test_parsed.edits:
                    apply_edits(self.config.root, test_parsed.edits)
                    if authored is not None:
                        # Only claim authorship of a file we brought into
                        # existence. Overwriting one that was already there is
                        # not a licence to delete it later.
                        authored.setdefault(test_path, not existed)
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
        inherited = pre_existing or {}
        for name in self._VERIFY_STEPS:
            command = self.config.commands.get(name, "")
            result = self._shell(run_id, name, command)
            already = inherited.get(name, set())
            introduced = signatures(result.detail) - already if already else set()
            self._record_step(
                ticket,
                f"verify-{name}",
                "ok" if result.ok else "failed",
                {
                    "command": command,
                    "pre_existing": sorted(already)[:20],
                    "introduced": sorted(introduced)[:20],
                },
                raw=result.detail,
            )
            if result.ok:
                continue

            # Everything this step is complaining about was already broken when
            # the ticket started, so it is not this ticket's to fix — and very
            # likely not in its scope to fix either. Passing the step is what
            # stops one abandoned file from failing an entire backlog.
            if already and not introduced:
                self.store.log(
                    run_id,
                    f"{ticket.ticket_id}: {name} still failing, but only on "
                    "errors that pre-date this ticket; not counted against it.",
                    level="warn",
                    kind="verify",
                    data={"step": name},
                )
                continue

            # Distilled, not tail-sliced: compilers lead with the error and
            # end with warnings and a summary, so the last 4k reliably kept
            # the noise and dropped the diagnosis.
            detail = f"{name} failed:\n{distill(result.detail)}"
            if already:
                # Without this the executor tries to fix all of it, including
                # the part it did not cause and cannot reach.
                detail += (
                    "\n\nSome errors in that output pre-date this ticket and "
                    "are not yours to fix — they are in files this ticket does "
                    "not own. Fix only what your change introduced:\n"
                    + "\n".join(f"- {s}" for s in sorted(introduced)[:10])
                )
            # The dropped edit is often the reason this step failed at all,
            # and it is invisible in the tool output. Attach it or the next
            # attempt cannot connect the missing symbol to the file that
            # never landed.
            if scope_note:
                detail += f"\n\n{scope_note}"
            return StepResult(ok=False, detail=detail)

        # --- REVIEW --------------------------------------------------
        diff = self._diff(baseline)
        # A ticket can pass verification having changed nothing — because the
        # work was already on disk, or because it rewrote a file byte for byte.
        # Handed `(empty diff)` and nothing else, a reviewer has no way to tell
        # that from "the files were never written", and it says so: one real
        # verdict read "No build.sh, build.ps1, README.md exist", about a repo
        # where all three did. Show it the state when there is no change.
        state: dict[str, str] = {}
        if not diff.strip():
            state = self._sources_for(ticket)[0]

        step_id = self.store.start_step(run_id, ticket.ticket_id, "review")
        try:
            completion = self._call(
                run_id,
                "reviewer",
                review_prompt(
                    ticket,
                    diff,
                    retrieved,
                    # Its own earlier rejections of this same ticket. Without
                    # them a reviewer can object to X, get X fixed, then object
                    # to Y it never raised — three attempts, three unrelated
                    # objections, and no signal that the spec was the problem.
                    prior_verdicts=list(rejections or []),
                    state=state,
                ),
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
            if rejections is not None:
                rejections.append(verdict)
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
            # Not `attempts`: that restarts at 1 on every retry cycle, which
            # would bury the failed cycle's artifacts under the new ones.
            ticket.attempt_number,
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
                    "cache_creation_tokens": completion.usage.cache_creation_tokens,
                    "cache_read_tokens": completion.usage.cache_read_tokens,
                    "total_tokens": completion.usage.total_tokens,
                    "cost_usd": completion.usage.cost_usd,
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
    # Directories whose contents are generated, vendored, or private to a tool.
    # `**/*_test.*` matches inside all of them, and cargo fills
    # `target/debug/.fingerprint/` with files named
    # `test-integration-test-game_test.json` — which sorts first, reads as the
    # repository's test convention, and makes the tester imitate a build
    # artifact.
    _IGNORED_DIRS = frozenset(
        {
            "target", "build", "dist", "out", "node_modules", "vendor",
            ".git", ".hg", ".svn", ".hybridforge", ".venv", "venv",
            "__pycache__", ".tox", ".mypy_cache", ".pytest_cache", ".next",
        }
    )

    def _example_test(self, exclude: list[str]) -> tuple[str, str] | None:
        """An existing test file for the tester to imitate, if the repo has one.

        Framework is not a preference here, it is a hard constraint: a pytest
        file under `unittest discover` collects zero tests. One real example
        settles it more reliably than any instruction.

        Files this ticket just wrote are excluded — handing back the tester's
        own previous attempt would launder a wrong guess into a convention.

        So are generated directories and non-source extensions. This answer
        decides which language the whole TESTS step believes the suite is
        written in, so picking up a `.json` fingerprint file does not merely
        show a bad example — it concludes the project's tests are JSON and
        skips test authoring for every ticket in the run.
        """
        written = {p.replace("\\", "/") for p in exclude}
        for pattern in self._TEST_GLOBS:
            for path in sorted(self.config.root.glob(pattern)):
                if not path.is_file() or path.suffix in ("", ".pyc"):
                    continue
                relative = path.relative_to(self.config.root).as_posix()
                if relative in written:
                    continue
                if self._IGNORED_DIRS.intersection(Path(relative).parts[:-1]):
                    continue
                if path.suffix.lower() in self._UNTESTABLE_SUFFIXES:
                    continue
                try:
                    content = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if not content.strip():
                    continue
                return relative, content[: self._EXAMPLE_TEST_CHARS]
        return None

    # Extensions that hold no behavior a unit test can assert against. A ticket
    # writing only these has nothing for the tester to encode, and asking anyway
    # is how a Rust project acquired `tests/index_html.rs` — a cargo target
    # whose whole job was string-matching an HTML file, which then broke the
    # build for every ticket that touched that HTML.
    _UNTESTABLE_SUFFIXES = frozenset(
        {
            ".md", ".rst", ".txt", ".json", ".toml", ".yaml", ".yml",
            ".lock", ".cfg", ".ini", ".env", ".gitignore", ".html", ".css",
            # Build and CI scripts. Testable in principle, but never by the
            # unit-test command of the language the project is written in.
            ".sh", ".ps1", ".bat", ".cmd",
        }
    )

    def _suite_suffix(
        self, written: list[str], example: tuple[str, str] | None
    ) -> str:
        """The file extension this project's test command actually collects.

        Taken from a real test file when the repo has one, because that is
        evidence rather than inference. Otherwise guessed from what the ticket
        just wrote, which is only wrong on the very first ticket of a fresh
        repository — where there is nothing yet for the guess to contradict.
        """
        if example is not None:
            return Path(example[0]).suffix.lower()
        counts: Counter[str] = Counter()
        for path in written:
            suffix = Path(path).suffix.lower()
            if suffix and suffix not in self._UNTESTABLE_SUFFIXES:
                counts[suffix] += 1
        return counts.most_common(1)[0][0] if counts else ""

    def _test_target(
        self, ticket: Ticket, written: list[str], example: tuple[str, str] | None
    ) -> tuple[str, str]:
        """Where this ticket's tests go, or why it should not have any.

        Returns `(path, reason)` with exactly one side filled in.

        The path is fixed for the life of the ticket so a retry overwrites its
        previous attempt instead of orphaning it. Orphans are not a tidiness
        problem: verification runs over the whole project, so a test file no
        ticket owns fails every ticket in the backlog and none of them has it
        in scope to delete.
        """
        # A planner that named a test file in the ticket's own scope has made
        # the decision already; honour it rather than inventing a second home
        # for the same assertions.
        designated = [
            normalize_path(path)
            for path in ticket.allowed_files
            if not any(ch in path for ch in "*?[") and matches_any(path, self._TEST_GLOBS)
        ]
        if len(designated) == 1:
            return designated[0], ""

        suffix = self._suite_suffix(written, example)
        if not suffix:
            return "", (
                "the ticket wrote no source file whose behavior a test could "
                "assert against"
            )
        if not any(Path(path).suffix.lower() == suffix for path in written):
            return "", (
                f"the ticket wrote no {suffix} file, and this project's test "
                f"command collects {suffix} tests"
            )

        directory = Path(example[0]).parent.as_posix() if example else "tests"
        slug = re.sub(r"[^a-z0-9]+", "_", ticket.ticket_id.lower()).strip("_") or "ticket"
        prefix = "" if directory in ("", ".") else f"{directory}/"
        # `_test` rather than a bare slug: it is mandatory for `go test`, it is
        # one of pytest's two default collection patterns, and it is inert
        # everywhere else.
        return f"{prefix}{slug}_test{suffix}", ""

    def _discard_tests(self, run_id: int, ticket: Ticket, created: dict[str, bool]) -> None:
        """Remove test files this ticket created but never got verified.

        A ticket that ends failed or blocked leaves behind assertions nothing
        ever confirmed, written against an implementation that does not work.
        Because verification is whole-project, those assertions go on failing
        every remaining ticket — and since the file is outside their scope, the
        backlog cannot recover on its own. That is the exact shape of the run
        where one abandoned `tests/wasm_layer.rs` blocked all six tickets.

        Only files that did not exist before this ticket wrote them are
        removed. Anything that was already on disk is somebody else's, and a
        failed ticket does not earn the right to delete it.
        """
        for path, was_created in sorted(created.items()):
            if not was_created or not is_safe_path(self.config.root, path):
                continue
            target = self.config.root / path
            try:
                target.unlink(missing_ok=True)
            except OSError as exc:
                self.store.log(
                    run_id,
                    f"{ticket.ticket_id}: could not remove unverified test "
                    f"{path} ({exc}); it will fail later tickets.",
                    level="warn",
                    kind="ticket",
                )
                continue
            self.store.log(
                run_id,
                f"{ticket.ticket_id}: removed unverified test {path}.",
                level="warn",
                kind="ticket",
            )

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
