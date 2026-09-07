"""Decomposition, and the invariant that makes it safe.

§6 of docs/ADAPTIVE-TICKET-LOOP.md. The union of the children's acceptance
criteria must cover the parent's, so scope is conserved by construction rather
than by a model's judgement about what it may drop. Most of what is tested
here is refusal: the proposals that must not become children.

The trigger ships off — `loop.volumeThreshold` is `0` — and one test pins that
too, because a brake that arms itself in a default configuration is the failure
mode this whole mechanism was written around.

    python -m unittest discover tests
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from forge import split
from forge.config import Config, LoopSettings
from forge.loop import Orchestrator
from forge.providers import Completion, Usage
from forge.state import Store, Ticket


def _parent(**fields) -> Ticket:
    base = {
        "title": "the whole thing",
        "spec": "build all of it",
        "criteria": ["holds a", "holds b", "holds c"],
        "original_criteria": ["holds a", "holds b", "holds c"],
        "allowed_files": ["a.py", "b.py", "c.py"],
        "distinct_classes": 12,
    }
    base.update(fields)
    return Ticket("T-1", **base)


def _proposal(*children: dict) -> str:
    return json.dumps({"children": list(children)})


def _child(ident: str, covers: list[int], **fields) -> dict:
    base = {
        "id": ident,
        "title": ident,
        "spec": f"build the {ident} part",
        "criteria": [f"{ident} holds"],
        "covers": covers,
        "allowed_files": ["a.py"],
        "needs": [],
    }
    base.update(fields)
    return base


class TestReadingAProposal(unittest.TestCase):
    """`split.parse_split`, which is strict because the fields are load-bearing."""

    def test_a_fenced_object_is_read(self):
        text = "Here is the split:\n```json\n" + _proposal(
            _child("T-1-a", [1, 2]), _child("T-1-b", [3])
        ) + "\n```"

        self.assertEqual(len(split.parse_split(text)), 2)

    def test_a_bare_object_is_read(self):
        children = split.parse_split(
            _proposal(_child("T-1-a", [1]), _child("T-1-b", [2, 3]))
        )

        self.assertEqual([child.ticket_id for child in children], ["T-1-a", "T-1-b"])

    def test_prose_is_refused(self):
        with self.assertRaises(split.SplitRefused):
            split.parse_split("I would split this into a model half and a view half.")

    def test_a_child_claiming_nothing_is_refused(self):
        # The permissive reading — default `covers` to empty — turns the
        # invariant into a check that passes because it was asked nothing.
        with self.assertRaises(split.SplitRefused):
            split.parse_split(_proposal(_child("T-1-a", []), _child("T-1-b", [1])))

    def test_a_child_with_no_criteria_is_refused(self):
        with self.assertRaises(split.SplitRefused):
            split.parse_split(_proposal(_child("T-1-a", [1], criteria=[])))

    def test_an_empty_children_list_is_refused(self):
        with self.assertRaises(split.SplitRefused):
            split.parse_split('{"children": []}')


class TestTheInvariant(unittest.TestCase):
    """`split.check`: what may become children of this parent."""

    def test_a_decomposition_covering_every_criterion_is_accepted(self):
        parent = _parent()
        children = split.parse_split(
            _proposal(_child("T-1-a", [1, 2]), _child("T-1-b", [3]))
        )

        split.check(parent, children, max_children=8)

    def test_an_uncovered_criterion_refuses_the_split(self):
        # The failure this mechanism exists to prevent: four green children
        # over a parent nobody finished.
        parent = _parent()
        children = split.parse_split(
            _proposal(_child("T-1-a", [1]), _child("T-1-b", [2]))
        )

        with self.assertRaises(split.SplitRefused) as caught:
            split.check(parent, children, max_children=8)

        self.assertIn("holds c", str(caught.exception))

    def test_a_criterion_may_be_covered_twice(self):
        parent = _parent()
        children = split.parse_split(
            _proposal(_child("T-1-a", [1, 2]), _child("T-1-b", [2, 3]))
        )

        split.check(parent, children, max_children=8)

    def test_a_criterion_the_parent_does_not_have_is_refused(self):
        parent = _parent()
        children = split.parse_split(
            _proposal(_child("T-1-a", [1, 2]), _child("T-1-b", [3, 4]))
        )

        with self.assertRaises(split.SplitRefused):
            split.check(parent, children, max_children=8)

    def test_the_ratified_contract_is_what_must_be_covered(self):
        # A parent that invented criteria along the way cannot hand them down
        # as obligations: the split covers what somebody signed.
        parent = _parent(
            ratified_criteria=["holds a", "holds b"],
            criteria=["holds a", "holds b", "holds whatever respec added"],
        )
        children = split.parse_split(
            _proposal(_child("T-1-a", [1]), _child("T-1-b", [2]))
        )

        split.check(parent, children, max_children=8)

    def test_more_children_than_the_ceiling_is_refused(self):
        parent = _parent()
        children = split.parse_split(
            _proposal(*[_child(f"T-1-{n}", [1, 2, 3]) for n in range(4)])
        )

        with self.assertRaises(split.SplitRefused):
            split.check(parent, children, max_children=3)

    def test_a_single_child_is_a_respec_under_another_name(self):
        parent = _parent()
        children = split.parse_split(_proposal(_child("T-1-a", [1, 2, 3])))

        with self.assertRaises(split.SplitRefused):
            split.check(parent, children, max_children=8)

    def test_a_child_writing_outside_the_parents_scope_is_refused(self):
        parent = _parent()
        children = split.parse_split(
            _proposal(
                _child("T-1-a", [1, 2], allowed_files=["vendor/x.py"]),
                _child("T-1-b", [3]),
            )
        )

        with self.assertRaises(split.SplitRefused) as caught:
            split.check(parent, children, max_children=8)

        self.assertIn("vendor/x.py", str(caught.exception))

    def test_a_child_that_may_write_nothing_is_refused(self):
        parent = _parent()
        children = split.parse_split(
            _proposal(
                _child("T-1-a", [1, 2], allowed_files=[]), _child("T-1-b", [3])
            )
        )

        with self.assertRaises(split.SplitRefused):
            split.check(parent, children, max_children=8)

    def test_two_children_sharing_an_id_are_refused(self):
        parent = _parent()
        children = split.parse_split(
            _proposal(_child("T-1-a", [1, 2]), _child("T-1-a", [3]))
        )

        with self.assertRaises(split.SplitRefused):
            split.check(parent, children, max_children=8)

    def test_a_child_that_waits_on_its_own_parent_is_refused(self):
        # A cycle by construction: the parent closes when its children finish,
        # so a child waiting on the parent waits forever. Observed live on the
        # second split this mechanism made — both children skipped as
        # unreachable and the run reported done over the deadlock.
        parent = _parent()
        children = split.parse_split(
            _proposal(
                _child("T-1-a", [1, 2], needs=["T-1"]), _child("T-1-b", [3])
            )
        )

        with self.assertRaises(split.SplitRefused) as caught:
            split.check(parent, children, max_children=8)

        self.assertIn("sibling", str(caught.exception))

    def test_a_child_waiting_on_a_sibling_is_accepted(self):
        parent = _parent()
        children = split.parse_split(
            _proposal(
                _child("T-1-a", [1, 2]), _child("T-1-b", [3], needs=["T-1-a"])
            )
        )

        split.check(parent, children, max_children=8)

    def test_siblings_that_wait_on_each_other_are_refused(self):
        parent = _parent()
        children = split.parse_split(
            _proposal(
                _child("T-1-a", [1, 2], needs=["T-1-b"]),
                _child("T-1-b", [3], needs=["T-1-a"]),
            )
        )

        with self.assertRaises(split.SplitRefused) as caught:
            split.check(parent, children, max_children=8)

        self.assertIn("cycle", str(caught.exception))

    def test_a_parent_with_no_contract_cannot_be_split(self):
        parent = _parent(criteria=[], original_criteria=[])
        children = split.parse_split(
            _proposal(_child("T-1-a", [1]), _child("T-1-b", [1]))
        )

        with self.assertRaises(split.SplitRefused):
            split.check(parent, children, max_children=8)


class TestWhatMayBeSplitAtAll(unittest.TestCase):
    """`split.splittable`, which answers in sentences a log line can carry."""

    def test_an_ordinary_stalled_ticket_may_be_split(self):
        self.assertEqual(split.splittable(_parent(), max_depth=2), "")

    def test_a_bug_ticket_is_never_split(self):
        # Its contract is a reproduction written before the fix, by a tester.
        # The party being judged does not restate it in smaller pieces.
        reason = split.splittable(_parent(kind="bug"), max_depth=2)

        self.assertIn("reproduction", reason)

    def test_a_ticket_at_the_depth_limit_is_not_split_again(self):
        reason = split.splittable(_parent(split_depth=2), max_depth=2)

        self.assertIn("deep", reason)

    def test_a_withheld_ticket_is_not_split(self):
        reason = split.splittable(_parent(route="withheld:security"), max_depth=2)

        self.assertIn("withheld", reason)


class TestTheChildrenThatComeOut(unittest.TestCase):
    """`split.children_of`: what a child inherits, and what it does not."""

    def _children(self, parent: Ticket | None = None) -> list[Ticket]:
        parent = parent or _parent(
            baseline_tree="abc123",
            context="the project uses tabs",
            learned=[{"text": "flake8 caps lines at 100", "count": 3}],
        )
        proposed = split.parse_split(
            _proposal(_child("T-1-a", [1, 2]), _child("T-1-b", [3]))
        )
        return split.children_of(parent, proposed)

    def test_a_child_is_judged_against_the_parents_own_words(self):
        # The defect the first live split shipped. The planner claimed a
        # parent criterion and restated it as the half that could be
        # satisfied; index coverage approved it, both children passed, and an
        # unsatisfiable backlog was reported done.
        parent = _parent(
            criteria=["count_words strips punctuation, so summarize reads 2"],
            original_criteria=["count_words strips punctuation, so summarize reads 2"],
        )
        proposed = split.parse_split(
            _proposal(
                _child("T-1-a", [1], criteria=["summarize reads 2"]),
                _child("T-1-b", [1], criteria=["summarize reads 2"]),
            )
        )

        first, _ = split.children_of(parent, proposed)

        self.assertIn(
            "count_words strips punctuation, so summarize reads 2", first.criteria
        )

    def test_a_childs_own_criteria_are_kept_after_the_inherited_ones(self):
        # They narrow the child's work; they do not replace what it inherited.
        parent = _parent()
        proposed = split.parse_split(
            _proposal(
                _child("T-1-a", [1], criteria=["and the file parses"]),
                _child("T-1-b", [2, 3]),
            )
        )

        first, _ = split.children_of(parent, proposed)

        self.assertEqual(first.criteria[0], "holds a")
        self.assertIn("and the file parses", first.criteria)

    def test_an_inherited_criterion_is_frozen_as_the_childs_original(self):
        # So the ratchet protects the parent's wording from a later respec
        # exactly as it protects a plan's.
        parent = _parent()
        proposed = split.parse_split(
            _proposal(_child("T-1-a", [1, 2]), _child("T-1-b", [3]))
        )

        first, _ = split.children_of(parent, proposed)

        self.assertEqual(first.original_criteria, first.criteria)
        self.assertIn("holds a", first.original_criteria)

    def test_each_child_records_what_it_covers(self):
        first, second = self._children()

        self.assertEqual(first.covers, [1, 2])
        self.assertEqual(second.covers, [3])

    def test_each_child_names_its_parent_and_its_depth(self):
        first, _ = self._children()

        self.assertEqual(first.parent_ticket_id, "T-1")
        self.assertEqual(first.split_depth, 1)

    def test_a_child_inherits_the_parents_baseline_tree(self):
        # A child taking its own baseline is judged against a tree its parent
        # already reddened, and is then blamed for its parent's red.
        first, _ = self._children()

        self.assertEqual(first.baseline_tree, "abc123")

    def test_a_child_inherits_what_the_parent_learned(self):
        first, _ = self._children()

        self.assertEqual(first.learned[0]["text"], "flake8 caps lines at 100")

    def test_a_child_is_told_which_parent_criteria_are_its_own(self):
        first, _ = self._children()

        self.assertIn("holds a", first.original_context)
        self.assertIn("holds b", first.original_context)
        self.assertNotIn("holds c", first.original_context)

    def test_a_child_carries_no_tests_from_its_parent(self):
        # `freezeTests` fingerprints criteria, spec and scope; a child differs
        # in all three, so its tests are written fresh against its own
        # contract rather than inherited.
        first, _ = self._children()

        self.assertEqual(first.tests_fingerprint, "")

    def test_children_sort_after_their_parent(self):
        first, second = self._children()

        self.assertLess(first.position, second.position)


class TestTheTriggerInTheLoop(unittest.TestCase):
    """`_consider_split`: when the loop asks, and when it does not."""

    def _orchestrator(self, **loop_settings) -> tuple[Orchestrator, Store, int]:
        root = Path(tempfile.mkdtemp()).resolve()
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
                **{"respec_on_retry": False, "preflight": False, **loop_settings}
            ),
        )
        store = Store(config.db_path)
        run_id = store.create_run("goal")
        self.addCleanup(store.close)
        return Orchestrator(config, store), store, run_id

    def _answer(self, orchestrator: Orchestrator, text: str) -> list:
        asked: list = []

        def converse(*args, **kwargs):
            asked.append(args)
            return Completion(text=text, usage=Usage())

        orchestrator._converse = converse
        return asked

    def test_the_default_configuration_never_splits(self):
        # The whole point of §4.1: no threshold on the only data available
        # separates a ticket worth decomposing from one about to land, so the
        # brake ships off.
        orchestrator, store, run_id = self._orchestrator()
        ticket = _parent(distinct_classes=99)
        store.add_tickets(run_id, [ticket])
        asked = self._answer(orchestrator, _proposal(_child("T-1-a", [1, 2, 3])))

        self.assertFalse(orchestrator._consider_split(run_id, ticket))
        self.assertEqual(asked, [])

    def test_a_ticket_below_the_threshold_is_left_alone(self):
        orchestrator, store, run_id = self._orchestrator(volume_threshold=8)
        ticket = _parent(distinct_classes=7)
        store.add_tickets(run_id, [ticket])
        asked = self._answer(orchestrator, "")

        self.assertFalse(orchestrator._consider_split(run_id, ticket))
        self.assertEqual(asked, [])

    def test_a_ticket_past_the_threshold_is_decomposed(self):
        orchestrator, store, run_id = self._orchestrator(volume_threshold=8)
        ticket = _parent(distinct_classes=12)
        store.add_tickets(run_id, [ticket])
        self._answer(
            orchestrator,
            _proposal(_child("T-1-a", [1, 2]), _child("T-1-b", [3])),
        )

        self.assertTrue(orchestrator._consider_split(run_id, ticket))

        ids = [t.ticket_id for t in store.list_tickets(run_id)]
        self.assertEqual(sorted(ids), ["T-1", "T-1-a", "T-1-b"])
        self.assertEqual(store.list_tickets(run_id)[0].status, "split")

    def test_a_refused_proposal_leaves_the_ticket_whole(self):
        orchestrator, store, run_id = self._orchestrator(volume_threshold=8)
        ticket = _parent(distinct_classes=12)
        store.add_tickets(run_id, [ticket])
        # Covers 1 and 2; nobody claims 3.
        self._answer(
            orchestrator,
            _proposal(_child("T-1-a", [1]), _child("T-1-b", [2])),
        )

        self.assertFalse(orchestrator._consider_split(run_id, ticket))
        self.assertEqual([t.ticket_id for t in store.list_tickets(run_id)], ["T-1"])
        self.assertEqual(store.list_tickets(run_id)[0].status, "pending")

    def test_a_proposal_reusing_an_existing_id_is_refused(self):
        orchestrator, store, run_id = self._orchestrator(volume_threshold=8)
        ticket = _parent(distinct_classes=12)
        store.add_tickets(run_id, [ticket, Ticket("T-1-a", position=2)])
        self._answer(
            orchestrator,
            _proposal(_child("T-1-a", [1, 2]), _child("T-1-b", [3])),
        )

        self.assertFalse(orchestrator._consider_split(run_id, ticket))
        self.assertEqual(len(store.list_tickets(run_id)), 2)

    def test_a_bug_ticket_past_the_threshold_is_not_asked_about(self):
        orchestrator, store, run_id = self._orchestrator(volume_threshold=8)
        ticket = _parent(distinct_classes=40, kind="bug")
        store.add_tickets(run_id, [ticket])
        asked = self._answer(orchestrator, "")

        self.assertFalse(orchestrator._consider_split(run_id, ticket))
        self.assertEqual(asked, [])


class TestTheParentAsAGate(unittest.TestCase):
    """`_close_split_parents`: a parent completes when its children do."""

    def _run(self) -> tuple[Orchestrator, Store, int]:
        root = Path(tempfile.mkdtemp()).resolve()
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
            loop=LoopSettings(**{"respec_on_retry": False, "preflight": False}),
        )
        store = Store(config.db_path)
        run_id = store.create_run("goal")
        self.addCleanup(store.close)
        store.add_tickets(
            run_id,
            [
                _parent(status="split"),
                Ticket("T-1-a", parent_ticket_id="T-1", position=101),
                Ticket("T-1-b", parent_ticket_id="T-1", position=102),
            ],
        )
        return Orchestrator(config, store), store, run_id

    def _set(self, store: Store, run_id: int, ticket_id: str, status: str) -> None:
        ticket = [t for t in store.list_tickets(run_id) if t.ticket_id == ticket_id][0]
        ticket.status = status
        store.update_ticket(run_id, ticket)

    def _status(self, store: Store, run_id: int, ticket_id: str) -> str:
        return [t for t in store.list_tickets(run_id) if t.ticket_id == ticket_id][
            0
        ].status

    def test_a_parent_waits_while_one_child_is_unfinished(self):
        orchestrator, store, run_id = self._run()
        self._set(store, run_id, "T-1-a", "done")

        orchestrator._close_split_parents(run_id)

        self.assertEqual(self._status(store, run_id, "T-1"), "split")

    def test_a_parent_is_done_when_every_child_is(self):
        orchestrator, store, run_id = self._run()
        self._set(store, run_id, "T-1-a", "done")
        self._set(store, run_id, "T-1-b", "done")

        orchestrator._close_split_parents(run_id)

        self.assertEqual(self._status(store, run_id, "T-1"), "done")

    def test_a_parked_child_parks_the_parent(self):
        # A parent reported done over three green children and one blocked one
        # is the outcome the invariant exists to prevent, one level up.
        orchestrator, store, run_id = self._run()
        self._set(store, run_id, "T-1-a", "done")
        self._set(store, run_id, "T-1-b", "blocked")

        orchestrator._close_split_parents(run_id)

        self.assertEqual(self._status(store, run_id, "T-1"), "blocked")
        note = [t for t in store.list_tickets(run_id) if t.ticket_id == "T-1"][
            0
        ].blocked_note
        self.assertIn("T-1-b", note)

    def test_a_run_holding_an_open_gate_is_not_reported_done(self):
        # The gate is unfinished work carrying no failure of its own: what
        # failed is a child, and a child parked as unreachable is `skipped`,
        # which the finish tally does not count. A run reported `done` over
        # exactly that.
        orchestrator, store, run_id = self._run()
        self._set(store, run_id, "T-1-a", "skipped")
        self._set(store, run_id, "T-1-b", "skipped")
        orchestrator._verify_plan = lambda: []

        self.assertEqual(orchestrator._finish(run_id), "blocked")

    def test_no_ordering_edge_is_derived_onto_a_gate(self):
        # The deadlock the third live run produced, and the reason it was
        # structural rather than a model slip: a child writes its parent's
        # files by construction, so the shared-file rule ordered the child
        # behind the parent — which is waiting for the child. `respec` reruns
        # that rule on every revision, so the split-time checks could not see
        # it coming.
        from forge.ingest import derive_needs

        parent = _parent(status="split", allowed_files=["a.py"], position=0)
        first = Ticket(
            "T-1-a", parent_ticket_id="T-1", allowed_files=["a.py"], position=1
        )
        second = Ticket(
            "T-1-b", parent_ticket_id="T-1", allowed_files=["a.py"], position=2
        )

        added = derive_needs([parent, first, second])

        self.assertNotIn("T-1", first.needs)
        self.assertNotIn("T-1", second.needs)
        # Siblings sharing a file are still ordered against each other.
        self.assertEqual([edge[:2] for edge in added], [("T-1-b", "T-1-a")])

    def test_a_ticket_that_was_never_split_is_untouched(self):
        orchestrator, store, run_id = self._run()
        self._set(store, run_id, "T-1", "pending")

        orchestrator._close_split_parents(run_id)

        self.assertEqual(self._status(store, run_id, "T-1"), "pending")


class TestASplitCycleKeepsTheRunGoing(unittest.TestCase):
    """The defect the first live split found.

    `_consider_split` takes the parent out of `eligible` — it is a gate now,
    not work — and puts its children on the backlog as pending tickets. When
    the parent was the only eligible ticket, the list empties, and the guard
    that reads an empty list as "nothing left to retry" ended the run with two
    freshly created children pending and never worked. Observed live on
    2026-09-07: `Finished: blocked  tickets: {"pending": 2, "split": 1}`.
    """

    def _orchestrator(self, **loop_settings) -> tuple[Orchestrator, Store, int]:
        root = Path(tempfile.mkdtemp()).resolve()
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
                    "retry_cycles": 3,
                    "volume_threshold": 2,
                    **loop_settings,
                }
            ),
        )
        store = Store(config.db_path)
        run_id = store.create_run("goal")
        self.addCleanup(store.close)
        orchestrator = Orchestrator(config, store)
        orchestrator._shell = lambda *a, **k: None
        return orchestrator, store, run_id

    def _failed_parent(self, store: Store, run_id: int) -> None:
        ticket = _parent(status="failed", distinct_classes=0)
        store.add_tickets(run_id, [ticket])
        for name, detail in (
            ("lint", "a.py:1:1: E501 line too long"),
            ("tests", "b.py:2: AssertionError: 1 != 2"),
        ):
            step_id = store.start_step(run_id, "T-1", name)
            store.end_step(step_id, "failed", detail)

    def test_a_cycle_that_only_split_still_continues_the_run(self):
        orchestrator, store, run_id = self._orchestrator()
        self._failed_parent(store, run_id)
        orchestrator._converse = lambda *a, **k: Completion(
            text=_proposal(_child("T-1-a", [1, 2]), _child("T-1-b", [3])),
            usage=Usage(),
        )

        self.assertIs(orchestrator._retry_cycle(run_id, "blocked"), True)

        statuses = {t.ticket_id: t.status for t in store.list_tickets(run_id)}
        self.assertEqual(statuses["T-1"], "split")
        self.assertEqual(statuses["T-1-a"], "pending")
        self.assertEqual(statuses["T-1-b"], "pending")

    def test_the_split_cycle_is_charged_to_the_retry_budget(self):
        # A cycle that produced work without spending one of the cycles a
        # person budgeted is a cycle the budget cannot see.
        orchestrator, store, run_id = self._orchestrator()
        self._failed_parent(store, run_id)
        orchestrator._converse = lambda *a, **k: Completion(
            text=_proposal(_child("T-1-a", [1, 2]), _child("T-1-b", [3])),
            usage=Usage(),
        )

        orchestrator._retry_cycle(run_id, "blocked")

        self.assertEqual(store.get_control(f"retries:{run_id}", "0"), "1")

    def test_a_cycle_that_split_nothing_still_ends_a_backlog_with_nothing_to_do(self):
        # The guard the branch above sits in front of keeps its own case: a
        # withheld ticket is requeued into being withheld again.
        orchestrator, store, run_id = self._orchestrator(volume_threshold=0)
        store.add_tickets(
            run_id, [Ticket("T-9", route="withheld:security", status="skipped")]
        )

        self.assertIs(orchestrator._retry_cycle(run_id, "blocked"), False)


if __name__ == "__main__":
    unittest.main()
