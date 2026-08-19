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

Around all of that sits an optional outer loop: when the backlog is exhausted
and anything is still unfinished, `loop.retryCycles` requeues the wreckage,
respecs it from the recorded failures, and runs the whole backlog again.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import traceback
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from . import respec
from . import evidence
from .artifacts import ABANDONED_DIR, Artifacts, safe_name
from .budget import BudgetGate, Wait
from .config import ANY_LANGUAGE, Config, ConfigError
from .failures import (
    distill,
    environment_failure,
    errors_naming,
    files_blamed,
    locations,
    signatures,
)
from .ingest import write_tickets
from .memory import MemoryClient, MemoryRefused, MemoryUnavailable, ticket_query
from .patch import (
    FileEdit,
    ParsedOutput,
    apply_edits,
    describe_unparsed,
    duplicate_paths,
    enforce_scope,
    foreign_bindings,
    infer_single_file,
    is_safe_path,
    matches_any,
    normalize_path,
    parse_output,
    repo_relative,
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
    PRIOR_ATTEMPT_HEADING,
    PRIOR_FAILURES_HEADING,
    PRIOR_VERDICTS_HEADING,
    build_prompt,
    parse_record,
    parse_verdict,
    parse_bug,
    parse_scope_argument,
    record_prompt,
    rediagnose_prompt,
    repro_prompt,
    review_prompt,
    scope_argument_prompt,
    strip_prompt_echo,
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
    TICKET_BUG,
    TICKET_DONE,
    TICKET_FAILED,
    TICKET_PENDING,
    TICKET_RUNNING,
    TICKET_SKIPPED,
    Store,
    Ticket,
)

# Consecutive memory failures before the loop stops trying for this run.
MEMORY_FAILURE_LIMIT = 3

# Message prefixes the budget gate may drop to make a prompt fit. Everything a
# role is judged on — the spec, the criteria, the diff — is outside this list
# and stays whatever the window costs.
_DROPPABLE_HEADINGS = (
    CONTEXT_HEADING,
    PRIOR_FAILURES_HEADING,
    PRIOR_VERDICTS_HEADING,
    PRIOR_ATTEMPT_HEADING,
)


# A failure in the reproduction that is about the test file rather than about
# the bug: it did not compile, did not import, was not collected. Deliberately
# narrow. A test naming its own file while reporting a failed assertion is the
# reproduction working, and treating that as a broken test parks every bug the
# loop could actually have fixed — so anything not matched here counts as
# evidence, and a build error that slips through fails the attempt the ordinary
# way instead of discarding the proof.
_UNBUILDABLE = re.compile(
    r"syntaxerror|indentationerror|importerror|modulenotfounderror|"
    r"error\[e\d+\]|cannot find|unresolved|undeclared|not declared|"
    r"no such (?:module|file)|failed to compile|collection error|"
    r"error: expected|fatal error",
    re.IGNORECASE,
)


def _droppable(message: Message) -> bool:
    """Whether the budget gate may leave this message out to make a prompt fit.

    Everything a role is judged on — the spec, the criteria, the diff, the
    newest failure — is outside this and stays whatever the window costs.

    An executor's own replayed answer goes too, and its half of the exchange
    may be dropped without the feedback that followed it. That is a turn short
    of a conversation, not a lie: it degrades to what the flat prompt has
    always shown, which is the failure with no answer attached.
    """
    if message.role == "assistant":
        return True
    return message.role == "user" and message.content.startswith(_DROPPABLE_HEADINGS)

CONTROL_KEY = "command"
CONTROL_RUN = "run"
CONTROL_PAUSE = "pause"
CONTROL_STOP = "stop"

# The run the loop is inside right now, written when it enters one. `forge go`
# drains its queue oldest first, so the live run is not always the newest, and
# the dashboard has no other way to tell which one to follow. Only meaningful
# while that run is non-terminal; see `ui.server.snapshot`.
CURRENT_RUN_KEY = "current-run"


def evidence_key(run_id: int) -> str:
    """Control-channel key holding the last retry cycle's failure fingerprint."""
    return f"retry-evidence:{run_id}"


def retries_key(run_id: int) -> str:
    """Control-channel key holding a run's spent automatic retry cycles.

    In the control table rather than in memory so the count survives what the
    loop is built to survive: a killed daemon, a rebooted host, a `forge go`
    that resumes yesterday's run. An in-memory counter would hand a restarted
    run a fresh budget every time, which is exactly how `retryCycles: 3`
    becomes unbounded without anyone choosing it.
    """
    return f"retries:{run_id}"


@dataclass
class StepResult:
    ok: bool
    detail: str = ""
    blocked: bool = False
    # Nothing about this attempt could be checked, and nothing about the next
    # ticket's could be either — the tree is failing every verify step on
    # errors outside this ticket's scope, so each step it "passes" is a step
    # that ran no assertion about it. Ends the run rather than the ticket. See
    # `_unverifiable`.
    halt: bool = False


class Stopped(Exception):
    """Raised internally when the control channel asks the loop to stop."""


