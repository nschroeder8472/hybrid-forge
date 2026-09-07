"""The dashboard's usage rows: input and output carried separately.

US-001. `snapshot()` already sums each model's usage into one row and shows
its `total` and `display`. These tests pin the split beside it — `input`
(fresh prompt plus cache writes and reads), `output` (completions), and the
two display strings — and that the row's existing keys are untouched.

    python -m unittest discover tests
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from forge.config import Config
from forge.state import Store
from forge.ui import server as ui_server


class TestUsageRowsCarryInputAndOutput(unittest.TestCase):
    """The four new keys on each row of `snapshot()["usage"]`."""

    def _config(self, root):
        return Config(
            root=root,
            models={"m": {"kind": "openai", "model": "x", "contextWindow": 8192}},
            roles={r: "m" for r in ("planner", "executor", "tester", "reviewer")},
        )

    def _store(self, root):
        store = Store(root / "t.db")
        store.create_run("goal")
        return store

    def test_one_call_splits_into_input_and_output(self):
        root = Path(tempfile.mkdtemp()).resolve()
        store = self._store(root)
        store.record_usage(
            "m",
            prompt_tokens=100,
            completion_tokens=20,
            cache_creation_tokens=5,
            cache_read_tokens=7,
        )

        state = ui_server.snapshot(store, self._config(root))

        self.assertEqual(len(state["usage"]), 1)
        row = state["usage"][0]
        self.assertEqual(row["input"], 112)
        self.assertEqual(row["output"], 20)

    def test_input_plus_output_equals_the_row_total(self):
        root = Path(tempfile.mkdtemp()).resolve()
        store = self._store(root)
        store.record_usage(
            "m",
            prompt_tokens=100,
            completion_tokens=20,
            cache_creation_tokens=5,
            cache_read_tokens=7,
        )

        state = ui_server.snapshot(store, self._config(root))

        row = state["usage"][0]
        self.assertEqual(row["input"] + row["output"], row["total"])

    def test_the_display_strings_follow_the_split(self):
        root = Path(tempfile.mkdtemp()).resolve()
        store = self._store(root)
        store.record_usage(
            "m",
            prompt_tokens=1500,
            completion_tokens=0,
            cache_creation_tokens=0,
            cache_read_tokens=0,
        )

        state = ui_server.snapshot(store, self._config(root))

        row = state["usage"][0]
        self.assertEqual(row["input_display"], "1.5k")
        self.assertEqual(row["output_display"], "0")

    def test_two_calls_against_one_model_sum_into_one_row(self):
        root = Path(tempfile.mkdtemp()).resolve()
        store = self._store(root)
        store.record_usage(
            "m",
            prompt_tokens=100,
            completion_tokens=20,
            cache_creation_tokens=5,
            cache_read_tokens=7,
        )
        store.record_usage(
            "m",
            prompt_tokens=300,
            completion_tokens=40,
            cache_creation_tokens=10,
            cache_read_tokens=13,
        )

        state = ui_server.snapshot(store, self._config(root))

        self.assertEqual(len(state["usage"]), 1)
        row = state["usage"][0]
        self.assertEqual(row["input"], (100 + 5 + 7) + (300 + 10 + 13))
        self.assertEqual(row["output"], 20 + 40)

    def test_the_existing_row_keys_are_untouched(self):
        root = Path(tempfile.mkdtemp()).resolve()
        store = self._store(root)
        store.record_usage(
            "m",
            prompt_tokens=100,
            completion_tokens=20,
            cache_creation_tokens=5,
            cache_read_tokens=7,
            cost_usd=0.42,
        )

        state = ui_server.snapshot(store, self._config(root))

        row = state["usage"][0]
        self.assertEqual(row["total"], 132)
        self.assertEqual(row["display"], "132")
        self.assertEqual(row["cached"], 12)
        self.assertEqual(row["cost_display"], "$0.42")

    def test_no_run_and_no_calls_returns_an_empty_usage_list(self):
        root = Path(tempfile.mkdtemp()).resolve()
        store = Store(root / "t.db")

        state = ui_server.snapshot(store, self._config(root))

        self.assertIsNone(state["run"])
        self.assertEqual(state["usage"], [])
