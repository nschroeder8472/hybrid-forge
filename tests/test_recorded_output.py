"""What the loop does when it is fed a real run's output, cycle after cycle.

Eleven live runs of `examples/sample-project` all landed on the first attempt,
so `_convergence`, `flatCycles` and the escalation ladder have never run
outside a unit test — and every unit test in the suite fed the failure parser
strings somebody typed. That combination hid a defect for as long as lint has
been supported: a whole `flake8` run parsed to zero diagnostic blocks, so
`signatures()` returned the empty set and both of its callers read that as *no
errors* rather than *cannot attribute*.

So these drive the loop the way a unit test does and feed it what a tool
actually said. The details come from `tests/recordings/`, harvested verbatim
from run databases by `scripts/harvest_recording.py`. See `tests/recorded.py`.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import recorded  # noqa: E402
from forge.failures import classify, signatures  # noqa: E402
from forge.loop import Orchestrator  # noqa: E402
from forge.providers import Completion, Usage  # noqa: E402

BLIND = "blind-lint-stall"


class TestTheRecordingsAreRealToolOutput(unittest.TestCase):
    """The regression guard for the defect this harness exists because of. If
    these ever parse to nothing again, every attribution the loop makes over a
    lint failure is blind and the suite says nothing."""

    def setUp(self):
        self.recording = recorded.Recording(BLIND)

    def test_the_first_lint_carries_the_findings_the_run_reported(self):
        detail = self.recording.detail(11)

        self.assertEqual(len(detail.splitlines()), 24)
        self.assertIn("E501 line too long", detail)
        # Windows separators, because that is what the run produced. A
        # recording normalised into posix would be a fixture again.
        self.assertIn("\\", detail)

    def test_every_finding_parses_as_its_own_signature(self):
        # What baseline amnesty rests on: a ticket's own failure has to be
        # distinguishable from one it inherited.
        for step in (11, 14):
            detail = self.recording.detail(step)

            self.assertEqual(
                len(signatures(detail)), len(detail.splitlines()), f"step {step}"
            )

    def test_the_classes_name_the_code_and_the_file(self):
        found = classify("lint", self.recording.detail(11))

        self.assertEqual(len(found), 2)
        self.assertTrue(all("E501" in name for name in found), found)

    def test_the_build_that_returned_nothing_is_recorded_as_nothing(self):
        # The step that made the loop re-run lint over unchanged files and
        # manufacture a repeat failure no change had caused.
        self.assertEqual(self.recording.detail(15), "")
        self.assertEqual(self.recording.step(15)["name"], "build")

    def test_the_repeat_really_is_byte_identical(self):
        self.assertEqual(self.recording.detail(14), self.recording.detail(16))


class TestTheCurveTheBlindRunProduced(unittest.TestCase):
    """`arm-blind` of `docs/BLIND-GRADING.md`, cycle by cycle, against the
    output it actually produced rather than a described version of it."""

    def setUp(self):
        self.recording = recorded.Recording(BLIND)
        self.orchestrator, self.store, self.run_id = recorded.orchestrator()

    def _cycle(self, detail):
        return recorded.cycle(self.orchestrator, self.store, self.run_id, detail)

    def test_it_descends_and_then_goes_flat(self):
        first = self._cycle(self.recording.detail(11))     # 24 findings, 2 files
        second = self._cycle(self.recording.detail(14))    # 7 findings, 1 file
        third = self._cycle(self.recording.detail(16))     # the same 7 again

        self.assertEqual(first, Orchestrator.FIRST)
        self.assertEqual(second, Orchestrator.DESCENDING)
        self.assertEqual(third, Orchestrator.FLAT)

    def test_the_descent_is_a_file_leaving_the_set(self):
        self._cycle(self.recording.detail(11))
        self.assertEqual(len(recorded.classes(self.store, self.run_id)), 2)

        self._cycle(self.recording.detail(14))

        self.assertEqual(
            recorded.classes(self.store, self.run_id),
            ["lint E501 in ./tests/stream_test.py"],
        )

    def test_a_repeat_no_change_caused_still_counts_as_a_flat_cycle(self):
        # Step 15 returned nothing, so step 16 is step 14's output again. The
        # detector cannot tell that from an executor that tried and failed —
        # which is the argument for not letting an empty build reach it.
        self._cycle(self.recording.detail(11))
        self._cycle(self.recording.detail(14))
        self._cycle(self.recording.detail(16))

        ticket = self.store.list_tickets(self.run_id)[0]
        self.assertEqual(ticket.flat_cycles, 1)


class TestASetThatShrinksWithoutChanging(unittest.TestCase):
    """The finding the blind runs were retracted over, re-established in the
    form that survives the parser repair — and now the fix for it.

    Named per code and per file, a failure set shrinking *within one file* does
    not move at all: 7 findings, then 3, then 1 is one class throughout. Read
    on classes alone that is `FLAT` twice over, and `flat_cycles` reaches
    `reviewWhenStuck`'s default rung on a ticket converging as fast as anything
    in this repository ever has.

    `_convergence` now reads the size of the set as well as its members, so
    the same curve descends. The subsets are truncations of a recorded step —
    every line is one a tool wrote."""

    def setUp(self):
        recording = recorded.Recording(BLIND)
        seven = recording.detail(14).splitlines()
        self.shrinking = ["\n".join(seven[:n]) + "\n" for n in (7, 3, 1)]
        self.orchestrator, self.store, self.run_id = recorded.orchestrator()

    def _ticket(self):
        return self.store.list_tickets(self.run_id)[0]

    def test_the_counts_really_do_fall(self):
        self.assertEqual(
            [len(signatures(detail)) for detail in self.shrinking], [7, 3, 1]
        )

    def test_the_class_is_the_same_one_throughout(self):
        # Without this the descent below would be the old signal, not the new
        # one: a class leaving the set is a descent every version could see.
        found = {
            name for detail in self.shrinking for name in classify("lint", detail)
        }

        self.assertEqual(len(found), 1, found)

    def test_every_cycle_after_the_first_descends(self):
        verdicts = [
            recorded.cycle(self.orchestrator, self.store, self.run_id, detail)
            for detail in self.shrinking
        ]

        self.assertEqual(
            verdicts,
            [Orchestrator.FIRST, Orchestrator.DESCENDING, Orchestrator.DESCENDING],
        )

    def test_and_the_ticket_never_reaches_the_rung_the_ladder_fires_on(self):
        for detail in self.shrinking:
            recorded.cycle(self.orchestrator, self.store, self.run_id, detail)

        self.assertEqual(self._ticket().flat_cycles, 0)

    def test_what_the_cycle_ended_on_is_recorded(self):
        for detail in self.shrinking:
            recorded.cycle(self.orchestrator, self.store, self.run_id, detail)

        self.assertEqual(self._ticket().cycle_volume, 1)

    def test_the_same_findings_again_is_still_flat(self):
        same = self.shrinking[0]

        verdicts = [
            recorded.cycle(self.orchestrator, self.store, self.run_id, detail)
            for detail in (same, same, same)
        ]

        self.assertEqual(verdicts[1:], [Orchestrator.FLAT, Orchestrator.FLAT])
        self.assertEqual(self._ticket().flat_cycles, 2)

    def test_more_of_the_same_failure_is_not_progress(self):
        # The other direction of the same comparison. A set that has grown is
        # not descending, and the brake belongs on it.
        verdicts = [
            recorded.cycle(self.orchestrator, self.store, self.run_id, detail)
            for detail in reversed(self.shrinking)
        ]

        self.assertEqual(
            verdicts,
            [Orchestrator.FIRST, Orchestrator.FLAT, Orchestrator.FLAT],
        )

    def test_output_that_cannot_be_counted_does_not_read_as_a_descent(self):
        # `signatures` returns nothing for output no pattern recognises, and
        # that means *cannot attribute* rather than *nothing failed*. A count
        # of zero against a real one must not excuse the cycle.
        unparseable = "the build tool said something nothing here recognises\n"
        self.assertEqual(signatures(unparseable), set())

        first = recorded.cycle(
            self.orchestrator, self.store, self.run_id, unparseable
        )
        second = recorded.cycle(
            self.orchestrator, self.store, self.run_id, unparseable
        )

        self.assertEqual(first, Orchestrator.FIRST)
        self.assertEqual(second, Orchestrator.FLAT)


class TestTheLadderIsLeftAloneWhileFindingsFall(unittest.TestCase):
    """The rung, driven through `_retry_cycle` over the shrinking curve. The
    measurement above says the verdict changed; this says the escalation it
    feeds changed with it."""

    def setUp(self):
        recording = recorded.Recording(BLIND)
        seven = recording.detail(14).splitlines()
        self.shrinking = ["\n".join(seven[:n]) + "\n" for n in (7, 3, 1)]
        self.orchestrator, self.store, self.run_id = recorded.orchestrator(
            review_when_stuck=2
        )
        self.asked: list[str] = []

        def call(_run_id, role, messages, **_kwargs):
            if role == "reviewer" and any(
                "satisfied as written" in message.content for message in messages
            ):
                self.asked.append(role)
                return Completion(
                    text="VERDICT: winnable\nThe rule is in the failure text.",
                    usage=Usage(),
                    finish_reason="stop",
                )
            return Completion(text="{}", usage=Usage(), finish_reason="stop")

        self.orchestrator._call = call

    def test_a_ticket_working_off_one_class_is_not_escalated(self):
        for detail in self.shrinking:
            recorded.fail(self.store, self.run_id, detail)
            self.orchestrator._retry_cycle(self.run_id, "blocked")
            ticket = self.store.list_tickets(self.run_id)[0]
            if ticket.status == "pending":
                ticket.status = "failed"
                self.store.update_ticket(self.run_id, ticket)

        self.assertEqual(self.asked, [])


class TestTheLadderClimbsOnRecordedOutput(unittest.TestCase):
    """The rung firing, driven through `_retry_cycle` rather than by calling
    the measurement directly — with the reviewer scripted and the failures
    real."""

    def setUp(self):
        self.recording = recorded.Recording(BLIND)
        self.orchestrator, self.store, self.run_id = recorded.orchestrator(
            review_when_stuck=2
        )
        self.asked: list[str] = []

        def call(_run_id, role, messages, **_kwargs):
            if role == "reviewer" and any(
                "satisfied as written" in message.content for message in messages
            ):
                self.asked.append(role)
                return Completion(
                    text="VERDICT: winnable\nThe rule is in the failure text.",
                    usage=Usage(),
                    finish_reason="stop",
                )
            return Completion(text="{}", usage=Usage(), finish_reason="stop")

        self.orchestrator._call = call

    def _cycles(self, *details):
        for detail in details:
            recorded.fail(self.store, self.run_id, detail)
            self.orchestrator._retry_cycle(self.run_id, "blocked")
            ticket = self.store.list_tickets(self.run_id)[0]
            if ticket.status == "pending":
                ticket.status = "failed"
                self.store.update_ticket(self.run_id, ticket)

    def test_a_ticket_repeating_one_real_failure_reaches_the_reviewer(self):
        same = self.recording.detail(14)

        self._cycles(same, same, same)

        self.assertEqual(self.asked, ["reviewer"], "the rung fires once")

    def test_a_ticket_whose_failures_are_leaving_is_left_alone(self):
        # The other half, and the one worth being sure of: escalating a
        # descending ticket spends a review call to be told to carry on.
        self._cycles(self.recording.detail(11), self.recording.detail(14))

        self.assertEqual(self.asked, [])


if __name__ == "__main__":
    unittest.main()