class Orchestrator:
    def __init__(self, config: Config, store: Store, started_at: float | None = None):
        self.config = config
        self.store = store
        self.gate = BudgetGate(store, config.rate_limit_policies())
        self.memory = MemoryClient.from_config(config.memory, room=config.room)
        # `maxRuntimeSeconds` caps unattended wall-clock time, not one run's
        # share of it. `forge go` builds a fresh Orchestrator per run in its
        # queue — the coverage and memory state below must not carry across —
        # and passes the queue's start time so draining three runs cannot
        # quietly spend three times the cap.
        self.started_at = time.time() if started_at is None else started_at
        # Bound to a run id in run(); until then nothing is recorded, which is
        # what a bare Orchestrator in a test should do.
        self.artifacts = Artifacts(config.config_dir, 0, enabled=False)
        # Set once a memory failure has been reported, so a server that is down
        # for a twenty-ticket run logs once rather than twenty times.
        self._memory_warned = False
        self._memory_failures = 0
        # Tickets that got a test file, and tickets that were told they should
        # not have one. Individually a skip is routine; every ticket in a run
        # skipping is a misconfiguration, and it presents as a quiet `info`
        # line per ticket rather than as anything wrong. Held as ids rather
        # than counts because a ticket that ends green having authored nothing
        # was checked by reading rather than by running, and `_finish` says
        # which ones those were. See `_report_test_coverage`.
        self._tests_authored: set[str] = set()
        self._tests_skipped: set[str] = set()
        # Tickets already granted the file they blocked asking for. One grant
        # each: a ticket that blocks again after getting what it asked for is
        # saying something a human should read. See `_widen_scope`.
        self._widened: set[str] = set()
        # Bug tickets whose fix works but whose suite fails on an older
        # assertion of the very behavior the report calls a bug, as
        # `{ticket_id: {test path: the lines blaming it}}`. Read at respec,
        # which is where retiring an assertion is settled.
        self._contradictions: dict[str, dict[str, list[str]]] = {}
        # Why the run gave up on verifying anything, once it has. Set when a
        # ticket's every verify step was excused, which means the project no
        # longer builds and no later ticket can be checked either. Ends the
        # run at the first ticket it happens to, rather than at the seventh.
        # See `_unverifiable`.
        self._halt = ""
        # The first verify command that failed without running the code, as
        # `(step, command, the line that says so)`. A missing binary or a
        # launcher that will not start is not a defect any ticket can fix, and
        # handing it to the executor spends a model on an argument it wins.
        # See `_shell` and `environment_failure`.
        self._toolchain: tuple[str, str, str] | None = None

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

        The answer wanted here is tiny — `NOTHING`, or a title and three
        sentences — but the budget is still the configured one rather than a
        ceiling picked to match. A cap is not an allocation: a model that
        replies `NOTHING` spends five tokens whatever it is allowed. A thinking
        model handed a small cap spends all of it before writing anything, and
        then reports that its output budget is too small — naming a number the
        operator never configured and cannot find, having already set
        `maxOutputTokens` to sixty-four times it.
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
                max_tokens=self._output_budget(self.config.record_role),
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
            # Retrieved context and history are droppable; the spec and the
            # criteria are not. Each block is identified by the same constant
            # that writes it — a literal here would silently stop matching the
            # day a heading is reworded. The gate drops in message order, and
            # the prompts put context ahead of history, so a prompt that has to
            # lose something loses retrieved memory before it loses the record
            # of what has already been tried.
            droppable=_droppable,
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

    def _note_toolchain(self, name: str, command: str, result: StepResult) -> None:
        """Record a verify command that never reached the code. First one only.

        Recorded rather than raised: this is called from baseline collection and
        from verification, and each caller has a ticket half-set-up to leave in
        a resumable state. `run` ends the run on it — see `_stop_for_toolchain`.

        Both conditions have to hold. A suite asserting on the text of a shell
        error is real output about real code and prints a diagnostic block
        beside it; a launcher that never started prints nothing else at all, so
        an empty `signatures` is what separates the two.
        """
        if result.ok or self._toolchain or signatures(result.detail):
            return
        reason = environment_failure(result.detail)
        if reason:
            self._toolchain = (name, command, reason)

    def _stop_for_toolchain(self, run_id: int) -> str:
        """End the run because the verify commands cannot run here.

        A failure with no diagnostic in it and a launcher's own words in its
        place is not evidence about the code. Nothing in the loop reads it that
        way: `signatures` finds nothing to attribute, so the baseline excuses
        it, `distill` keeps it whole, and it arrives in the executor's prompt as
        the thing to fix. The executor then explains that the build environment
        is misconfigured and writes no files — which is recorded as a reply that
        did not parse, and costs a reprompt and an attempt.

        A real run did that for ten minutes and thirty model calls before a line
        of code was written, on `Gradle requires JVM 17 or later to run. Your
        build is currently configured to use JVM 8`. Every executor reply in
        that window was right.

        `failed`, not `blocked`: nothing is wrong with the backlog. A ticket
        interrupted on the way in goes back to `pending` so `forge go` picks it
        up unchanged, and a backlog that had already finished keeps its tickets
        green — what is unknown is the state of the tree, not of the work.

        Reached from three places, which is why the message names the command
        rather than the moment: the baseline before a ticket is delegated, the
        verify step after one is, and the final check over a finished backlog —
        where the ordinary red-build message would read as work left undone.
        """
        step, command, reason = self._toolchain or ("", "", "")
        note = (
            f"the {step} command cannot run on this machine: {reason}"
        )
        self.store.set_run_status(run_id, RUN_FAILED, note)
        self.store.log(
            run_id,
            f"Stopping: `{command}` failed without ever reaching the code.\n"
            f"  {reason}\n"
            f"No ticket can fix this and nothing has been delegated for it — a "
            f"model handed a broken toolchain answers, correctly, that the "
            f"environment is wrong, and that answer costs an attempt. Fix the "
            f"command or the machine, check it with `forge doctor`, then "
            f"`forge go`: no ticket was blamed for this and nothing was "
            f"requeued.",
            level="error",
            kind="lifecycle",
            data={"step": step, "command": command, "reason": reason},
        )
        return RUN_FAILED

    # The verify steps, in the order a failure is cheapest to diagnose.
    _VERIFY_STEPS = ("lint", "typecheck", "test")

    def _verify_plan(self) -> list[tuple[str, str]]:
        """Every verify command to run, as `(step name, command)`.

        One command per step assumed a repository is one language. With a map
        there is one per language, and all of them run: verification stays
        whole-project, which is what the baseline amnesty, the orphan sweep and
        "you broke this, not the ticket before you" all rest on. A language
        with no files in the tree is skipped — a JavaScript runner in a repo
        that has no JavaScript yet has nothing to say, and running it would
        fail on an empty match.

        Identical commands under two keys run once. A step with a single
        command keeps its plain name, so a one-language project's step log and
        dashboard read exactly as before; only a project that genuinely has two
        gets `test[.js]`.
        """
        present = self._languages_present()
        plan: list[tuple[str, str]] = []
        for kind in self._VERIFY_STEPS:
            commands = self.config.commands_for(kind)
            wanted = {
                suffix: command
                for suffix, command in commands.items()
                if suffix == ANY_LANGUAGE or suffix in present
            }
            seen: dict[str, str] = {}
            for suffix, command in sorted(wanted.items()):
                if command in seen.values():
                    continue
                seen[suffix] = command
            for suffix, command in seen.items():
                label = kind if len(seen) == 1 else f"{kind}[{suffix}]"
                plan.append((label, command))
        return plan

    def _languages_present(self) -> set[str]:
        """Extensions of the source files this project actually has.

        Read per call rather than cached: a ticket that writes the project's
        first `.js` file has just made the JavaScript runner relevant, and the
        verify step that follows it is the first place that matters.
        """
        return {
            suffix
            for path in evidence.repo_files(self.config.root, limit=4000)
            if (suffix := Path(path).suffix.lower())
        }

    @staticmethod
    def _fence_guidance(truncated: list[str], written: Sequence[str] = ()) -> str:
        """What to tell an executor whose fence was shorter than its content."""
        detail = (
            "These files are wrapped in a fence no longer than one they "
            "contain, so the block ends inside the file and they were not "
            "written:\n"
            + "\n".join(f"- {path}" for path in truncated)
            + "\n\nA ``` inside a file closes a ``` wrapper. Wrap any file "
            "whose own contents use fences — README.md and most other markdown "
            "— in a LONGER fence: four backticks, or five."
        )
        if written:
            detail += (
                "\n\nThe rest of your response was written and is on disk:\n"
                + "\n".join(f"- {path}" for path in written)
                + "\nSend only the files listed above as missing. Emit each "
                "file exactly once."
            )
        else:
            detail += " Emit each file exactly once."
        return detail

    @staticmethod
    def _duplicate_guidance(repeated: list[str]) -> str:
        return (
            "Your response contained more than one block for the same file, so "
            "nothing was written:\n"
            + "\n".join(f"- {path}" for path in repeated)
            + "\n\nThe usual cause is a fence, not a mistake in the code. A "
            "file whose own contents contain ``` closes its wrapping fence "
            "early, and the rest of that file is then read as further files "
            "named after whatever paths appear in its prose. Wrap any such "
            "file in a longer fence — four backticks or five — and emit each "
            "file exactly once."
        )

    def _recover_unlabeled(
        self, run_id: int, ticket: Ticket, text: str
    ) -> ParsedOutput | None:
        """Read a reply that wrote the right file and forgot to name it.

        `None` when nothing here is safe to recover, which leaves the reply
        refused exactly as before.

        The observed failure is not a model that cannot follow the format. It is
        a model that reasons at length about a hard ticket, quotes the existing
        code in one fence, emits the whole corrected file in another, and omits
        the path line above it. Asking again produces the same shape — the
        reprompt was answered twice, identically — because the reasoning is what
        filled the reply, not a misunderstanding about headers. Three of one
        ticket's five attempts went this way, and six of another's nine.

        Only attempted when the ticket has exactly **one** writable file, so
        there is no destination to guess: either that file was rewritten or
        nothing was. Which block is the file is decided in `infer_single_file`,
        against what is already on disk.

        Recorded at `warn` whenever it fires. The harness has just written a
        file the model did not explicitly address, and that should be visible in
        the log rather than inferred from a diff.
        """
        writable = [
            path for path in ticket.allowed_files if not any(c in path for c in "*?[")
        ]
        if len(writable) != 1:
            return None

        path = writable[0]
        try:
            current = (self.config.root / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            current = ""

        body = infer_single_file(text, current)
        if not body:
            return None

        self.store.log(
            run_id,
            f"{ticket.ticket_id}: the executor's reply held the whole of "
            f"{path} in a fenced block with no path line above it. This ticket "
            f"writes one file and the block still contains what is on disk, so "
            f"it was read as that file rather than thrown away. The reply was "
            f"right; only its header was missing.",
            level="warn",
            kind="ticket",
            data={"ticket": ticket.ticket_id, "path": path, "characters": len(body)},
        )
        return ParsedOutput(edits=[FileEdit(path=path, content=body)])

    def _malformed_reply(self, parsed: ParsedOutput, text: str) -> str:
        """Why a reply did not parse into files, or `""` if it did.

        Only formatting. A `BLOCKED:` reply is a decision, and a reply carrying
        no file content at all may be a ticket whose work is already done —
        neither is malformed, and neither is worth asking again about.

        A reply that parsed *some* files is not malformed either. Those are
        written, and the attempt reports what is still missing; asking again
        would risk trading a partial answer for a worse one.
        """
        if parsed.is_blocked:
            return ""
        if parsed.truncated and not parsed.edits:
            return self._fence_guidance(parsed.truncated)
        if parsed.is_empty:
            return describe_unparsed(text)
        repeated = duplicate_paths(parsed)
        return self._duplicate_guidance(repeated) if repeated else ""

    @staticmethod
    def _signature_scope(
        signature: str, allowed: list[str], root: Path | str | None = None
    ) -> bool:
        """Whether a diagnostic points at a file this ticket may write.

        Signatures carry their location, so the file a complaint is about can be
        compared against the ticket's own scope. Matching is lowercased because
        `signatures` folds case, and `Cargo.toml` would otherwise never match
        `cargo.toml`.

        Locations are read by `failures.locations`, which knows every spelling a
        compiler uses. This once read rustc's `-->` marker and nothing else,
        which made the check silently language-specific: cargo was attributed
        correctly, and javac, tsc, go, gcc, and pytest — none of which emit
        `-->` — parsed to no location at all and were therefore all excused. A
        Java run took seven tickets to green with twenty compile errors standing,
        because every one of them looked like somebody else's.

        `root` lets an absolute path be recognised as a file inside the
        repository. Without it a javac diagnostic naming
        `d:\\repo\\src\\main\\java\\A.java` cannot match the pattern
        `src/main/java/A.java` that put the file in scope in the first place.

        A signature with no parseable location answers False, which leaves the
        failure excusable. That is the safe direction: the alternative blames a
        ticket for something it may have no authority to touch.
        """
        patterns = [pattern.lower() for pattern in allowed]
        for location in locations(signature):
            if matches_any(repo_relative(location, root), patterns):
                return True
        return False

    def _inherited_failures(
        self, run_id: int, ticket: Ticket, repro_path: str = ""
    ) -> dict[str, set[str]]:
        """What this ticket is allowed to blame on the state it arrived in.

        The baseline is re-taken every cycle, and has to be: tickets run in
        between, and a ticket that inherits a stale baseline is asked to fix
        errors another one wrote after it. The cost is a ticket whose own
        breakage is still on disk when the next cycle starts, so a fresh
        baseline reads it as pre-existing — amnesty for the errors it just
        wrote, renewed every cycle, so the debt can only grow. A real run went 3
        errors, then 7, then 13, then 20, and finished with every ticket `done`
        on a tree that did not compile.

        `quarantineFailed` closes most of that: a ticket that gives up has its
        work taken back out of the tree, so the next cycle usually starts from
        the state it inherited. It cannot be relied on to — the revert needs a
        baseline tree to read, and a repository without git has none — so the
        subtraction below stays load-bearing rather than becoming redundant.

        Subtracting what the ticket has been charged for is what separates
        "already broken when I got here" from "broken by me last time". Both
        halves are needed: keep the fresh baseline and a ticket is punished for
        its neighbours, keep only the first cycle's and it is forgiven for
        itself.
        """
        if not self.config.loop.baseline_verify:
            return {}
        baseline = self._baseline_failures(
            run_id, ticket, extra_scope=[repro_path] if repro_path else []
        )
        charged = set(ticket.charged_failures)
        if not charged:
            return baseline

        kept: dict[str, set[str]] = {}
        for name, found in baseline.items():
            remaining = found - charged
            if remaining:
                kept[name] = remaining
            if len(remaining) != len(found):
                self.store.log(
                    run_id,
                    f"{ticket.ticket_id}: {len(found) - len(remaining)} {name} "
                    f"error(s) pre-date this cycle but were introduced by an "
                    f"earlier one of its own; still its to fix.",
                    level="warn",
                    kind="verify",
                    data={"step": name, "signatures": sorted(found - remaining)[:20]},
                )
        return kept

    # A ticket that will not run again in this cycle. Red left behind by one of
    # these is red nothing in the backlog is still working on.
    _GAVE_UP = frozenset({TICKET_FAILED, TICKET_BLOCKED, TICKET_SKIPPED})

    def _unverifiable(
        self, run_id: int, ticket: Ticket, excused: Sequence[str], output: str
    ) -> str:
        """Why nothing this ticket did could be checked, or "" if that is fine.

        The amnesty is right and this is its cost. A failure that pre-dates a
        ticket is excused so one abandoned file cannot fail an entire backlog —
        but a step excused whole ran no assertion about the ticket in front of
        it, and when *every* step is in that state the ticket has been checked
        by nothing. On a compiled language it is worse than it sounds: a red
        typecheck means the test binary was never built, so the suite did not
        run at all. The reviewer reads a diff and says yes, and `done` comes to
        mean "a model liked the look of it".

        A whole run went that way. Two files were left importing a package that
        has never existed; five tickets after them were verified against a tree
        that could not compile, each logging `typecheck still failing, but only
        on errors that pre-date this ticket`, and the backlog reported every one
        of them green. 168 minutes and 2.4M tokens for a project where
        `compileJava` fails on the first file it reads.

        Two things have to be true before that is worth stopping a run over, and
        the second is what keeps this from firing on ordinary work:

        - Not one verify step passed. A ticket with a green typecheck and an
          excused test suite has still had its code compiled.
        - Some ticket that may write a red file has already given up. Red owned
          by a ticket still pending is a backlog mid-flight — a JVM plan is
          routinely red between the ticket that calls a class and the one that
          writes it — and red owned by nobody is an orphan, which `_finish` and
          the orphan sweep already handle. Red owned by a ticket that is out of
          attempts is the one case where nothing coming will clear it, and
          every ticket after this one will be marked green against it.

        Returns the note to park on, or "" to carry on as before.
        """
        red = sorted(
            {
                repo_relative(path, self.config.root).lower()
                for path in files_blamed(output)
            }
        )
        if not red:
            # Output nothing could locate. The failure may be real and may be
            # somebody's, but there is no file to name and no owner to look up,
            # and stopping a run on an unparseable diagnostic is the wrong
            # direction to be wrong in.
            return ""

        owners: dict[str, str] = {}
        for other in self.store.list_tickets(run_id):
            patterns = [path.lower() for path in other.allowed_files]
            if any(matches_any(path, patterns) for path in red):
                owners[other.ticket_id] = other.status
        stalled = sorted(
            ticket_id
            for ticket_id, status in owners.items()
            if status in self._GAVE_UP and ticket_id != ticket.ticket_id
        )
        if not stalled:
            return ""

        note = (
            f"nothing this ticket wrote was compiled or run. Every verify step "
            f"this project has ({', '.join(excused)}) failed on errors in files "
            f"the ticket does not own, so each one was excused — and a step "
            f"excused whole asserts nothing about the work in front of it. "
            f"Passing review on top of that would record a green nobody checked."
            f"\n\nThe tree is red on:\n"
            + "\n".join(f"  - {path}" for path in red[:8])
            + f"\n\n{', '.join(stalled)} already gave up on those files, so "
            f"nothing left in this backlog is going to clear them and every "
            f"ticket after this one would be marked done against a project that "
            f"does not build. Fix them, then `forge retry`."
        )
        self.store.log(
            run_id,
            f"{ticket.ticket_id}: {note}",
            level="error",
            kind="verify",
            data={
                "ticket": ticket.ticket_id,
                "excused": list(excused),
                "red": red,
                "stalled": stalled,
            },
        )
        return note

    def _charge(self, run_id: int, ticket: Ticket, found: set[str]) -> None:
        """Record failures as this ticket's, permanently.

        A charged signature is excluded from every later baseline this ticket
        takes, which is the whole point: the baseline is re-taken each cycle so
        that other tickets' breakage stays excused, and without a memory of what
        this one broke there is no way to tell the two apart. There was not one,
        and a failed ticket's own errors came back the next cycle wearing the
        pre-existing label it had just earned them.

        Kept sorted and deduplicated so the persisted list is stable to compare
        across cycles, and written only when it actually changes — this runs on
        every verify step of every attempt.
        """
        if not found:
            return
        merged = sorted(set(ticket.charged_failures) | found)
        if merged == ticket.charged_failures:
            return
        ticket.charged_failures = merged
        self.store.update_ticket(run_id, ticket)

    def _baseline_failures(
        self, run_id: int, ticket: Ticket, extra_scope: Sequence[str] = ()
    ) -> dict[str, set[str]]:
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

        The excuse stops at the edge of the ticket's own scope. A failure in a
        file the ticket may write is one it is able to fix, and on a retry it is
        usually one the ticket *caused* — where quarantine could not take the
        work back out, the next cycle starts with its own breakage on disk and
        would otherwise collect a baseline that forgives it. That happened: a
        ticket left four
        clippy errors in `src/board.rs`, was requeued, and passed its lint step
        on the grounds that the errors pre-dated the attempt. They did. It wrote
        them.
        """
        known: dict[str, set[str]] = {}
        for name, command in self._verify_plan():
            result = self._shell(run_id, f"baseline-{name}", command)
            # Before anything is attributed. A command that cannot start is the
            # one failure the baseline must not treat as a fact about the code.
            self._note_toolchain(name, command, result)
            if result.ok:
                continue
            found = signatures(result.detail)
            if not found:
                # Unparseable output cannot be compared against anything later.
                # Leaving it out means the ticket is judged on this step
                # normally, which is the safe direction to be wrong in.
                continue

            # `extra_scope` carries a bug ticket's reproduction test. That
            # file is not writable by anyone here, so the ordinary rule would
            # excuse it — and on a retry cycle the reproduction is already on
            # disk and already failing, which is exactly the failure the ticket
            # exists to clear. Amnesty for it would let a bug ticket pass
            # verification with the bug still in place.
            scope = list(ticket.allowed_files) + list(extra_scope)
            owned = {
                signature
                for signature in found
                if self._signature_scope(signature, scope, self.config.root)
            }
            if owned:
                self.store.log(
                    run_id,
                    f"{ticket.ticket_id}: {len(owned)} pre-existing {name} "
                    f"error(s) are in files this ticket may write, so they are "
                    f"its to fix and are not excused.",
                    level="warn",
                    kind="verify",
                    data={"step": name, "signatures": sorted(owned)[:20]},
                )

            inherited = found - owned
            if not inherited:
                continue
            known[name] = inherited
            self.store.log(
                run_id,
                f"{ticket.ticket_id}: {name} was already failing before this "
                f"ticket started ({len(inherited)} error(s)) outside its scope; "
                "it will not be blamed for them.",
                level="warn",
                kind="verify",
                data={"step": name, "signatures": sorted(inherited)[:20]},
            )
        return known

    # ------------------------------------------------------------------
    # The loop
    # ------------------------------------------------------------------

    def run(self, run_id: int) -> str:
        """Drive a run to a terminal state. Returns that state."""
        self.store.set_control(CONTROL_KEY, CONTROL_RUN)
        self.store.set_control(CURRENT_RUN_KEY, str(run_id))
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

        unreachable = self._preflight(run_id)
        if unreachable:
            self.store.set_run_status(
                run_id, RUN_FAILED, f"{len(unreachable)} role(s) unreachable"
            )
            return RUN_FAILED

        # Before the first ticket, and before anything is requeued: a run
        # cannot verify anything on a tree that was red when it started, and
        # every ticket in the backlog would be excused for it in turn. See
        # `_green_baseline`.
        red = self._green_baseline(run_id)
        if self._toolchain:
            return self._stop_for_toolchain(run_id)
        if red:
            return self._stop_for_red_baseline(run_id, red)

        # Before the first ticket: a human may have requeued something already
        # green since the last run, and whatever was built on top of it was
        # judged against the version now being replaced.
        self._reopen_stale(run_id)

        try:
            while True:
                self._honor_control(run_id)
                self._check_runtime(run_id)

                # Checked before the halt and before any ticket, because it is
                # not a fact about the backlog at all: the machine cannot run
                # the commands this project is verified with, and every ticket
                # would fail identically for a reason none of them can fix.
                if self._toolchain:
                    return self._stop_for_toolchain(run_id)

                # A tree that fails every check on work outside the running
                # ticket's scope cannot verify the next ticket either, and a
                # retry cycle only requeues tickets into the same wall. Ends
                # the run where the evidence ran out.
                if self._halt:
                    self.store.log(
                        run_id,
                        f"Stopping: the project no longer builds, and nothing "
                        f"the loop verifies is verifying anything. "
                        f"{self._halt}",
                        level="error",
                        kind="lifecycle",
                    )
                    return self._finish(run_id)

                ticket = self.store.next_ticket(run_id)
                if ticket is None:
                    outcome = self._finish(run_id)
                    if outcome == RUN_DONE or not self._retry_cycle(run_id, outcome):
                        return outcome
                    continue

                self._work_ticket(run_id, ticket)

        except Stopped:
            self.store.set_run_status(run_id, RUN_STOPPED, "stopped by request")
            self.store.log(run_id, "Loop stopped.", level="warn", kind="lifecycle")
            return RUN_STOPPED
        except Exception as exc:  # noqa: BLE001 - the daemon must record why it died
            # With the traceback. Without it the record is a sentence with no
            # location — one run died on `bad parameter or other API misuse`,
            # which is sqlite's way of saying two threads shared a connection,
            # and nothing said which call it happened in.
            self.store.set_run_status(run_id, RUN_FAILED, str(exc))
            self.store.log(
                run_id,
                f"Loop failed: {exc}",
                level="error",
                kind="lifecycle",
                data={"traceback": traceback.format_exc()[-4000:]},
            )
            return RUN_FAILED

    def _dep_stamp(self, run_id: int, ticket: Ticket) -> dict[str, str]:
        """Fingerprint every dependency of `ticket` as it stands right now."""
        if not ticket.needs:
            return {}
        current = {t.ticket_id: t for t in self.store.list_tickets(run_id)}
        return {
            dep: current[dep].fingerprint
            for dep in ticket.needs
            if dep in current
        }

    def _stale_dependents(self, run_id: int) -> dict[str, list[str]]:
        """Done tickets whose dependencies have been rewritten since they passed.

        Transitive: re-opening a ticket makes its own dependents stale too,
        because whatever they were built on is about to be rebuilt. Iterated to
        a fixed point rather than walked recursively, which keeps a diamond
        from being reported twice.

        Returns `{ticket_id: [dependency that moved, ...]}` so the log can say
        which ticket forced which re-open — an unattended run that quietly
        re-does half a backlog needs to be able to answer why.
        """
        tickets = {t.ticket_id: t for t in self.store.list_tickets(run_id)}
        stale: dict[str, list[str]] = {}
        changing = {
            t.ticket_id for t in tickets.values() if t.status != TICKET_DONE
        }
        while True:
            found = False
            for ticket in tickets.values():
                if ticket.status != TICKET_DONE or ticket.ticket_id in stale:
                    continue
                moved = [
                    dep
                    for dep in ticket.needs
                    if dep in tickets
                    and (
                        dep in changing
                        or ticket.dep_stamp.get(dep) != tickets[dep].fingerprint
                    )
                ]
                if moved:
                    stale[ticket.ticket_id] = moved
                    changing.add(ticket.ticket_id)
                    found = True
            if not found:
                return stale

    def _report_test_coverage(self, run_id: int) -> None:
        """Say so when the whole run authored no tests.

        One ticket skipping test authoring is ordinary — a build script or a
        stylesheet has nothing a unit test can assert against, and review
        checks its criteria instead. Every ticket skipping is a project-level
        misconfiguration wearing the same clothes, and it reported itself as a
        routine `info` line per ticket. A run once degraded to review-only
        after its second ticket and said nothing that read as wrong.
        """
        self._report_unexecuted(run_id)
        self._report_unlinted(run_id)
        if not self.config.commands_for("test") or self._tests_authored:
            return
        if not self._tests_skipped:
            return
        self.store.log(
            run_id,
            f"No ticket in this run authored tests ({len(self._tests_skipped)} "
            f"skipped) while a test command is configured. Verification ran on "
            f"review alone. Check that `commands.test` and the files the "
            f"tickets write are the same language.",
            level="warn",
            kind="lifecycle",
        )

    def _report_unlinted(self, run_id: int) -> None:
        """Name the languages this project builds and does not lint.

        Reported, never gated. Tests are proof and lint is quality: a ticket in
        a language nothing can test is a ticket nothing can check, while one in
        a language nothing lints is merely one nobody is holding to a style.
        Blocking on the second would stall a backlog over a build script.
        """
        if not self.config.commands_for("lint"):
            return
        present = self._languages_present() & self._CODE_SUFFIXES
        missing = sorted(
            suffix
            for suffix in present
            if not self.config.covers("lint", suffix)
            and not self.config.exempt("lint", suffix)
            and not self.config.exempt("test", suffix)
        )
        if not missing:
            return
        self.store.log(
            run_id,
            f"No lint command covers {', '.join(missing)}. Work in "
            f"{'that language' if len(missing) == 1 else 'those languages'} was "
            f"tested but never linted — add one with `forge toolchain --kind "
            f"lint --language {missing[0]}`, or leave it and know that is the "
            f"bar this run was held to.",
            level="warn",
            kind="lifecycle",
            data={"unlinted": missing},
        )

    def _report_unexecuted(self, run_id: int) -> None:
        """Name the tickets that went green without anything being run.

        A ticket that authors no tests is checked at review, against its
        criteria — and criteria for a browser shell are often satisfiable by a
        text search. One read `web/main.js` calls `WebAssembly.instantiateStreaming`,
        which was true of code that threw on the next line: the backlog was
        green, the suite was 36 tests, and the page loaded to an empty board.
        Nothing in the pipeline could have caught it, and nothing said so.

        This does not test anything new. It says what the green did not cover,
        which is what would have pointed a human at the two files worth opening
        by hand.
        """
        review_only = self._tests_skipped - self._tests_authored
        if not review_only:
            return
        named = sorted(
            ticket.ticket_id
            for ticket in self.store.list_tickets(run_id)
            if ticket.status == TICKET_DONE and ticket.ticket_id in review_only
        )
        if not named:
            return
        subject = "It authored" if len(named) == 1 else "None of them authored"
        them = "it" if len(named) == 1 else "them"
        self.store.log(
            run_id,
            f"{', '.join(named)} passed on review alone. {subject} no tests and "
            f"nothing the test command runs covers {them}, so the criteria were "
            f"checked by reading the diff rather than by running anything. A "
            f"criterion a text search can satisfy is also satisfied by code that "
            f"never executes — open {them} by hand before trusting this run.",
            level="warn",
            kind="lifecycle",
            data={"review_only": named},
        )

    def _reopen_stale(self, run_id: int) -> list[str]:
        """Requeue tickets that passed on a dependency which has since moved.

        The trigger is a human, not the automatic cycle: `reset_tickets` only
        requeues failed, blocked and skipped work, and a done ticket's
        dependencies are necessarily done too — so nothing an unattended run
        does on its own can move them. What does is `forge retry --ticket` or
        `--all` on something already green, which is a normal thing to do after
        reading a diff. Everything built on top of it was judged against the
        version being replaced.

        Off by `loop.reopenStaleDependents` for anyone who would rather be
        warned than have a backlog redone underneath them.
        """
        stale = self._stale_dependents(run_id)
        if not stale:
            return []

        if not self.config.loop.reopen_stale_dependents:
            for ticket_id, moved in sorted(stale.items()):
                self.store.log(
                    run_id,
                    f"{ticket_id}: passed against {', '.join(moved)}, which has "
                    f"changed since. Left done — reopenStaleDependents is off.",
                    level="warn",
                    kind="ticket",
                )
            return []

        for ticket_id, moved in sorted(stale.items()):
            self.store.log(
                run_id,
                f"{ticket_id}: reopened — it passed against {', '.join(moved)}, "
                f"which has changed since.",
                level="warn",
                kind="ticket",
            )
        reopened = self.store.reset_tickets(run_id, ticket_ids=sorted(stale))
        return [ticket.ticket_id for ticket in reopened]

    def _park_unreachable(self, run_id: int) -> int:
        """Skip tickets whose dependencies will never arrive.

        The scheduler stops handing out work when nothing is eligible, which
        on an acyclic graph means every remaining ticket is waiting on one
        that failed. Attempting them anyway is not merely wasted budget: the
        executor runs against a half-built dependency, and the failures it
        produces are filed as evidence about *this* ticket's spec, which respec
        then reads as a defect to fix. One run spent three attempts per cycle
        on a ticket whose only problem was that its dependency had not landed.
        """
        parked = 0
        for ticket in self.store.list_tickets(run_id):
            if ticket.status != TICKET_PENDING:
                continue
            missing = self.store.unmet_needs(run_id, ticket)
            if not missing:
                continue
            ticket.status = TICKET_SKIPPED
            ticket.blocked_note = f"dependency not met: {', '.join(missing)}"
            self.store.update_ticket(run_id, ticket)
            self.store.log(
                run_id,
                f"{ticket.ticket_id}: skipped — needs {', '.join(missing)}.",
                level="warn",
                kind="ticket",
            )
            parked += 1
        return parked

    def _preflight(self, run_id: int) -> list[str]:
        """Probe every configured role before spending a ticket on it.

        `forge doctor` has always caught a dead endpoint in two seconds; `forge
        go` did not ask, so a missing model produced a full backlog of blocked
        tickets, a respec pass over each one, and a stop — all reporting
        whatever the failure looked like from inside the loop rather than what
        it was. One run blamed six tickets of 1-3k tokens for being "too large
        for this model" when the model was not on the server at all.

        Each distinct model is probed once, not each role, so the common
        single-model config costs one call. Returns the model names that did
        not answer.
        """
        if not self.config.loop.preflight:
            return []

        checked: dict[str, str] = {}
        for role in sorted(set(self.config.roles)):
            try:
                provider = self.config.provider_for(role)
            except ConfigError as exc:
                self.store.log(
                    run_id,
                    f"Cannot start: the {role} role is not configured — {exc}",
                    level="error",
                    kind="lifecycle",
                )
                return [role]
            if provider.name in checked:
                continue
            report = provider.health()
            checked[provider.name] = report
            if report.startswith("FAIL"):
                self.store.log(
                    run_id,
                    f"Cannot start: the {role} model did not answer. {report} "
                    f"Nothing has been spent. Fix it and run `forge doctor` to "
                    f"confirm before starting again.",
                    level="error",
                    kind="lifecycle",
                )
        return [name for name, report in checked.items() if report.startswith("FAIL")]

    def _green_baseline(self, run_id: int) -> str:
        """Refuse to start on a tree that is already red. Returns a note or "".

        Everything the loop knows about who broke what is relative. A failure
        that pre-dates a ticket is excused so one abandoned file cannot fail an
        entire backlog, and `_unverifiable` catches the case where that amnesty
        has swallowed a ticket whole. Neither reaches a repository that was
        red before the run: the errors are in files no ticket owns, so every
        ticket inherits them, every ticket is excused for them, and there is no
        exhausted owner for `_unverifiable` to name. The backlog then reports
        green over a project that never compiled once, and the only thing that
        notices is `_finish` — after every ticket has been spent.

        So the tree is checked once, before the first delegation, and the run
        refuses to start rather than reporting the problem an hour later. The
        first unit of work on a red repository is fixing the red, and that is a
        ticket a human writes: the loop cannot scope it, because the files it
        would have to authorise are precisely the ones nobody has claimed.

        Two failures are deliberately not gated on:

        A command that never reached the code. `_note_toolchain` already
        recognises those and `run` ends on `_stop_for_toolchain`, which says
        what is actually wrong instead of blaming the tree.

        A failure naming no file. `pytest` exits 5 on a repository with no
        tests, `npm test` fails with no script, and a greenfield project is the
        normal way a backlog starts — it is where this very run began, with an
        empty repository and a `test` command that had nothing to run. Blocking
        those would make the gate fire hardest on the runs it has nothing to
        say about, so an unattributable failure is reported and the run
        continues. This is the same direction `_unverifiable` and
        `_signature_scope` are already wrong in, for the same reason.
        """
        if not self.config.loop.require_green_baseline:
            return ""

        failed: list[str] = []
        output: list[str] = []
        for name, command in self._verify_plan():
            result = self._shell(run_id, f"start-{name}", command)
            self._note_toolchain(name, command, result)
            if self._toolchain:
                return ""
            if not result.ok:
                failed.append(name)
                output.append(result.detail)
        if not failed:
            return ""

        red = sorted(
            {
                repo_relative(path, self.config.root)
                for path in files_blamed("\n".join(output))
            }
        )
        if not red:
            self.store.log(
                run_id,
                f"{', '.join(failed)} already failed before the first ticket, "
                f"but named no file. Reported rather than gated: an empty "
                f"suite on a new project fails this way, and it is how most "
                f"backlogs start. Nothing before the first ticket is excused "
                f"for this — every ticket will inherit it.",
                level="warn",
                kind="verify",
                data={"steps": failed},
            )
            return ""

        return (
            f"the project is already red before the first ticket: "
            f"{', '.join(failed)} fails on files no ticket in this backlog "
            f"owns.\n\n"
            + "\n".join(f"  - {path}" for path in red[:8])
            + (f"\n  ... and {len(red) - 8} more" if len(red) > 8 else "")
            + "\n\n"
            + distill("\n".join(output), limit=1500)
        )

    def _stop_for_red_baseline(self, run_id: int, note: str) -> str:
        """End the run before anything is delegated. Nothing is spent."""
        self.store.set_run_status(run_id, RUN_BLOCKED, "the tree was red before the run")
        self.store.log(
            run_id,
            f"Cannot start — {note}\n\n"
            f"A ticket is only ever judged on the errors it introduces, so "
            f"every one of these would be excused for every ticket in the "
            f"backlog. The run would finish reporting green having compiled "
            f"nothing. Fix the tree first — that is the first ticket, and it "
            f"is one a human writes, because the files it would have to "
            f"authorise are the ones no ticket claims — then `forge go`. To "
            f"start anyway and let the backlog inherit this, set "
            f"`loop.requireGreenBaseline` to false or pass "
            f"`--allow-red-baseline`.",
            level="error",
            kind="lifecycle",
            data={"note": note},
        )
        return RUN_BLOCKED

    def _finish(self, run_id: int) -> str:
        # Order matters here. Parking first turns "pending forever" into a
        # reported skip, so the counts below describe the run a human has to
        # act on rather than one that merely stopped.
        self._park_unreachable(run_id)
        # An unverified test file left by a ticket that did not land fails the
        # final check below, and every ticket of any retry cycle after it.
        self._sweep_orphan_tests(run_id)
        self._report_test_coverage(run_id)

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
        for name, command in self._verify_plan():
            result = self._shell(run_id, f"final-{name}", command)
            # The last place a broken toolchain can appear, and the one where
            # mislabelling it is most misleading: every ticket is green, so
            # "backlog complete but typecheck still fails" reads as work left
            # undone rather than as a command that could not start. Nothing
            # reaches here with one already recorded — `run` ends the run on
            # that before asking for the next ticket.
            self._note_toolchain(name, command, result)
            if self._toolchain:
                return self._stop_for_toolchain(run_id)
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

    # ------------------------------------------------------------------
    # Automatic retry cycles
    # ------------------------------------------------------------------

    def _evidence_fingerprint(self, run_id: int, ticket_ids: Sequence[str]) -> str:
        """A stable digest of why these tickets are unfinished.

        Built from the recorded step failures, which `ticket_failures` already
        deduplicates — so a cycle that reproduces the previous one's failures
        exactly produces the same digest, and one that fails in any new way
        does not. Hashed rather than stored whole: this goes in a control row,
        and the failures it summarizes can run to tens of kilobytes.
        """
        material: list[str] = []
        for ticket_id in sorted(ticket_ids):
            for failure in self.store.ticket_failures(run_id, ticket_id):
                material.append(f"{ticket_id}::{failure['name']}::{failure['detail']}")
        if not material:
            # No recorded evidence at all. Never equal to a previous cycle's
            # digest, so an absent step log can never be the thing that stops
            # a retry — it is the one case where nothing has been learned.
            return f"none::{time.time()}"
        return hashlib.sha256("\n".join(material).encode("utf-8")).hexdigest()

    def _retries_spent(self, run_id: int) -> int:
        try:
            return int(self.store.get_control(retries_key(run_id), "0"))
        except ValueError:
            return 0

    def _retry_cycle(self, run_id: int, outcome: str) -> bool:
        """Requeue the wreckage and go round again. Returns whether it did.

        `forge retry --respec`, run by the loop itself, at the one moment the
        evidence is complete: every ticket has been tried, every failure is in
        the step log, and nothing is half-finished. A `False` here means the
        run keeps the terminal state `_finish` gave it.

        Cheap to get wrong in the expensive direction, so five things bound it.
        The count is persisted, not held in memory. A cycle with nothing to
        requeue ends the run. A cycle that reproduced the previous cycle's
        failures exactly ends the run. A respec that changed nothing ends the
        run, because the next cycle would hand the executor a ticket that has
        already failed and hope for a different sample. And the control channel
        is checked before every step of the cycle that follows, so `forge stop`
        still lands on an unbounded one.
        """
        limit = self.config.loop.retry_cycles
        if limit == 0:
            return False

        spent = self._retries_spent(run_id)
        if limit > 0 and spent >= limit:
            self.store.log(
                run_id,
                f"Run still {outcome} after {spent} automatic retry cycle(s); "
                f"leaving it for a human. Raise loop.retryCycles, or requeue by "
                f"hand with `forge retry --respec`.",
                level="warn",
                kind="lifecycle",
            )
            return False

        # Captured before the requeue clears them — for a ticket the executor
        # gave up on with `BLOCKED:`, the note is the only record of what it
        # could not decide, and no step was logged as failed.
        tickets = self.store.list_tickets(run_id)
        notes = {t.ticket_id: t.blocked_note for t in tickets}
        # Triage still holds. A claude-only ticket is one the plan judged
        # unsafe to delegate, so requeueing it only gets it skipped again — and
        # under `-1` that is a cycle that repeats forever while doing nothing
        # but spending a planner call on each pass.
        # A bug ticket that never reproduced is in the same position: it stops
        # at the same step every cycle, because nothing between cycles makes an
        # undemonstrable fault demonstrable. One report ran fifteen cycles that
        # way — two tester calls apiece — and would have run forever under
        # `-1`. The tester's own explanation is in `blocked_note`; that is the
        # thing to read, and it needs a person.
        unprovable = {
            ticket.ticket_id
            for ticket in tickets
            if ticket.kind == TICKET_BUG
            and ticket.status == TICKET_BLOCKED
            and not self.store.reproduced(run_id, ticket.ticket_id)
        }
        for ticket_id in sorted(unprovable):
            self.store.log(
                run_id,
                f"{ticket_id}: not retried — the bug was never reproduced, and "
                f"another cycle would write the same test against the same code "
                f"for the same result. Read the blocked note: either the report "
                f"needs sharpening, or the fault is somewhere this project's "
                f"test command does not reach.",
                level="warn",
                kind="lifecycle",
            )

        eligible = [
            ticket.ticket_id
            for ticket in tickets
            if ticket.status in self.store.RETRYABLE
            and ticket.route == "delegate"
            and ticket.ticket_id not in unprovable
        ]
        if not eligible:
            # Nothing here is the loop's to retry: either every ticket landed
            # and the *final* verify is failing on breakage no ticket in this
            # backlog introduced, or what is left is claude-only. Another cycle
            # would requeue nothing and arrive straight back here, forever.
            self.store.log(
                run_id,
                "Nothing left for the loop to retry — what remains needs a human "
                "(claude-only tickets, or a failure no ticket in this backlog owns).",
                level="warn",
                kind="lifecycle",
            )
            return False

        # A cycle that ends with exactly the failures the last one ended with
        # has established that nothing in this arrangement varies: same spec,
        # same code, same objection. Another cycle spends a full backlog of
        # calls to reproduce it. This is the brake `-1` needs and a count
        # cannot provide — one ticket rewriting identical code and collecting
        # an identical rejection ran 37 attempts across a dozen cycles.
        #
        # Checked before the requeue: deciding to stop after resetting the
        # tickets would leave them pending behind a run reported as blocked,
        # and the next `forge go` would work them again anyway.
        fingerprint = self._evidence_fingerprint(run_id, eligible)
        if fingerprint == self.store.get_control(evidence_key(run_id), ""):
            self.store.log(
                run_id,
                "Stopping the automatic retries: this cycle failed in exactly "
                "the way the previous one did, on the same tickets. Nothing is "
                "varying, so another cycle would spend the same calls for the "
                "same result. Read the last rejection — the ticket needs a "
                "human, not another attempt.",
                level="error",
                kind="lifecycle",
            )
            return False
        self.store.set_control(evidence_key(run_id), fingerprint)

        # Respec first, requeue second. A cycle whose tickets came back
        # unchanged is a re-run of inputs that already failed, and the only
        # thing left varying is model sampling — so whether there is anything
        # to retry has to be known before the tickets are reset, not after.
        if self.config.loop.respec_on_retry:
            by_id = {ticket.ticket_id: ticket for ticket in tickets}
            # Requeueing a skipped ticket is right — it must run once its
            # dependency lands. Respec'ing one is not. It has no attempts, so
            # the only evidence is `dependency not met: TT-002`, and the planner
            # is handed that under a heading reading "what happened, oldest
            # attempt first" and told to revise the ticket so the next attempt
            # succeeds. It complies, because that is the only answer the schema
            # allows: three untried tickets had their human-authored specs
            # rewritten twice each, one of them acquiring a fabricated xorshift
            # constant and another a `lib.rs must contain exactly` clause that
            # contradicted the two tickets after it.
            attempted = [
                by_id[ticket_id]
                for ticket_id in eligible
                if by_id[ticket_id].attempts > 0
            ]
            revised, asked, parked = self._respec(run_id, attempted, notes)
            # A ticket the planner has just called unsatisfiable stays parked.
            eligible = [ticket_id for ticket_id in eligible if ticket_id not in parked]
            # Nothing ran this cycle, so there is nothing for a respec to have
            # learned from and no revision to require before going round again.
            # Requeueing the skipped work is the whole point of the cycle.
            if attempted and not revised:
                self.store.log(
                    run_id,
                    (
                        "Stopping the automatic retries: the respec changed nothing, "
                        "so another cycle would hand the executor the same ticket "
                        "that has already failed and hope the model samples "
                        "differently. Read the last rejection — when the planner "
                        "says the ticket is right, the disagreement is between the "
                        "executor and the reviewer, and no rewrite of the ticket "
                        "settles that."
                    )
                    if asked
                    else (
                        "Stopping the automatic retries: respecOnRetry is on but the "
                        "planner could not be reached, so every further cycle would "
                        "be a plain re-run of tickets that already failed."
                    ),
                    level="error",
                    kind="lifecycle",
                )
                return False

        requeued = self.store.reset_tickets(run_id, ticket_ids=eligible)
        if not requeued:
            return False
        # After the requeue, not before: a ticket only becomes stale once the
        # dependency it was judged against is actually going round again.
        self._reopen_stale(run_id)

        cycle = spent + 1
        label = f"{cycle}/{limit}" if limit > 0 else str(cycle)
        self.store.set_control(retries_key(run_id), str(cycle))
        self.store.log(
            run_id,
            f"Run ended {outcome}; starting automatic retry cycle {label} over "
            f"{len(requeued)} ticket(s).",
            level="warn",
            kind="lifecycle",
            data={"cycle": cycle, "limit": limit, "tickets": [t.ticket_id for t in requeued]},
        )

        self.store.set_run_status(run_id, RUN_RUNNING, f"retry cycle {label}")
        return True

    def _respec(
        self, run_id: int, tickets: list[Ticket], notes: dict[str, str]
    ) -> tuple[list[Ticket], bool, set[str]]:
        """Rewrite each ticket from why it failed. Never raises.

        Returns `(revised, asked, parked)`: the tickets whose text changed,
        whether the planner could be reached at all, and the ids it reported as
        unsatisfiable. The caller needs all three — a cycle over tickets that
        came back unchanged is a re-run of inputs that already failed, and one
        the planner never saw is not a respec at all.

        Best-effort per ticket. The calls go through `_call` like every other
        model call in the run, which is what makes an unattended cycle park on
        a rate limit rather than fail on one.
        """
        parked: set[str] = set()
        try:
            # Ask for what this planner can produce rather than assuming a
            # ceiling: a thinking model spends most of its budget before the
            # first character of the answer.
            budget = self._output_budget("planner")
        except (ConfigError, ProviderError, OSError, ValueError) as exc:
            self.store.log(
                run_id,
                f"The planner is unavailable, so nothing was re-specced ({exc}).",
                level="warn",
                kind="lifecycle",
            )
            return [], False, parked

        revised: list[Ticket] = []
        for ticket in tickets:
            result = respec.revise(
                self.store,
                run_id,
                ticket,
                notes.get(ticket.ticket_id, ""),
                call=lambda messages, limit: self._call(
                    run_id, "planner", messages, max_tokens=limit, temperature=0.0
                ),
                budget=budget,
                # The planner has no filesystem either. Rewriting a spec about
                # code it cannot see is how a ticket acquired a coordinate
                # convention the implementation had never used.
                sources=self._sources_for(ticket)[0],
                criteria_locked=not self.config.loop.respec_criteria,
                contradiction=self._contradictions.get(ticket.ticket_id),
                # So a revised read scope is checked against the tree rather
                # than taken on the planner's word for where a file lives.
                root=self.config.root,
            )
            if result.impossible:
                # Spending a full attempt budget on a ticket the planner has
                # just shown to be unsatisfiable is the most expensive way to
                # learn nothing.
                parked.add(ticket.ticket_id)
                ticket.status = TICKET_BLOCKED
                ticket.blocked_note = f"respec: {result.impossible}"
                self.store.update_ticket(run_id, ticket)
                continue

            # Respec asked to retire an assertion. It does not get to decide
            # that — it is the role whose job is making this ticket pass, and a
            # scope grant justified by the party that benefits is not a check.
            # The reviewer rules, and has to argue for it.
            if result.pending_scope and self._grant_contradicted_scope(
                run_id, ticket, result.pending_scope
            ):
                if ticket not in revised:
                    revised.append(ticket)
                continue

            if result.revised:
                revised.append(ticket)
                continue
            # The rationale is the whole content of this outcome. "Kept the
            # ticket as written" says the planner declined to act; only its
            # reasoning says whether that is "the spec is fine, the executor
            # simply did not finish" or "I could not tell what to change".
            self.store.log(
                run_id,
                f"{ticket.ticket_id}: respec left the ticket as written — "
                f"{result.rationale or result.note}",
                level="warn",
                kind="ticket",
                data={"note": result.note, "rationale": result.rationale},
            )

        if not revised:
            return revised, True, parked

        # The tickets on disk are what a human reads to understand the run. A
        # revision that lives only in the database makes those files lie —
        # but only the revised ones are rewritten, so the count in the log is
        # the number of tickets respec actually changed.
        try:
            write_tickets(self.config.tickets_dir, revised)
        except OSError as exc:
            self.store.log(
                run_id,
                f"Could not rewrite the ticket files after respec ({exc}); "
                f"the database holds the revised specs either way.",
                level="warn",
                kind="lifecycle",
            )
        return revised, True, parked

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

        # Before anything is spent. A ticket in a language nothing runs cannot
        # be checked, and the loop's answer to that used to be to run it
        # anyway and report the skip as routine.
        uncovered = self._uncovered_languages(ticket)
        if uncovered:
            self._park(run_id, ticket, self._no_runner_note(ticket, uncovered))
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
        # Captured the first time this ticket runs and kept from then on. A
        # retry cycle starts with the previous cycle's implementation still in
        # the tree, so a fresh snapshot here would take that work as the
        # starting point: the executor rewrites it byte for byte, git reports
        # nothing, and the reviewer is asked to approve a change it cannot see.
        # It refuses — correctly, on the evidence — twenty-eight times in one
        # run, over nine cycles, for a ticket whose implementation was fine.
        if not ticket.baseline_tree:
            ticket.baseline_tree = self._snapshot()
            self.store.update_ticket(run_id, ticket)
        baseline = ticket.baseline_tree

        # Taken once per ticket, for the same reason as the snapshot: this is
        # the state the ticket inherited, and every attempt is judged against
        # it. Re-running it per attempt would also fold the ticket's own
        # half-finished work into what counts as "already broken".
        # A bug ticket's reproduction is settled before anything is verified,
        # because the baseline has to know about that file: on a retry cycle it
        # is already on disk and already failing, and amnesty for it would let
        # the ticket pass with the bug still there.
        repro: tuple[str, str] | None = None
        if ticket.kind == TICKET_BUG:
            repro_path, why_not = self._repro_target(ticket)
            if not repro_path:
                self._park(run_id, ticket, why_not)
                return
        else:
            repro_path = ""

        pre_existing = self._inherited_failures(run_id, ticket, repro_path)

        # The baseline has just run this project's own commands. One of them
        # not starting is not this ticket's defect and not any ticket's, so the
        # ticket goes back on the backlog untouched and `run` ends the run
        # before a single delegation is spent arguing with a model that is
        # right. See `_stop_for_toolchain`.
        if self._toolchain:
            ticket.status = TICKET_PENDING
            self.store.update_ticket(run_id, ticket)
            return

        if ticket.kind == TICKET_BUG:
            # Proof is durable, and it has to be: once the fix lands the test
            # passes, so a second cycle re-running reproduction would find
            # nothing wrong and park a ticket whose work is nearly done.
            proof = self.store.reproduced(run_id, ticket.ticket_id)
            if not proof:
                proof = self._prove(run_id, ticket, repro_path)
                if not proof:
                    return
            repro = (repro_path, proof)
            # The failure the executor is being asked to clear. Stated as
            # evidence rather than left for it to infer from the spec: the
            # first thing it needs to know is what the test actually reported.
            failure_context = (
                f"The bug is reproduced by `{repro_path}`, which fails against the "
                f"code as it stands. Your fix is not done until it passes, and you "
                f"cannot edit it — it is outside this ticket's scope:\n\n{proof}"
            )

        # Test files this ticket created, and whether it created them rather
        # than overwriting something that was already there. Unverified ones
        # are removed if the ticket never passes.
        authored: dict[str, bool] = {}
        # Every implementation file this ticket has landed, across all of its
        # attempts. Read only when it gives up, to put the tree back the way it
        # found it. See `_quarantine`.
        touched: set[str] = set()
        # Everything that has already failed on this ticket, and every verdict
        # the reviewer has already given it. `failure_context` alone carries
        # only the newest one, which is what lets an executor oscillate — fix A
        # breaks B, fix B breaks A — for its whole retry budget without
        # anything noticing the cycle.
        history: list[str] = []
        rejections: list[str] = []

        # Seeded from the step log on a retry cycle. Both lists are locals, and
        # a cycle enters here fresh — so cycle 2's reviewer met a ticket it had
        # already rejected three times as though for the first time, re-raised
        # the same objections, and the "a rejection that repeats means the spec
        # is wrong" nudge never fired, because `prior_verdicts` was empty
        # exactly when it mattered. Same for the executor and its own failures.
        if ticket.attempt_base:
            history = [
                f"Earlier cycle, {item['name']} failed:\n{item['detail']}"
                for item in self.store.ticket_failures(
                    run_id, ticket.ticket_id, limit=self._PRIOR_FAILURES
                )
            ]
            # Stripped on the way in for the same reason the live list is: this
            # goes straight into the next reviewer's prompt, and a verdict
            # carrying the prompt's own headings offers them back for copying.
            rejections = [
                strip_prompt_echo(verdict)
                for verdict in self.store.ticket_rejections(
                    run_id, ticket.ticket_id, limit=self._PRIOR_VERDICTS
                )
            ]

        while ticket.attempts < self.config.loop.max_attempts:
            ticket.attempts += 1
            self.store.update_ticket(run_id, ticket)

            outcome = self._attempt(
                run_id, ticket, failure_context, retrieved, baseline,
                pre_existing=pre_existing, authored=authored, touched=touched,
                prior_failures=history[-self._PRIOR_FAILURES:],
                rejections=rejections,
                repro=repro,
            )

            # Checked before `blocked`, and it must be: the note names the
            # files the tree is red on, and `_widen_scope` reads a block note
            # for exactly that. Granting them here would hand this ticket
            # somebody else's broken file and call it scope.
            if outcome.halt:
                self._discard_tests(run_id, ticket, authored)
                self._quarantine(run_id, ticket, touched)
                ticket.status = TICKET_BLOCKED
                ticket.blocked_note = outcome.detail
                self.store.update_ticket(run_id, ticket)
                self._halt = outcome.detail
                return

            if outcome.blocked:
                # The executor's own words, read back before the block is
                # spent. It was told `BLOCKED:` "can widen the ticket" — see
                # `_scope_feedback` — and until now nothing made that true.
                if self._widen_scope(run_id, ticket, outcome.detail):
                    # The attempt produced nothing to judge, so it is not
                    # charged: the executor was asking a question, and it now
                    # has the answer.
                    ticket.attempts -= 1
                    self.store.update_ticket(run_id, ticket)
                    continue

                self._discard_tests(run_id, ticket, authored)
                self._quarantine(run_id, ticket, touched)
                ticket.status = TICKET_BLOCKED
                ticket.blocked_note = outcome.detail
                self.store.update_ticket(run_id, ticket)
                self.store.log(
                    run_id,
                    f"{ticket.ticket_id}: BLOCKED — {outcome.detail[:400]}",
                    level="warn",
                    kind="ticket",
                )
                # After the revert, not before: what matters is whether the
                # tree is still red once this ticket's work is out of it.
                self._red_left_behind(run_id, ticket)
                if self.config.loop.stop_on_blocked:
                    raise Stopped()
                return

            if outcome.ok:
                ticket.status = TICKET_DONE
                # Record what it passed on top of. Without this the pass is
                # undated: a later respec can rewrite a dependency's contract
                # and nothing knows this ticket was judged against the old one.
                ticket.dep_stamp = self._dep_stamp(run_id, ticket)
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
        self._quarantine(run_id, ticket, touched)
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
        # Asked here rather than left for the next ticket to discover halfway
        # through its own verify step, which is a full attempt spent to learn
        # something that was already true.
        self._red_left_behind(run_id, ticket)
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
    # Earlier rejections shown to the reviewer, for the same reason and with
    # the same ceiling. Uncapped, this was the one block that grew with every
    # attempt: three is enough to show an objection repeating, which is the
    # signal the block exists for.
    _PRIOR_VERDICTS = 3

    def _grant_contradicted_scope(
        self, run_id: int, ticket: Ticket, wanted: list[str]
    ) -> bool:
        """Let the reviewer argue for retiring a stale assertion. True if granted.

        The one place the loop hands a ticket write access to a test. Everything
        about how it is done is chosen so the grant is auditable afterwards:

        - The **reviewer** rules, not the planner that asked. Respec's job is to
          make a failing ticket pass, so it is exactly the wrong role to also
          decide that the assertion blocking it is wrong.
        - It must **argue**, not assert. A `GRANT:` that never names the file or
          runs to two lines is recorded as a refusal — see `parse_scope_argument`.
          "The ticket cannot pass otherwise" is true of every contradiction and
          settles none of them.
        - The argument is written to the run log **verbatim**, whichever way it
          goes, because the thing a person will want later is not that scope
          changed but why somebody thought the old assertion was wrong.

        A refusal leaves the ticket parked with the contradiction in its note,
        which is the honest end: two demands disagree and nothing here could
        tell which is right.
        """
        report = ""
        run = self.store.get_run(run_id)
        if run is not None:
            report = run["source"] or ""
        blamed = self._contradictions.get(ticket.ticket_id, {})
        repro_path, _ = self._repro_target(ticket)
        granted: list[str] = []

        for path in wanted:
            sources = self._sources_for(ticket, extra=[path, repro_path])[0]
            try:
                completion = self._call(
                    run_id,
                    "reviewer",
                    scope_argument_prompt(
                        ticket,
                        report,
                        test_path=path,
                        test_source=sources.get(path, ""),
                        blamed=blamed.get(path, []),
                        repro_path=repro_path,
                        repro_source=sources.get(repro_path, ""),
                    ),
                    max_tokens=self._output_budget("reviewer"),
                    temperature=0.0,
                )
            except (ContextOverflow, ProviderError) as exc:
                self.store.log(
                    run_id,
                    f"{ticket.ticket_id}: could not ask the reviewer whether "
                    f"{path} should be retired ({exc}); it stays out of scope.",
                    level="warn",
                    kind="ticket",
                )
                continue

            ok, argument, why_not = parse_scope_argument(completion.text, path)
            if not ok:
                self.store.log(
                    run_id,
                    f"{ticket.ticket_id}: the reviewer would not have {path} "
                    f"retired"
                    + (f" — {why_not}" if why_not else "")
                    + f". Scope unchanged. What it said:\n{argument[:1200]}",
                    level="warn",
                    kind="ticket",
                    data={"ticket": ticket.ticket_id, "path": path, "why_not": why_not},
                )
                continue

            granted.append(path)
            self.store.log(
                run_id,
                f"{ticket.ticket_id}: the reviewer argued that {path} asserts "
                f"the behavior this report calls a bug, and it was granted to "
                f"the ticket to retire. This is the loop editing a test it is "
                f"judged by — read the argument:\n{argument[:2000]}",
                level="warn",
                kind="ticket",
                data={"ticket": ticket.ticket_id, "path": path, "argument": argument},
            )

        if not granted:
            return False

        ticket.allowed_files = list(ticket.allowed_files) + granted
        self.store.update_ticket(run_id, ticket)
        return True

    def _contradicting_tests(
        self,
        ticket: Ticket,
        repro: tuple[str, str] | None,
        output: str,
        already: set[str] | None = None,
    ) -> dict[str, list[str]]:
        """Test files outside this ticket's scope that assert the reported bug.

        The failure this exists for is the project's founding problem in its
        purest form. An earlier ticket wrote both an implementation and the
        assertion it is judged by, and encoded the bug in the assertion:

            assert_eq!(piece::color(kind), (kind as u8) + 1);   // color(0) == 1

        A bug report later says `color(0)` should be `255`. Both cannot hold.
        The fix lands, the reproduction passes, and the suite fails on a file
        the ticket may not touch — so the attempt is scored as a failure and
        the executor is asked again, five times, for an edit that cannot exist.

        Four conditions, all necessary. This is a **bug** ticket, so there is a
        reproduction to be the contract. That reproduction **passes**, or the
        fix is simply not working yet and this is nothing special. What fails is
        a **test file outside the ticket's scope** — a broken source file is an
        ordinary regression the executor should fix, and treating it as a
        contradiction would turn this into a way to widen scope by breaking
        things. And the failure is one this ticket **introduced**, which
        `already` decides.

        That last condition was missing on the first run of this code and it
        matters more than it looks. Without it, every red file in the repository
        reads as being about whichever ticket is in hand: a bug about the game's
        starting *level*, scoped to `src/game.rs`, was reported as contradicted
        by `tests/tt_001_test.rs` — an assertion about piece geometry that had
        been failing since before the ticket was filed. The ticket then blocked
        on a contradiction that did not exist, one line under an amnesty log
        saying those errors pre-dated it.
        """
        if repro is None:
            return {}
        blamed = files_blamed(output, exclude=already)
        # Whether the reproduction is among the *failures*, which is not the
        # same question as whether the output mentions it. `errors_naming`
        # answers the looser one and matches cargo's `Running tests\bug_002_
        # test.rs` banner, which every run prints whether the test passed or
        # not — so asking it here found the reproduction in its own success
        # line and concluded the fix was not working.
        repro_key = normalize_path(repro[0])
        if any(normalize_path(path) == repro_key for path in blamed):
            return {}
        writable = {normalize_path(path) for path in ticket.allowed_files}
        writable.add(repro_key)
        found: dict[str, list[str]] = {}
        for path, lines in blamed.items():
            if normalize_path(path) in writable:
                continue
            if not self._is_test_path(path):
                continue
            if not (self.config.root / path).is_file():
                continue
            found[path] = lines[:6]
        return found

    def _contradiction_note(
        self, ticket: Ticket, repro: tuple[str, str] | None, contradicted: dict[str, list[str]]
    ) -> str:
        """The block note for a ticket no edit in its scope can satisfy.

        Written for the two readers it has: a human deciding which assertion is
        right, and the respec that runs before the next cycle. Both need the
        same thing — the two demands side by side, and where each one lives.
        """
        lines = [
            f"BLOCKED: this ticket cannot be satisfied within its scope, and "
            f"another attempt would not change that.",
            "",
            f"The fix works. The reproduction "
            + (f"`{repro[0]}` " if repro else "")
            + "passes against it.",
            "",
            "What fails is an assertion this ticket may not write:",
        ]
        for path, blamed in contradicted.items():
            lines.append(f"  - {path}")
            for entry in blamed:
                lines.append(f"      {entry}")
        lines += [
            "",
            "That assertion states the behavior this report calls a bug, so it "
            "and the reproduction are direct opposites. One of them is wrong.",
            "",
            "If the report is right, the assertion is stale and has to be "
            "retired — which is a decision about the project's contract, not an "
            "edit, and is settled at respec rather than by whoever is holding "
            "the ticket. If the assertion is right, the report is wrong and the "
            "ticket should be closed.",
        ]
        return "\n".join(lines)

    def _widen_scope(self, run_id: int, ticket: Ticket, note: str) -> list[str]:
        """Grant a blocked ticket the file it named, once. Returns what it got.

        The executor is told that `BLOCKED:` "names the file you need ... and
        can widen the ticket". That was half true: the note reached a human,
        and nothing widened anything. So a ticket whose scope was one file too
        narrow parked, and the sentence naming the missing file — `the Game
        struct ... is likely defined in src/game.rs ... outside the allowed
        scope I'm permitted to modify` — sat in the block note being read by
        nobody. That run had already reproduced the bug.

        What makes this safe to do automatically is that it is not negotiation.
        The file has to already exist in the repository, so a model cannot
        invent its way into scope, and `neverDelegate` is checked exactly as it
        is everywhere else — a path a human has placed off-limits is refused
        here too, and the ticket parks with that stated. What is granted is a
        file the project already contains, to a ticket that has stopped.

        Once per ticket per run. A second grant would be the loop bargaining
        with itself, and a ticket that blocks again after being given what it
        asked for is telling a human something real.
        """
        if ticket.ticket_id in self._widened:
            return []

        named = evidence.paths_named(self.config.root, note)
        writable = {normalize_path(path) for path in ticket.allowed_files}
        wanted = [path for path in named if normalize_path(path) not in writable]
        if not wanted:
            return []

        # A test file is never granted by asking. The executor wanting write
        # access to the assertion it is being judged by is the exact move the
        # reproduce-first design exists to prevent, and it does not become safe
        # because the request was phrased as a block. Retiring an assertion is
        # settled at respec, by a reviewer that has to argue for it — see
        # `_argue_for_scope`.
        refused = [
            path
            for path in wanted
            if matches_any(path, self.config.never_delegate)
            or self._is_test_path(path)
        ]
        granted = [path for path in wanted if path not in refused]
        if refused:
            self.store.log(
                run_id,
                f"{ticket.ticket_id}: asked for {', '.join(refused)}, which is "
                f"either off-limits at the project level or a test file; "
                f"refused. neverDelegate is a decision a human made and the "
                f"loop does not revisit it, and no ticket writes the assertion "
                f"it is judged by just because it asked to.",
                level="warn",
                kind="ticket",
            )
        if not granted:
            return []

        self._widened.add(ticket.ticket_id)
        ticket.allowed_files = list(ticket.allowed_files) + granted
        ticket.reference_files = evidence.reading_scope(
            self.config.root, ticket.allowed_files, ticket.reference_files
        )
        self.store.update_ticket(run_id, ticket)
        self.store.log(
            run_id,
            f"{ticket.ticket_id}: blocked asking for {', '.join(granted)}, "
            f"which exists in this repository and is not off-limits; granted "
            f"and retried without spending the attempt. Scope is now "
            f"{', '.join(ticket.allowed_files)}.",
            level="warn",
            kind="ticket",
            data={"ticket": ticket.ticket_id, "granted": granted, "note": note[:2000]},
        )
        return granted

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
        touched: set[str] | None = None,
        prior_failures: Sequence[str] = (),
        rejections: list[str] | None = None,
        repro: tuple[str, str] | None = None,
    ) -> StepResult:
        """One attempt at a ticket: build, apply, test, verify, review.

        `repro` marks this as a bug ticket whose reproduction is already on
        disk, as `(path, the failure it produced)`. It changes two things. No
        tests are authored — the contract was written before the fix and asking
        for more now would let the party being judged add to it — and the
        reviewer is shown what failed before, so it judges the fix against the
        fault rather than against a green suite.
        """
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

        # Rebuilt from the step log on every call, so the transport stays
        # stateless and a retry cycle inherits the thread — the daemon's state
        # machine remains the only state machine. Empty unless the flag is set,
        # which keeps the flat prompt the default.
        prior_turns = (
            self.store.ticket_turns(
                run_id, ticket.ticket_id, limit=self.config.loop.executor_turns
            )
            if self.config.loop.executor_turns
            else []
        )

        step_id = self.store.start_step(run_id, ticket.ticket_id, "build")
        # A reply that did not parse into files is a formatting mistake, not a
        # failed implementation, and spending a whole attempt on one buys
        # nothing: the next attempt re-reads the same spec and the model makes
        # the same mistake. One ticket lost six of its nine attempts that way,
        # every one to a fenced block with no path line above it, while the
        # three that parsed drew specific and answerable review objections it
        # never got the budget to address.
        #
        # So the reply is refused and asked for again inside the attempt, the
        # way the tester already reprompts a rejected test file. Once only —
        # a model that cannot follow the format twice will not follow it on the
        # third ask, and the attempt should end while the evidence is fresh.
        malformed = ""
        for remaining in (1, 0):
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
                        malformed=malformed,
                        prior_turns=prior_turns,
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

            # A response cut off at the output limit parses cleanly — the fence
            # the model opened is simply never closed, or closes around half a
            # function. Applying it writes a file that is syntactically wrong
            # for a reason no reviewer would guess from the diff, so nothing is
            # written and the attempt is spent instead. Not reprompted: the
            # second call gets the same budget and runs out of it the same way.
            if completion.truncated:
                detail = (
                    "Your previous response was cut off at the output limit, so "
                    "no files were written. Emit the same implementation in "
                    "fewer output tokens — fewer files per response, no "
                    "restated context — or reply BLOCKED: if the ticket cannot "
                    "be implemented within that budget."
                )
                self.store.end_step(step_id, "failed", completion.text[:20000])
                return StepResult(ok=False, detail=detail)

            parsed = parse_output(completion.text)
            malformed = self._malformed_reply(parsed, completion.text)
            if not malformed or not remaining:
                break
            self.store.log(
                run_id,
                f"{ticket.ticket_id}: the executor's reply did not parse into "
                f"files; asking again before the attempt is spent.",
                level="warn",
                kind="ticket",
                data={"complaint": malformed[:400]},
            )

        self.store.end_step(step_id, "ok", completion.text[:20000])

        if parsed.is_blocked:
            return StepResult(ok=False, blocked=True, detail=parsed.blocked_reason)

        # Still unreadable after being told exactly what was wrong with it —
        # which for a local model is the common case rather than the rare one,
        # because the format is usually not what it got wrong.
        if malformed:
            recovered = self._recover_unlabeled(run_id, ticket, completion.text)
            if recovered is None:
                return StepResult(ok=False, detail=malformed)
            parsed, malformed = recovered, ""

        # Everything that could not be read has already been refused above. What
        # is left is a reply that parsed — possibly into no files at all, which
        # is not the same thing. A ticket whose work is already on disk has
        # nothing to write, and the executor is shown the current files, so
        # "there is nothing to change" is sometimes the honest answer. Spending
        # an attempt punishing it is how a finished ticket failed three times a
        # cycle.
        wrote_nothing = parsed.is_empty

        # --- APPLY ---------------------------------------------------
        written: list[str] = []
        scope_note = ""
        if not wrote_nothing:
            scoped = enforce_scope(
                parsed, ticket.allowed_files, self.config.never_delegate
            )
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
                    detail=self._scope_guidance(
                        ticket, scoped.rejected, total_loss=True
                    ),
                )

            step_id = self.store.start_step(run_id, ticket.ticket_id, "apply")
            try:
                written = apply_edits(self.config.root, scoped.edits)
            except ValueError as exc:
                self.store.end_step(step_id, "failed", str(exc))
                return StepResult(ok=False, blocked=True, detail=str(exc))
            self.store.end_step(step_id, "ok", "\n".join(written))
            # Accumulated across every attempt, because quarantine happens
            # once the ticket has given up and by then the attempt that wrote a
            # file may be three failures ago. Recorded from `apply_edits`
            # rather than derived from a diff: these are the literal paths that
            # landed, so the revert needs no glob expansion and cannot reach a
            # file this ticket never wrote. See `_quarantine`.
            if touched is not None:
                touched.update(written)
            self._record_step(
                ticket,
                "apply",
                "ok",
                {"written": written, "rejected": scoped.rejected},
            )
            # A partial rejection used to be logged and then dropped. The
            # executor saw a successful apply, lint failed on the piece that
            # never landed, and it had no way to connect the two — so it re-sent
            # the same edit every attempt. Carry the rejection forward as
            # verification evidence.
            scope_note = (
                self._scope_guidance(ticket, scoped.rejected, total_loss=False)
                if scoped.rejected
                else ""
            )

        # The clean files are on disk now, so the next attempt only has to send
        # the ones that were cut short. Stopping here rather than carrying on to
        # review: the response is known to be incomplete, and asking a reviewer
        # to judge a diff the harness already knows is missing a file spends two
        # model calls to be told so.
        if parsed.truncated:
            return StepResult(
                ok=False, detail=self._fence_guidance(parsed.truncated, written)
            )

        # --- TESTS ---------------------------------------------------
        # The criteria come from the ticket, never from the executor's own
        # suggestion. A model that writes both the code and the assertion it is
        # judged against will encode its bugs as passing tests.
        # Suffix first, example second. The suite decides which language it is
        # written in; the example only shows how. Reading the language off
        # whichever example turned up first is what let one `.js` file in a
        # Rust repo disable test authoring for the whole backlog.
        if repro is not None:
            # The contract for a bug ticket was written before the fix was
            # attempted, by a role that could not see it. Authoring more tests
            # here would let the attempt add to the standard it is judged
            # against, which is the rule the loop already enforces everywhere
            # else — and the reproduction is the standard.
            test_path, no_tests_because = repro[0], ""
            suffix, example = "", None
        else:
            suffix = self._suite_suffix(written, exclude=written)
            example = self._example_test(written, suffix)
            test_path, no_tests_because = self._test_target(
                ticket, written, example, suffix
            )
        if wrote_nothing and repro is None:
            # Nothing was written this attempt, so there is no new behavior to
            # assert against — and authoring a file here would put a test on
            # disk for an attempt that changed nothing. Whatever this ticket
            # wrote earlier is still present and still runs at verify.
            test_path, no_tests_because = "", (
                "the attempt wrote no files, so there is nothing new to assert "
                "against"
            )
        if test_path and repro is None:
            # On a retry the ticket's own test file is already on disk, and a
            # fixed path makes that the common case rather than the rare one.
            # Handing it back as "the convention this repo follows" would
            # launder the previous attempt's mistakes into a rule.
            example = self._example_test(written + [test_path], suffix)
        if repro is not None:
            # Counted as covered whatever its criteria say: a bug ticket has a
            # test that failed and then passed, which is the strongest coverage
            # any ticket in this loop can have.
            self._tests_authored.add(ticket.ticket_id)
        if ticket.criteria and test_path and repro is None:
            self._tests_authored.add(ticket.ticket_id)
        if ticket.criteria and no_tests_because and repro is None:
            self._tests_skipped.add(ticket.ticket_id)
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
        if ticket.criteria and test_path and repro is None:
            step_id = self.store.start_step(run_id, ticket.ticket_id, "tests")
            existed = (self.config.root / test_path).exists()
            try:
                rejected_bindings: list[str] = []
                for remaining in (1, 0):
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
                        test_command=self.config.command_for("test", test_path),
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
                            rejected_bindings=rejected_bindings,
                            # Pointed at rather than left to be noticed. The
                            # tester is the only role that can edit this file,
                            # and a style error in it fails the ticket for as
                            # long as the tester keeps reproducing it — which
                            # it will, because a failure naming its own file
                            # reads like evidence about the implementation.
                            own_file_errors=errors_naming(
                                failure_context, test_path
                            ),
                        ),
                        max_tokens=self._output_budget("tester"),
                        temperature=0.1,
                    )
                    # Half a test file is worse than no test file: it fails
                    # verify on a syntax error, which reads as the
                    # implementation being broken. Discard it and let review
                    # check the criteria instead — the same trade the handler
                    # below already makes.
                    if completion.truncated:
                        raise ValueError("tester hit its output limit; partial tests discarded")
                    # Exactly one path, and not the ticket's source files
                    # either. The previous allowlist was every test-shaped path
                    # in the repository, which let one ticket's tester scatter
                    # six files across three attempts and let it overwrite the
                    # very implementation it was supposed to be judging.
                    test_parsed = enforce_scope(
                        parse_output(completion.text),
                        [test_path],
                        self.config.never_delegate,
                    )
                    # A test that re-declares its subject with `extern` or
                    # `dlopen` does not fail an assertion, it fails to link —
                    # and takes every other test in the target down with it.
                    # TESTER_SYSTEM already forbids this; a small local model
                    # did it anyway, seven unresolved symbols at a time. So it
                    # is rejected here and asked for once more, with what was
                    # wrong quoted back.
                    rejected_bindings = [
                        line
                        for edit in test_parsed.edits
                        for line in foreign_bindings(edit.content)
                    ]
                    if not rejected_bindings:
                        break
                    self.store.log(
                        run_id,
                        f"{ticket.ticket_id}: tester declared the code under test "
                        f"as a foreign binding "
                        f"({'; '.join(rejected_bindings)[:200]}); "
                        + ("asking again." if remaining else "discarding the tests."),
                        level="warn",
                        kind="ticket",
                    )
                    if not remaining:
                        raise ValueError(
                            "tester kept declaring the code under test as a "
                            "foreign binding; tests discarded rather than "
                            "breaking the link for every other ticket"
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
        # Which steps actually ran an assertion about this ticket, and which
        # only reported somebody else's breakage. A step in the second list is
        # not evidence, and a ticket with nothing in the first has been checked
        # by nothing at all — see `_unverifiable`.
        proved: list[str] = []
        excused: list[str] = []
        excused_output = ""
        for name, command in self._verify_plan():
            result = self._shell(run_id, name, command)
            # Checked here too, not only at the baseline: `baselineVerify` can
            # be off, and a toolchain can break mid-run when a ticket edits the
            # file that configures it.
            self._note_toolchain(name, command, result)
            already = inherited.get(name, set())
            introduced = signatures(result.detail) - already if already else set()
            if not result.ok:
                # Charged whether or not a baseline existed for this step, and
                # whether or not the attempt goes on to pass review. These are
                # the errors standing while this ticket was the one running, and
                # the loop is single-threaded — no other ticket can have written
                # them. Recording them here is what stops the next cycle's
                # baseline from handing them back as somebody else's problem.
                self._charge(run_id, ticket, signatures(result.detail) - already)
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
                proved.append(name)
                continue

            # Everything this step is complaining about was already broken when
            # the ticket started, so it is not this ticket's to fix — and very
            # likely not in its scope to fix either. Passing the step is what
            # stops one abandoned file from failing an entire backlog.
            if already and not introduced:
                # One thing is never excused on a bug ticket: its own
                # reproduction. On a second cycle that test is already on disk
                # and already failing, so it pre-dates the attempt by every
                # measure the amnesty uses — and excusing it would pass the
                # ticket with the bug it was filed for still in place. Checked
                # textually because the signature parser needs a location the
                # tool may not print.
                still_failing = (
                    errors_naming(result.detail, repro[0]) if repro else []
                )
                if not still_failing:
                    self.store.log(
                        run_id,
                        f"{ticket.ticket_id}: {name} still failing, but only on "
                        "errors that pre-date this ticket; not counted against it.",
                        level="warn",
                        kind="verify",
                        data={"step": name},
                    )
                    excused.append(name)
                    excused_output = excused_output or result.detail
                    continue
                self.store.log(
                    run_id,
                    f"{ticket.ticket_id}: {name} is failing on the reproduction "
                    f"itself, which pre-dates this attempt and is exactly what "
                    f"the ticket exists to clear; not excused.",
                    level="warn",
                    kind="verify",
                    data={"step": name, "reproduction": repro[0]},
                )

            # A bug ticket whose fix works and whose suite still fails, because
            # an older test asserts the behavior the report calls a bug. Worth
            # separating from an ordinary regression before the executor is
            # asked again: there is no edit inside this scope that satisfies
            # both, and asking five times produces five attempts at squaring a
            # circle. It did — one ticket oscillated for five attempts and then
            # reported "gave up after 5 attempts", which reads as a fix nobody
            # could write rather than a contract nobody can satisfy.
            contradicted = self._contradicting_tests(
                ticket, repro, result.detail, already
            )
            if contradicted:
                self._contradictions[ticket.ticket_id] = contradicted
                self.store.log(
                    run_id,
                    f"{ticket.ticket_id}: the reproduction passes, so the fix "
                    f"works — but {', '.join(contradicted)} asserts the "
                    f"behavior this ticket was filed to change, and is outside "
                    f"its scope. Nothing the executor may write satisfies both.",
                    level="warn",
                    kind="verify",
                    data={"ticket": ticket.ticket_id, "contradicted": contradicted},
                )
                return StepResult(
                    ok=False,
                    blocked=True,
                    detail=self._contradiction_note(ticket, repro, contradicted),
                )

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

        # Every step the project has was red, and every one of them was excused.
        # The ticket is about to go to review having had nothing compiled and
        # nothing run. `_unverifiable` decides whether that is a run-ending
        # state or an ordinary backlog mid-flight.
        if excused and not proved:
            unverifiable = self._unverifiable(run_id, ticket, excused, excused_output)
            if unverifiable:
                return StepResult(ok=False, halt=True, detail=unverifiable)

        # --- REVIEW --------------------------------------------------
        # The ticket's own files, plus the test file written on its behalf —
        # which the plan did not list but the review is expected to see.
        diff = self._diff(baseline, [*ticket.allowed_files, test_path])
        # A ticket can pass verification having changed nothing — because the
        # work was already on disk, or because it rewrote a file byte for byte.
        # Handed `(empty diff)` and nothing else, a reviewer has no way to tell
        # that from "the files were never written", and it says so: one real
        # verdict read "No build.sh, build.ps1, README.md exist", about a repo
        # where all three did. Show it the state when there is no change.
        state: dict[str, str] = {}
        if not diff.strip():
            state = self._sources_for(ticket)[0]

        # Files this attempt wrote that git reports as unchanged, because what
        # was written matched what was already there. On a retry that is most
        # of the ticket: the previous cycle's implementation is still on disk
        # (autoCommit is off, and quarantine only reaches a ticket whose
        # baseline tree could be read), the
        # executor reproduces it exactly, and only the discarded test file
        # shows up as new. A reviewer handed that diff says the implementation
        # is missing and rejects — every attempt, every cycle, forever.
        invisible = self._written_but_unchanged(written, diff)
        if invisible:
            self.store.log(
                run_id,
                f"{ticket.ticket_id}: {len(invisible)} file(s) were rewritten "
                f"exactly as they already were, so they are absent from the "
                f"diff; showing the reviewer their contents instead.",
                kind="review",
                data={"files": sorted(invisible)},
            )

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
                    prior_verdicts=list(rejections or [])[-self._PRIOR_VERDICTS :],
                    state=state,
                    unchanged=invisible,
                    reproduced=repro,
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
                # Stripped on the way in, not on the way out: this list is
                # quoted into the next attempt's prompt, and a verdict carrying
                # the prompt's own headings gets offered back for copying again.
                # The raw completion stays in `steps.detail` either way — that
                # is the durable record.
                rejections.append(strip_prompt_echo(verdict))
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
    # What counts as a test file anywhere in the loop: which files the planner
    # already designated, which one the tester should imitate, what the suite is
    # written in, and what the executor may never be granted.
    #
    # These were the snake-case spellings only — `test_x`, `x_test`, `x.test` —
    # plus a `tests/` or `test/` directory at the repository root. Every one of
    # those is a Rust, Go, Python, or JavaScript convention. A Gradle project
    # keeps `VideoExtensionsTest.java` under `src/test/java/`, which matched
    # nothing here, so a test file the *plan* had already named was invisible:
    # `_test_target` fell through to inventing `tests/pn_001_test.java`, a path
    # no JVM build system compiles. The tester's whole output was written,
    # never compiled, never run, and every ticket passed a suite that had
    # silently excluded it.
    #
    # `src/test/**` covers the Maven and Gradle layout whatever the file is
    # called, and the `*Test` / `*Tests` / `*Spec` spellings cover JVM, .NET,
    # and the spec-style runners. Widening this is safe in the one place it
    # deletes files — `_owned_test_files` gates on an exact ticket-derived
    # stem, so a wider search finds the same ticket's file in more places
    # rather than finding more files.
    # Candidates to scan for on disk. Deliberately over-broad — `fnmatch` folds
    # case on Windows and not on Linux, so no glob can tell `VideoExtensionsTest`
    # from `latest`. Every decision is made by `_is_test_path` instead; these
    # only decide which files are worth looking at, and a false positive here
    # costs one `stat`.
    _TEST_GLOBS = (
        "tests/**/*", "test/**/*", "spec/**/*", "src/test/**/*",
        "**/test_*.*", "**/*_test.*", "**/*.test.*",
        "**/*Test.*", "**/*Tests.*", "**/*Spec.*", "**/*_spec.*", "**/*.spec.*",
    )

    # A directory whose name says everything under it is a test. `src/test/java`
    # and `src/test/kotlin` are reached by the bare `test` segment, so the Maven
    # and Gradle layout needs no rule of its own.
    _TEST_DIRS = frozenset({"test", "tests", "spec", "specs", "testing"})

    # snake_case and dotted conventions: pytest, go, rust, rspec, jest.
    # Case-insensitive because none of them depend on capitalisation.
    _SNAKE_TEST = re.compile(r"^test_|_test$|_spec$|\.test$|\.spec$", re.IGNORECASE)

    # PascalCase conventions: JUnit, NUnit, xUnit, Kotest, ScalaTest.
    # Case-SENSITIVE, and the capital is the whole point — `VideoExtensionsTest`
    # is a test and `latest` is not, and lowercasing the two makes them the same
    # string. The preceding character must be lowercase or a digit so a word
    # merely *containing* the letters cannot qualify: `Testimonials` does not
    # end in `Test`, and `contest` has no capital to anchor on.
    _CAMEL_TEST = re.compile(r"(?:^|[a-z0-9])(?:Test|Tests|Spec|Specs)$")

    # Languages where the filename is not decoration: the public type has to be
    # declared in a file named after it, so `pn_001_test.java` cannot hold
    # `Pn001Test` and will not compile whatever directory it sits in.
    _TYPE_NAMED_SUFFIXES = frozenset({".java", ".kt", ".scala", ".groovy", ".cs"})

    # Where a build expects tests when the repository has none yet to copy. The
    # Maven layout, which Gradle also adopts, is the only one universal enough
    # in its ecosystem to assume; everything else gets `tests/`.
    _TEST_ROOTS = {
        ".java": "src/test/java",
        ".kt": "src/test/kotlin",
        ".scala": "src/test/scala",
        ".groovy": "src/test/groovy",
    }
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

    @classmethod
    def _is_test_path(cls, path: str) -> bool:
        """Whether this path is a test file, in any language's spelling.

        The single answer to a question the loop asks in five places: which
        files the plan already designated as tests, which one the tester should
        imitate, what the suite is written in, what the executor may never be
        granted, and which files can contradict a bug report.

        It used to be `matches_any(path, _TEST_GLOBS)`, and those globs held
        only the snake_case conventions — `test_x`, `x_test`, `x.test` — plus a
        `tests/` directory at the repository root. A Gradle project keeps
        `VideoExtensionsTest.java` under `src/test/java/`, which matched none of
        them, so a test file the plan had already named was invisible and
        `_test_target` invented `tests/pn_001_test.java` instead — a path no JVM
        build compiles. The tester's output was written, never compiled, never
        run, and every ticket passed a suite that had silently excluded it.

        Globs cannot replace this. `fnmatch` folds case on Windows and not on
        Linux, so `*Test.*` matches `latest.js` on one platform and not the
        other; the capital in `VideoExtensionsTest` is exactly the information a
        case-folding match destroys.
        """
        normalized = normalize_path(path)
        parts = normalized.split("/")
        if any(part.lower() in cls._TEST_DIRS for part in parts[:-1]):
            return True
        stem = Path(parts[-1]).stem
        return bool(cls._SNAKE_TEST.search(stem) or cls._CAMEL_TEST.search(stem))

    def _example_test(
        self, exclude: list[str], suffix: str = ""
    ) -> tuple[str, str] | None:
        """An existing test file for the tester to imitate, if the repo has one.

        Framework is not a preference here, it is a hard constraint: a pytest
        file under `unittest discover` collects zero tests. One real example
        settles it more reliably than any instruction.

        `suffix` filters to the language the suite is actually written in.
        This used to run the other way round — the first example found decided
        the suffix — which in a mixed repo showed the tester a JavaScript file
        and asked it for Rust. The suite decides; the example follows.

        Files this ticket just wrote are excluded — handing back the tester's
        own previous attempt would launder a wrong guess into a convention. So
        are generated directories and non-source extensions.
        """
        written = {p.replace("\\", "/") for p in exclude}
        for pattern in self._TEST_GLOBS:
            for path in sorted(self.config.root.glob(pattern)):
                if not path.is_file() or path.suffix in ("", ".pyc"):
                    continue
                if suffix and path.suffix.lower() != suffix:
                    continue
                relative = path.relative_to(self.config.root).as_posix()
                if relative in written or not self._is_test_path(relative):
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

    # What each test runner collects, matched as a substring of the configured
    # test command so a containerised or wrapped invocation still resolves.
    # Longest-lived evidence in the project: a repo with a Rust core and a
    # browser shell has both `.rs` and `.js` under tests/, and only the command
    # says which of them the suite actually is.
    _RUNNER_SUFFIXES: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("cargo", (".rs",)),
        ("pytest", (".py",)),
        ("unittest", (".py",)),
        ("nosetests", (".py",)),
        ("tox", (".py",)),
        ("gotestsum", (".go",)),
        ("go test", (".go",)),
        ("dotnet test", (".cs",)),
        ("xcodebuild", (".swift",)),
        ("swift test", (".swift",)),
        ("rspec", (".rb",)),
        ("rake", (".rb",)),
        ("phpunit", (".php",)),
        ("ctest", (".cpp", ".cc", ".c")),
        ("gradle", (".kt", ".java")),
        ("mvn", (".java",)),
        ("deno test", (".ts",)),
        # JavaScript runners collect several extensions, so these narrow the
        # field rather than settling it; the repo's own files break the tie.
        ("vitest", (".ts", ".tsx", ".js", ".jsx")),
        ("jest", (".ts", ".tsx", ".js", ".jsx")),
        ("playwright", (".ts", ".js")),
        ("cypress", (".ts", ".js")),
        ("mocha", (".ts", ".js")),
        ("ava", (".ts", ".js")),
        ("npm test", (".ts", ".tsx", ".js", ".jsx")),
        ("pnpm test", (".ts", ".tsx", ".js", ".jsx")),
        ("yarn test", (".ts", ".tsx", ".js", ".jsx")),
    )

    def _runner_suffixes(self) -> tuple[str, ...]:
        """Extensions the configured test command can collect, if it names a
        runner this knows. Empty when there is no command or no match."""
        found: list[str] = []
        for command in self.config.commands_for("test").values():
            lowered = command.lower()
            for needle, suffixes in self._RUNNER_SUFFIXES:
                if needle in lowered:
                    found.extend(suffixes)
        return tuple(dict.fromkeys(found))

    def _repo_test_suffixes(self, exclude: list[str]) -> Counter[str]:
        """How many test files of each extension the repo already has."""
        skip = {p.replace("\\", "/") for p in exclude}
        counts: Counter[str] = Counter()
        for pattern in self._TEST_GLOBS:
            for path in self.config.root.glob(pattern):
                if not path.is_file() or path.suffix.lower() in self._UNTESTABLE_SUFFIXES:
                    continue
                relative = path.relative_to(self.config.root).as_posix()
                if relative in skip or not self._is_test_path(relative):
                    continue
                if self._IGNORED_DIRS.intersection(Path(relative).parts[:-1]):
                    continue
                counts[path.suffix.lower()] += 1
        return counts

    def _suite_suffix(self, written: list[str], exclude: list[str] | None = None) -> str:
        """The file extension this project's test command actually collects.

        The command decides, because it is the only thing in the project that
        states how tests are run. Reading it off one existing test file instead
        is what broke a Rust backlog: the browser-shell ticket legitimately
        wrote `tests/tt_005_test.js`, and from then on every Rust ticket was
        told the suite collects `.js`, wrote no tests at all, and had the skip
        reported as routine. Verification quietly degraded to review-only for
        the rest of the run.

        Where a runner collects several extensions — the JavaScript ones — the
        repo's own files break the tie. Where no command is configured, the
        repo's majority decides, and a majority is used rather than the first
        file found so one stray fixture cannot outvote a whole suite. A fresh
        repo with neither falls back to what this ticket just wrote, which can
        only be wrong before there is anything to contradict it.
        """
        # What this ticket actually wrote decides first, when something runs
        # that language. A project with a Rust core and a browser shell has two
        # answers, and the right one depends on which ticket is asking — the
        # shell's tests belong in JavaScript however much Rust surrounds them.
        # Falls through when nothing covers it, so a single-command project
        # answers exactly as it always did.
        own: Counter[str] = Counter()
        for path in written:
            suffix = Path(path).suffix.lower()
            # A recognised language only. A `.xyz` file is not a language to
            # author tests in, and a catch-all command saying nothing about it
            # is not a reason to try.
            if suffix in self._CODE_SUFFIXES:
                own[suffix] += 1
        for suffix, _count in own.most_common():
            if self.config.covers("test", suffix):
                return suffix

        repo = self._repo_test_suffixes(exclude or [])
        allowed = self._runner_suffixes()
        if allowed:
            for suffix, _count in repo.most_common():
                if suffix in allowed:
                    return suffix
            return allowed[0]
        if repo:
            return repo.most_common(1)[0][0]

        counts: Counter[str] = Counter()
        for path in written:
            suffix = Path(path).suffix.lower()
            if suffix and suffix not in self._UNTESTABLE_SUFFIXES:
                counts[suffix] += 1
        return counts.most_common(1)[0][0] if counts else ""

    @classmethod
    def _test_stem(cls, ticket: Ticket, suffix: str) -> str:
        """The filename this loop gives a test it had to invent a home for.

        Shared with `_owned_test_files`, and it has to be: that is what deletes
        an unverified test, it finds the file by this exact name, and a stem
        derived in two places drifts into orphans nothing can reclaim.

        `_test` rather than a bare slug — mandatory for `go test`, one of
        pytest's two default collection patterns, inert everywhere else. Except
        where the filename has to match a public type, which rules the
        underscore out: `pn_001_test.java` cannot declare `Pn001Test`, and javac
        rejects the file wherever it is put.
        """
        slug = cls._ticket_slug(ticket)
        if suffix.lower() not in cls._TYPE_NAMED_SUFFIXES:
            return f"{slug}_test"
        return "".join(part.capitalize() for part in slug.split("_") if part) + "Test"

    @staticmethod
    def _closest_designated(designated: list[str], written: list[str]) -> str:
        """Which of the plan's test files this ticket's tests belong in.

        The tester writes one file, so several designated paths need deciding
        between. Whichever names a file the ticket just wrote is the right one —
        `ScannedFileTest.java` for a ticket that wrote `ScannedFile.java` — and
        that reads across languages, because pairing a test with its subject by
        name is what every convention here already does.

        Sorted rather than left in the plan's order, so the answer cannot move
        when a respec rewrites `allowed_files`. The path has to be fixed for the
        life of the ticket: a second cycle that picks differently leaves the
        first cycle's file behind, owned by nobody, failing every ticket after
        it.
        """
        for path in sorted(designated):
            stem = Path(path).stem.lower()
            for source in written:
                subject = Path(source).stem.lower()
                if subject and subject != stem and subject in stem:
                    return path
        return sorted(designated)[0]

    def _test_target(
        self,
        ticket: Ticket,
        written: list[str],
        example: tuple[str, str] | None,
        suffix: str = "",
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
        #
        # Any number of them, not exactly one. Requiring a lone candidate was a
        # Rust-shaped assumption: one integration test per ticket. Languages
        # that pair a test class with each production class routinely designate
        # several — PN-001 named `ScannedFileTest.java` and
        # `VideoExtensionsTest.java` — and the ticket then fell through to
        # inventing `tests/pn_001_test.java`, outside the build's test source
        # set, where it was never compiled and never run.
        designated = [
            normalize_path(path)
            for path in ticket.allowed_files
            if not any(ch in path for ch in "*?[") and self._is_test_path(path)
        ]
        if designated:
            return self._closest_designated(designated, written), ""

        # Asked before the language question, and it is a different question: a
        # ticket that wrote only a README and two build scripts has nothing to
        # assert against in any language, and saying "wrote no .rs file" about
        # it names the wrong problem.
        if not any(
            Path(path).suffix and Path(path).suffix.lower() not in self._UNTESTABLE_SUFFIXES
            for path in written
        ):
            return "", (
                "the ticket wrote no source file whose behavior a test could "
                "assert against"
            )

        suffix = suffix or self._suite_suffix(written)
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

        if example:
            directory = Path(example[0]).parent.as_posix()
        else:
            # `tests/` is a fine guess in most ecosystems and a wrong one in the
            # JVM's, where the build compiles a fixed source set and a file
            # outside it is not a failing test but an invisible one. That is how
            # a whole run's tester output came to be written, never compiled,
            # and never run, with every ticket passing the suite that excluded it.
            directory = self._TEST_ROOTS.get(suffix, "tests")
        prefix = "" if directory in ("", ".") else f"{directory}/"
        return f"{prefix}{self._test_stem(ticket, suffix)}{suffix}", ""

    def _uncovered_languages(self, ticket: Ticket) -> list[str]:
        """Languages this ticket writes that nothing here can test.

        Only meaningful in a project that tests *something*: one with no test
        command at all is a project without tests, which is a different
        situation and already reported at run end. What this catches is the
        polyglot gap — a Rust project's JavaScript, verified by nothing, whose
        tickets pass on review alone over criteria a text search can satisfy.
        That is not a hypothetical: it shipped a page that threw on the second
        line of its own entry point, with six tickets green above it.
        """
        if not self.config.commands_for("test"):
            return []
        found: list[str] = []
        for path in ticket.allowed_files:
            if any(character in path for character in "*?["):
                continue
            suffix = Path(path).suffix.lower()
            if (
                suffix in self._CODE_SUFFIXES
                and suffix not in found
                and not self.config.covers("test", suffix)
                # A language the config declares as needing no runner is a
                # decision on the record, not the oversight this gate is for.
                and not self.config.exempt("test", suffix)
            ):
                found.append(suffix)
        return found

    def _no_runner_note(self, ticket: Ticket, languages: list[str]) -> str:
        """What to tell a human whose ticket has no way to be checked."""
        files = [
            path
            for path in ticket.allowed_files
            if Path(path).suffix.lower() in languages
        ]
        return (
            f"this ticket writes {', '.join(languages)} and no test command "
            f"covers {'it' if len(languages) == 1 else 'them'}, so nothing here "
            f"could check the work — it would pass on review alone, against "
            f"criteria a text search can satisfy. Files: {', '.join(files[:6])}.\n\n"
            f"Set a runner up:\n"
            f"  forge toolchain --language {languages[0]}\n"
            f"or add it by hand in .hybridforge/config.json:\n"
            f'  "commands": {{ "test": {{ "{languages[0]}": "<command>" }} }}\n\n'
            f"Then: forge retry --ticket {ticket.ticket_id}"
        )

    def _park(self, run_id: int, ticket: Ticket, note: str) -> None:
        """Stop work on a ticket and leave a human the reason.

        Same ending as an executor's `BLOCKED:`, and it honours
        `stopOnBlocked` for the same reason: a ticket that needs a person is
        not made better by the loop moving on to the next one.
        """
        ticket.status = TICKET_BLOCKED
        ticket.blocked_note = note
        self.store.update_ticket(run_id, ticket)
        self.store.log(
            run_id,
            f"{ticket.ticket_id}: BLOCKED — {note[:400]}",
            level="warn",
            kind="ticket",
        )
        if self.config.loop.stop_on_blocked:
            raise Stopped()

    def _repro_target(self, ticket: Ticket) -> tuple[str, str]:
        """Where a bug ticket's reproduction goes, or why it cannot have one.

        Returns `(path, reason)` with exactly one side filled in — the same
        contract as `_test_target`, and the same fixed-path-per-ticket rule, so
        a second cycle overwrites its own reproduction rather than leaving one
        behind that nothing owns.

        Unlike `_test_target` this runs before any code is written, so there
        are no changed files to read a language off. The test command decides,
        and a project with no test command cannot reproduce anything: there is
        nothing that would run the proof.
        """
        designated = [
            normalize_path(path)
            for path in ticket.allowed_files
            if not any(ch in path for ch in "*?[") and self._is_test_path(path)
        ]
        if len(designated) == 1:
            return designated[0], ""

        # Asked of the ticket's own scope before any fallback runs. Left to
        # `_suite_suffix`, a JavaScript hypothesis in a Rust project resolves
        # to `.rs` — the project's runner — and the reproduction would then
        # assert something in a language the fault does not live in.
        uncovered = self._uncovered_languages(ticket)
        if uncovered:
            return "", self._no_runner_note(ticket, uncovered)

        suffix = self._suite_suffix(list(ticket.allowed_files))
        if not suffix or not self.config.commands_for("test"):
            return "", (
                "this project has no test command, so a reproduction could "
                "never be run — and a bug loop with nothing to run the proof "
                "is a fix nobody checked"
            )
        if not self.config.covers("test", suffix):
            # The level-0 case exactly: the fault is real and sits in a
            # language nothing here runs, so a reproduction written in the
            # project's own suite would assert something that was never wrong.
            return "", self._no_runner_note(ticket, [suffix])
        example = self._example_test([], suffix)
        directory = Path(example[0]).parent.as_posix() if example else "tests"
        prefix = "" if directory in ("", ".") else f"{directory}/"
        return f"{prefix}{self._ticket_slug(ticket)}_test{suffix}", ""

    # Extensions that hold behavior a test suite could have covered. A ticket
    # that cannot reproduce a fault in one language is worth pointing at the
    # others; a ticket that cannot reproduce one in a stylesheet is not.
    _CODE_SUFFIXES = frozenset(
        """.rs .py .js .mjs .ts .tsx .jsx .go .rb .java .kt .swift .c .cc .cpp
        .h .hpp .cs .php .sh .ps1 .lua .ex .exs .scala .dart""".split()
    )

    def _unreachable_layers(self, test_path: str) -> str:
        """A note naming languages in this project the test command cannot run.

        The case that produced it: a report said the game starts at level 0.
        The Rust sets it to 1, so no test of that code could fail, and the loop
        parked — correctly. The symptom was real and lived in `web/main.js`,
        which threw on its second line and left the page showing the hardcoded
        `Level: 0` forever. `cargo test` runs no JavaScript, so nothing in the
        pipeline could have reached it, and the block said only "sharpen the
        report" about a report that was accurate.

        Naming the gap costs nothing and is often the whole answer.
        """
        suite = Path(test_path).suffix.lower()
        others = sorted(
            {
                suffix
                for path in evidence.repo_files(self.config.root)
                if (suffix := Path(path).suffix.lower()) in self._CODE_SUFFIXES
                and suffix != suite
            }
        )
        if not others:
            return ""
        command = self.config.command_for("test", test_path) or "the test command"
        return (
            f"\n\nBefore sharpening it, consider where the fault is: the suite "
            f"is written in {suite} and `{command}` runs nothing else, while "
            f"this project also contains {', '.join(others)} files. A symptom "
            f"you can see in the running program and cannot reproduce in {suite} "
            f"is usually in one of those, and no ticket here can reach it."
        )

    def _prove(self, run_id: int, ticket: Ticket, test_path: str) -> str:
        """Demonstrate the fault, re-diagnosing when the explanation is wrong.

        Returns the failure that proved it, or "" with the ticket parked.

        A reproduction that cannot be written is a measurement, not a dead end.
        The tester saying "this code already does what the report asks for" is
        a fact about the code, and the right use of it is to look somewhere
        else — the report is not what was disproved, the previous ticket's
        reading of it was. One run parked on exactly that: the level really was
        initialised to 1, the reporter really did see 0, and the answer sat one
        layer away in a file the first hypothesis never named.

        So each failure feeds a re-diagnosis, up to `loop.bugHypotheses`, with
        everything already ruled out in front of the planner so the next guess
        cannot be the last one again. Parking is what happens when the budget
        runs out or the planner has nothing better than another guess — with
        every hypothesis it tried written down, which is the part a human
        actually needs.
        """
        for remaining in range(self.config.loop.bug_hypotheses - 1, -1, -1):
            outcome = self._reproduce(run_id, ticket, test_path)
            if outcome.ok:
                return outcome.detail
            if not remaining:
                self._park(run_id, ticket, self._exhausted(run_id, ticket, outcome.detail))
                return ""

            revised = self._rediagnose(run_id, ticket, outcome.detail)
            if revised is None:
                self._park(run_id, ticket, self._exhausted(run_id, ticket, outcome.detail))
                return ""
        return ""

    def _exhausted(self, run_id: int, ticket: Ticket, last: str) -> str:
        """The blocked note for a bug that was never demonstrated.

        Every hypothesis that was tried, in order, because that is the work
        this ticket actually did and the next person should not repeat it.
        """
        tried = self.store.ruled_out(run_id, ticket.ticket_id)
        note = last
        if tried:
            note += "\n\nHypotheses tried and ruled out, in order:\n" + "\n".join(
                f"  {index}. {spec.splitlines()[0][:200]}"
                for index, (spec, _why) in enumerate(tried, start=1)
            )
        return note

    def _rediagnose(self, run_id: int, ticket: Ticket, disproof: str) -> Ticket | None:
        """Rewrite the ticket around a different cause. None when there is none.

        The ticket is revised in place and persisted, so the next reproduction
        attempt runs against the new hypothesis — and `original_spec` still
        holds what the report first became, which is what the ratchet compares
        a later respec against.
        """
        report = ""
        run = self.store.get_run(run_id)
        if run is not None:
            report = run["source"] or ""

        try:
            completion = self._call(
                run_id,
                "planner",
                rediagnose_prompt(
                    ticket,
                    report or ticket.spec,
                    disproof=disproof,
                    ruled_out=self.store.ruled_out(run_id, ticket.ticket_id),
                    evidence=evidence.gather(self.config.root, report or ticket.spec),
                    sources=self._sources_for(ticket)[0],
                ),
                max_tokens=self._output_budget("planner"),
                temperature=0.2,
            )
            fields = parse_bug(completion.text)
        except (ContextOverflow, ProviderError, ValueError) as exc:
            # `unclear` arrives here too, as a ValueError carrying what the
            # planner said it would need. Either way there is no next
            # hypothesis, and saying so is the honest end.
            self.store.log(
                run_id,
                f"{ticket.ticket_id}: no further diagnosis — {exc}",
                level="warn",
                kind="ticket",
            )
            return None

        # Written before the ticket changes, so the record is of what was
        # dropped rather than of what replaced it.
        self.store.log(
            run_id,
            f"{ticket.ticket_id}: that explanation was disproved, so it was "
            f"dropped and a different cause proposed:\n"
            f"  was: {ticket.spec.splitlines()[0][:160]}\n"
            f"  now: {fields['spec'].splitlines()[0][:160]}",
            level="warn",
            kind="ticket",
            data={
                "ticket": ticket.ticket_id,
                "ruled_out": ticket.spec,
                "disproof": disproof[:2000],
                "scope": ticket.allowed_files,
            },
        )

        ticket.title = fields["title"] or ticket.title
        ticket.spec = fields["spec"]
        ticket.allowed_files = fields["allowed_files"] or ticket.allowed_files
        # Read scope is widened around the new writable scope for the same
        # reason it is at filing time: a hypothesis is checked by reading the
        # code around it, and a role shown only the file it may write cannot
        # tell whether the cause it was handed is the right one.
        ticket.reference_files = evidence.reading_scope(
            self.config.root, ticket.allowed_files, fields["reference_files"]
        )
        ticket.context = fields["reproduce"] or ticket.context
        # The reproduction path is derived from the ticket id and does not
        # move, so the next attempt overwrites the test that proved nothing
        # rather than leaving it behind asserting something never in doubt.
        self.store.update_ticket(run_id, ticket)
        return ticket

    def _reproduce(self, run_id: int, ticket: Ticket, test_path: str) -> StepResult:
        """Write a test that fails because of this bug, and prove it fails.

        The one step in the loop where a red suite is the result being asked
        for. `ok` means the test ran and failed for the reported reason, and
        its output is the evidence — carried into the executor's prompt as what
        to fix, and into the reviewer's as what the fix has to have addressed.

        Three ways it does not get there, and they are not the same thing:

        - The test will not build. That is a defect in the test, not evidence
          about the code, so the tester is pointed at its own errors and asked
          again — the same reprompt the ordinary test path already does.
        - The test passes. Either it asserts something the bug does not touch,
          or it asserts the bug itself. One more attempt with the passing
          output quoted back, then the ticket parks.
        - The tester replies `BLOCKED:`. A report too vague to assert anything
          specific is a report a human has to sharpen, and guessing at it
          produces a proof of nothing that everything downstream then trusts.

        Parking is the honest end for all three. A bug nobody can demonstrate
        is a bug the loop would be fixing on faith, and the green afterwards
        would mean exactly as much as the green that shipped the two defects
        this whole path exists to catch.
        """
        command = self.config.command_for("test", test_path)
        sources = self._sources_for(ticket)[0]
        example = self._example_test([test_path], Path(test_path).suffix.lower())
        own_file_errors: list[str] = []
        passed_instead = ""

        for remaining in (1, 0):
            step_id = self.store.start_step(run_id, ticket.ticket_id, "reproduce")
            try:
                completion = self._call(
                    run_id,
                    "tester",
                    repro_prompt(
                        ticket,
                        test_path=test_path,
                        test_command=command,
                        example_test=example,
                        sources=sources,
                        reproduce=ticket.context,
                        own_file_errors=own_file_errors,
                        passed_instead=passed_instead,
                    ),
                    max_tokens=self._output_budget("tester"),
                    temperature=0.1,
                )
            except (ContextOverflow, ProviderError) as exc:
                self.store.end_step(step_id, "failed", str(exc))
                return StepResult(ok=False, blocked=True, detail=f"reproduction unavailable: {exc}")

            self._record_call(ticket, "reproduce", "tester", completion)
            if completion.truncated:
                self.store.end_step(step_id, "failed", completion.text[:20000])
                return StepResult(
                    ok=False,
                    blocked=True,
                    detail="the tester ran out of output room writing the "
                    "reproduction; raise maxOutputTokens for the tester model",
                )

            parsed = parse_output(completion.text)
            if parsed.is_blocked:
                self.store.end_step(step_id, "failed", parsed.blocked_reason)
                return StepResult(
                    ok=False,
                    blocked=True,
                    detail=f"the report cannot be turned into a test: "
                    f"{parsed.blocked_reason}",
                )

            scoped = enforce_scope(parsed, [test_path], self.config.never_delegate)
            bindings = [
                line for edit in scoped.edits for line in foreign_bindings(edit.content)
            ]
            if not scoped.edits or bindings:
                detail = (
                    "the tester declared the code under test as a foreign binding"
                    if bindings
                    else "the tester wrote no test file"
                )
                self.store.end_step(step_id, "failed", detail)
                if not remaining:
                    return StepResult(ok=False, blocked=True, detail=detail)
                self.store.log(
                    run_id,
                    f"{ticket.ticket_id}: {detail}; asking again.",
                    level="warn",
                    kind="ticket",
                )
                continue

            apply_edits(self.config.root, scoped.edits)
            result = self._shell(run_id, "reproduce-test", command)

            if result.ok:
                # Nothing was demonstrated. The file stays on disk for the
                # retry to overwrite; a passing test hurts nothing while it
                # sits there, and deleting it would throw away the thing the
                # next attempt is supposed to improve on.
                passed_instead = distill(result.detail, limit=2000) or "(no output)"
                self.store.end_step(
                    step_id, "failed", f"the test passed, so nothing was proved:\n{passed_instead}"
                )
                if not remaining:
                    return StepResult(
                        ok=False,
                        blocked=True,
                        detail=(
                            f"the bug could not be reproduced. `{test_path}` was "
                            f"written twice and passed both times, so there is "
                            f"nothing to fix and no way to tell whether a fix "
                            f"worked. Sharpen the report — the exact input, the "
                            f"value you saw, the value you expected — or fix it "
                            f"by hand. The test is on disk to start from."
                            + self._unreachable_layers(test_path)
                        ),
                    )
                self.store.log(
                    run_id,
                    f"{ticket.ticket_id}: the reproduction passed, so it proved "
                    f"nothing; asking for a sharper one.",
                    level="warn",
                    kind="ticket",
                )
                continue

            # Two conditions, because either alone is wrong. The test file
            # has to be implicated — otherwise the build error belongs to
            # somebody else's code and the reproduction stands — and the run
            # has to report a build or collection error somewhere, because a
            # failing assertion names its own file too and that is the evidence
            # this step exists to collect.
            implicated = errors_naming(result.detail, test_path)
            own_file_errors = (
                implicated if implicated and _UNBUILDABLE.search(result.detail) else []
            )
            if own_file_errors:
                # A test that will not build fails the command for a reason
                # that has nothing to do with the bug. Counting that as a
                # reproduction would hand the executor a compiler error in a
                # file it cannot even write.
                self.store.end_step(
                    step_id,
                    "failed",
                    "the reproduction does not build:\n" + "\n".join(own_file_errors),
                )
                if not remaining:
                    return StepResult(
                        ok=False,
                        blocked=True,
                        detail=(
                            f"`{test_path}` still does not build, so the bug was "
                            f"never demonstrated:\n" + "\n".join(own_file_errors[:10])
                        ),
                    )
                self.store.log(
                    run_id,
                    f"{ticket.ticket_id}: the reproduction does not build; asking again.",
                    level="warn",
                    kind="ticket",
                )
                continue

            proof = distill(result.detail, limit=4000)
            self.store.end_step(step_id, "ok", proof)
            self.store.log(
                run_id,
                f"{ticket.ticket_id}: reproduced. `{test_path}` fails against the "
                f"code as it stands, and the fix is not done until it passes.",
                kind="ticket",
                data={"test": test_path},
            )
            return StepResult(ok=True, detail=proof)

        # Unreachable: every branch above either returns or continues, and the
        # last iteration always returns.
        return StepResult(ok=False, blocked=True, detail="reproduction failed")

    @staticmethod
    def _ticket_slug(ticket: Ticket) -> str:
        """The filename-safe form of a ticket id, as `_test_target` spells it."""
        return re.sub(r"[^a-z0-9]+", "_", ticket.ticket_id.lower()).strip("_") or "ticket"

    def _owned_test_files(self, ticket: Ticket) -> list[str]:
        """Test files that are this ticket's by name, whoever wrote them.

        `_test_target` derives the tester's filename from the ticket id, so
        `tests/tt_004_test.rs` cannot have come from anywhere but this loop
        writing tests for TT-004. Ownership by name is what makes reclaiming
        one idempotent.

        Authorship alone is not enough, and the gap is not theoretical. The
        `created` map records whether *this* ticket run brought the file into
        existence, so a file that survives a single run — the tester declined
        to author on the next one, the run ended between writing and
        discarding — is thereafter seen as pre-existing by every run after it.
        `created` says False forever, no ticket can ever reclaim it, and the
        orphan fails the whole backlog until a human deletes it. One run spent
        five retry cycles, thirty-five minutes, and roughly 800k tokens on
        exactly that.
        """
        # Every spelling `_test_stem` can produce, not just the one this
        # ticket's language would pick today. A file written under an earlier
        # rule — or before this repository had a test command to read a suffix
        # off — is still this ticket's to reclaim, and a stem set that narrowed
        # to the current answer would strand it: owned by nobody, failing every
        # ticket after it, deletable only by hand.
        slug = self._ticket_slug(ticket)
        stems = {f"{slug}_test"} | {
            self._test_stem(ticket, suffix) for suffix in self._TYPE_NAMED_SUFFIXES
        }
        found: set[str] = set()
        for pattern in self._TEST_GLOBS:
            for path in self.config.root.glob(pattern):
                if not path.is_file() or path.stem not in stems:
                    continue
                relative = path.relative_to(self.config.root).as_posix()
                if self._IGNORED_DIRS.intersection(Path(relative).parts[:-1]):
                    continue
                found.add(relative)
        return sorted(found)

    def _remove_test_file(self, run_id: int, ticket: Ticket, path: str) -> None:
        """Delete one unverified test file, reporting either outcome."""
        if not is_safe_path(self.config.root, path):
            return
        target = self.config.root / path
        # Checked rather than `missing_ok`, so the log records removals that
        # happened instead of removals that were attempted.
        if not target.is_file():
            return
        # Set aside under the same quarantine as the implementation it was
        # written against, and for the same reason: the assertions have to stop
        # running, but a human reading the blocked note wants both halves of
        # what the ticket produced, not the code without the tests.
        self._keep_a_copy(run_id, ticket, path)
        try:
            target.unlink()
        except OSError as exc:
            self.store.log(
                run_id,
                f"{ticket.ticket_id}: could not remove unverified test "
                f"{path} ({exc}); it will fail later tickets.",
                level="warn",
                kind="ticket",
            )
            return
        self.store.log(
            run_id,
            f"{ticket.ticket_id}: removed unverified test {path}.",
            level="warn",
            kind="ticket",
        )

    def _red_left_behind(self, run_id: int, ticket: Ticket) -> None:
        """Check the tree the moment a ticket gives up, not at the next one.

        `_unverifiable` already refuses to record a green nobody checked, but it
        can only speak from inside a ticket's verify step — so the ticket after
        the one that gave up is delegated, tested, verified and only then told
        that none of it was checked. That is a whole attempt spent to learn
        something that was true before it started: a run with a red tree and an
        exhausted owner has no way back, and every model call after that point
        is spent producing evidence nothing can read.

        Quarantine usually makes this a no-op — the tree goes back to the state
        the ticket inherited, and the next ticket is verified against a build
        that works. It is what catches the cases quarantine cannot fix: a
        repository with no git for the revert to read, a copy that could not be
        written, or a failure the ticket left in a file it never wrote.

        The ownership rule is the one `_unverifiable` uses, with the ticket that
        just gave up now counted among the owners rather than excluded from
        them. Red owned only by tickets still pending is a backlog mid-flight —
        a JVM plan is routinely red between the ticket that calls a class and
        the one that writes it. Red owned by nobody is an orphan, which
        `_finish` and the orphan sweep handle. Red owned by something out of
        attempts is the one nothing coming will clear.
        """
        if self._halt or self._toolchain:
            return
        # Nothing runnable is left, so `_finish` runs the same commands next
        # and reports what it finds. Checking here as well would pay for the
        # suite twice to reach the same answer.
        if self.store.next_ticket(run_id) is None:
            return

        failed: list[str] = []
        output: list[str] = []
        for name, command in self._verify_plan():
            result = self._shell(run_id, f"after-{ticket.ticket_id}-{name}", command)
            self._note_toolchain(name, command, result)
            if self._toolchain:
                return
            if not result.ok:
                failed.append(name)
                output.append(result.detail)
        if not failed:
            return

        red = sorted(
            {
                repo_relative(path, self.config.root).lower()
                for path in files_blamed("\n".join(output))
            }
        )
        if not red:
            # A failure with nothing to attribute. It may be real and it may be
            # someone's, but there is no file to name and no owner to look up,
            # and ending a run on an unparseable diagnostic is the wrong
            # direction to be wrong in.
            return

        stalled = sorted(
            other.ticket_id
            for other in self.store.list_tickets(run_id)
            if other.status in self._GAVE_UP
            and any(matches_any(path, [p.lower() for p in other.allowed_files]) for path in red)
        )
        if not stalled:
            return

        self._halt = (
            f"{ticket.ticket_id} gave up and the tree is still red on files "
            f"nothing left in this backlog owns:\n"
            + "\n".join(f"  - {path}" for path in red[:8])
            + f"\n\n{', '.join(stalled)} already ran out of attempts on those "
            f"files, so {', '.join(failed)} will fail identically for every "
            f"ticket after this one — and each of them would be excused for it "
            f"and recorded green having compiled nothing. Fix them, then "
            f"`forge retry`."
        )
        self.store.log(
            run_id,
            f"{ticket.ticket_id}: {self._halt}",
            level="error",
            kind="verify",
            data={"steps": failed, "files": red[:20], "stalled": stalled},
        )

    def _abandoned_dir(self, run_id: int, ticket: Ticket) -> Path:
        return (
            self.config.config_dir
            / ABANDONED_DIR
            / f"run-{run_id}"
            / safe_name(ticket.ticket_id, "ticket")
        )

    def _keep_a_copy(self, run_id: int, ticket: Ticket, path: str) -> bool:
        """Save the current contents of `path` under `.hybridforge/abandoned/`.

        Salvage was the whole argument for leaving a failed ticket's work in
        the tree, and it is a good one: a ticket that failed on its fourth file
        may have got the first three right, and a human reading the blocked
        note needs to see what was attempted. Taking the work out of the tree
        does not have to take it away — it only has to stop it being compiled.

        Returns whether the copy landed. A revert is not performed on a file
        whose copy could not be written: losing the work is worse than leaving
        the tree red, and the tree being red is a state the loop already
        detects and reports.
        """
        if not is_safe_path(self.config.root, path):
            return False
        source = self.config.root / path
        if not source.is_file():
            # Never written, or already gone. Nothing to save and nothing lost.
            return True
        target = self._abandoned_dir(run_id, ticket) / normalize_path(path)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            return True
        except (OSError, shutil.Error) as exc:
            self.store.log(
                run_id,
                f"{ticket.ticket_id}: could not set {path} aside ({exc}); "
                f"leaving it in the tree rather than discarding it.",
                level="warn",
                kind="ticket",
            )
            return False

    def _baseline_blob(self, tree: str, path: str) -> bytes | None:
        """The bytes of `path` in `tree`, or None if it was not there.

        Read as bytes through `git cat-file`, not as text: a ticket may
        authorise a binary fixture, and round-tripping one through a decode
        would corrupt the file this method exists to restore faithfully.
        """
        try:
            result = subprocess.run(
                ["git", "cat-file", "blob", f"{tree}:{normalize_path(path)}"],
                cwd=self.config.root,
                capture_output=True,
                check=False,
            )
        except (FileNotFoundError, OSError):
            return None
        return result.stdout if result.returncode == 0 else None

    def _quarantine(self, run_id: int, ticket: Ticket, touched: set[str]) -> None:
        """Take a given-up ticket's work out of the tree, keeping a copy.

        Nothing used to revert a failed ticket. The reasoning was salvage, and
        the cost was paid by every ticket after it: verification is
        whole-project, so an abandoned file that does not compile is reported
        to each of them, and because it is outside their scope the baseline
        excuses them for it — which means they pass having had nothing
        compiled and nothing run. A real run went that way and stopped at
        `PN-005` with two tickets `done`, one out of attempts, and a tree where
        `compileJava` failed on the first file it read. Everything downstream
        of the abandoned file was unreachable for the rest of the run.

        What is reverted is only what this ticket's own `apply` steps wrote,
        recorded path by path rather than inferred from a diff. `baseline_tree`
        is pinned for the ticket's whole life, so a diff against it on a retry
        cycle would also name files other tickets landed in between — and a
        glob in `allowed_files` is a scope rule, not a filename, so expanding
        one to decide what to delete would reach further than the ticket ever
        did.

        Restoring means the version in `baseline_tree`, or removal when the
        file was not there: a ticket that created a file and failed leaves no
        file. Without a usable baseline nothing is reverted at all — deleting
        on a guess could take a hand-written file the ticket was extending, and
        that is not a mistake a copy under `abandoned/` makes up for.

        "Usable" is checked, not assumed. A snapshot is an unreferenced tree
        object, so `git gc` may prune one out from under a long run — and a
        tree that cannot be read answers "this path was not in the baseline"
        for *every* path, which is the answer that deletes files. The tree is
        proved readable once before anything is touched.
        """
        if not self.config.loop.quarantine_failed or not touched:
            return
        if not ticket.baseline_tree:
            self.store.log(
                run_id,
                f"{ticket.ticket_id}: no baseline tree was recorded, so its "
                f"{len(touched)} file(s) stay in the tree as it left them. "
                f"Whatever they break is now outside every later ticket's "
                f"scope and will be excused rather than fixed.",
                level="warn",
                kind="ticket",
                data={"files": sorted(touched)},
            )
            return

        if self._git("ls-tree", "--name-only", ticket.baseline_tree).returncode != 0:
            self.store.log(
                run_id,
                f"{ticket.ticket_id}: its baseline tree "
                f"{ticket.baseline_tree[:12]} can no longer be read, so there "
                f"is nothing to restore from and its {len(touched)} file(s) "
                f"stay as it left them. Removing them on that basis would "
                f"delete work no copy of which exists.",
                level="warn",
                kind="ticket",
                data={"files": sorted(touched)},
            )
            return

        restored: list[str] = []
        removed: list[str] = []
        for path in sorted(touched):
            if not is_safe_path(self.config.root, path):
                continue
            if not self._keep_a_copy(run_id, ticket, path):
                continue
            target = self.config.root / path
            original = self._baseline_blob(ticket.baseline_tree, path)
            try:
                if original is None:
                    if target.is_file():
                        target.unlink()
                        removed.append(path)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(original)
                    restored.append(path)
            except OSError as exc:
                self.store.log(
                    run_id,
                    f"{ticket.ticket_id}: could not restore {path} ({exc}); "
                    f"it stays as the ticket left it.",
                    level="warn",
                    kind="ticket",
                )

        if not restored and not removed:
            return
        self.store.log(
            run_id,
            f"{ticket.ticket_id}: gave up, so its work was taken back out of "
            f"the tree — {len(restored)} file(s) restored, {len(removed)} "
            f"removed. The tree is as this ticket found it, so the tickets "
            f"after it are verified against a build that is not carrying this "
            f"failure. A copy of what it wrote is under "
            f"{ABANDONED_DIR}/run-{run_id}/{safe_name(ticket.ticket_id, 'ticket')}/.",
            level="warn",
            kind="ticket",
            data={"restored": restored, "removed": removed},
        )

    def _discard_tests(self, run_id: int, ticket: Ticket, created: dict[str, bool]) -> None:
        """Remove test files this ticket owns but never got verified.

        A ticket that ends failed or blocked leaves behind assertions nothing
        ever confirmed, written against an implementation that does not work.
        Because verification is whole-project, those assertions go on failing
        every remaining ticket — and since the file is outside their scope, the
        backlog cannot recover on its own. That is the exact shape of the run
        where one abandoned `tests/wasm_layer.rs` blocked all six tickets.

        Two kinds of ownership, because they carry different risks:

        By name, unconditionally — the id-derived path from `_test_target`.
        Nothing but this loop produces that filename, so there is nobody else
        it could belong to.

        By authorship, only when this run created it — a path the *plan*
        designated. That one may be a hand-written file the ticket was asked
        to extend, and a failed ticket does not earn the right to delete a
        human's work.
        """
        if ticket.kind == TICKET_BUG:
            # A reproduction is not an unverified assertion — it is the one
            # assertion here that was demonstrated against real behavior, and
            # it is half of what the ticket was for. Deleting it on failure
            # would throw away the only durable record that the fault is real
            # and leave the next person with the report they started from.
            #
            # Safe to leave failing, too: every other ticket takes a baseline
            # before it runs and is not blamed for breakage outside its own
            # scope, which is exactly what this file is to them.
            self.store.log(
                run_id,
                f"{ticket.ticket_id}: the reproduction was kept. It still "
                f"fails, and it is the evidence — fix the bug by hand, or "
                f"retry the ticket once the report is sharper.",
                level="warn",
                kind="ticket",
            )
            return
        doomed = set(self._owned_test_files(ticket))
        doomed.update(path for path, was_created in created.items() if was_created)
        for path in sorted(doomed):
            self._remove_test_file(run_id, ticket, path)

    def _sweep_orphan_tests(self, run_id: int) -> None:
        """Reclaim test files whose ticket never reached a discard of its own.

        The per-ticket path covers a ticket that fails or blocks inside the
        loop. It does not cover one that was skipped, or one whose run ended
        between the tester writing the file and the ticket resolving. Those
        orphans outlive the run and fail the final verify — and every ticket
        of every later retry cycle — so the backlog is swept once more before
        the run reports anything.
        """
        for ticket in self.store.list_tickets(run_id):
            if ticket.status == TICKET_DONE or ticket.kind == TICKET_BUG:
                continue
            for path in self._owned_test_files(ticket):
                self._remove_test_file(run_id, ticket, path)

    def _written_but_unchanged(self, written: Sequence[str], diff: str) -> dict[str, str]:
        """Contents of the files this attempt wrote that the diff does not show.

        A file is absent from a diff for exactly one reason once it has been
        written: what was written is what was already there. That is invisible
        to the reviewer and indistinguishable, from its side, from work that
        never happened.
        """
        found: dict[str, str] = {}
        for path in written:
            normalized = normalize_path(path)
            if f"b/{normalized}" in diff or f"+++ {normalized}" in diff:
                continue
            if not is_safe_path(self.config.root, path):
                continue
            candidate = (self.config.root / path).resolve()
            try:
                if candidate.is_file():
                    found[path] = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
        return found

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

    def _diff(self, since: str = "", paths: Sequence[str] = ()) -> str:
        """The changeset a reviewer should judge.

        Scoped to one ticket two ways, and it needs both. `since` is the tree
        captured before the ticket started; `paths` are the files it owns.

        Time alone used to do the whole job — anything the ticket did not touch
        appeared in both trees and cancelled out. That works only while the
        baseline moves forward with the ticket. Now it is pinned for the
        ticket's whole life, so work other tickets landed in between no longer
        cancels, and without a path filter the reviewer would see it and
        correctly reject it as outside this ticket's scope. That is the failure
        this method exists to prevent, arriving from the other direction.

        A pathspec is only applied when every path is literal. A glob in
        `allowed_files` is a scope rule rather than a filename, and git would
        read it under its own matching rules — showing the reviewer less than
        the ticket changed, which is worse than showing it more.
        """
        wanted = [path for path in paths if path]
        globbed = any(character in path for path in wanted for character in "*?[")
        pathspec = ["--", *wanted] if wanted and not globbed else []
        try:
            if since:
                current = self._snapshot()
                if current:
                    result = self._git("diff-tree", "-p", since, current, *pathspec)
                    if result.returncode == 0:
                        return result.stdout

            # No baseline, or git refused one — a pruned object, a fresh clone,
            # a tree hash from a run whose history is gone. A degraded diff
            # beats a failed review, so fall back to the whole tree.
            # `git add -N` registers new files with the index without staging
            # their contents, so a ticket that creates a file still appears.
            self._git("add", "-N", ".")
            return self._git("diff", *pathspec).stdout
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
