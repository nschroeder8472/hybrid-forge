"""RT-001: `Store.clear_step_detail` empties the transcripts of pruned runs.

The step rows stay — only their `detail` goes — and the return value reports
how many rows changed and how many UTF-8 bytes of `detail` they held.

    python -m unittest discover tests
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from forge.cli import cmd_prune
from forge.state import Store


class TestClearStepDetail(unittest.TestCase):
    """The retention half of `forge prune`, on the database side."""

    def _store(self, root):
        return Store(root / "run.db")

    def _steps(self, store, run_id, details):
        """One step per detail, closed `ok` so the detail is stored verbatim."""
        for detail in details:
            step_id = store.start_step(run_id, "T-001", "build")
            store.end_step(step_id, "ok", detail)

    def test_empty_run_ids_changes_nothing(self):
        root = Path(tempfile.mkdtemp()).resolve()
        store = self._store(root)
        first = store.create_run("first")
        self._steps(store, first, ["alpha", "beta"])

        self.assertEqual(store.clear_step_detail([]), (0, 0))

        rows = store._connection.execute(
            "SELECT detail FROM steps WHERE run_id = ?", (first,)
        ).fetchall()
        self.assertEqual([row["detail"] for row in rows], ["alpha", "beta"])

    def test_returns_row_count_and_utf8_byte_count(self):
        root = Path(tempfile.mkdtemp()).resolve()
        store = self._store(root)
        first = store.create_run("first")
        second = store.create_run("second")
        self._steps(store, first, ["abc", "héllo"])
        self._steps(store, second, ["gamma"])

        rows, total = store.clear_step_detail([first])

        self.assertEqual(rows, 2)
        # "héllo" is five characters but six UTF-8 bytes.
        self.assertEqual(
            total, len("abc".encode("utf-8")) + len("héllo".encode("utf-8"))
        )
        self.assertEqual(total, 3 + 6)

    def test_only_the_named_run_loses_its_detail(self):
        root = Path(tempfile.mkdtemp()).resolve()
        store = self._store(root)
        first = store.create_run("first")
        second = store.create_run("second")
        self._steps(store, first, ["alpha", "beta"])
        self._steps(store, second, ["gamma"])

        store.clear_step_detail([first])

        first_details = [
            row["detail"]
            for row in store._connection.execute(
                "SELECT detail FROM steps WHERE run_id = ? ORDER BY id", (first,)
            )
        ]
        second_details = [
            row["detail"]
            for row in store._connection.execute(
                "SELECT detail FROM steps WHERE run_id = ? ORDER BY id", (second,)
            )
        ]
        self.assertEqual(first_details, ["", ""])
        self.assertEqual(second_details, ["gamma"])

    def test_status_is_unchanged(self):
        root = Path(tempfile.mkdtemp()).resolve()
        store = self._store(root)
        first = store.create_run("first")
        self._steps(store, first, ["alpha"])

        store.clear_step_detail([first])

        rows = store._connection.execute(
            "SELECT status FROM steps WHERE run_id = ?", (first,)
        ).fetchall()
        self.assertEqual([row["status"] for row in rows], ["ok"])

    def test_second_call_finds_nothing_left_to_clear(self):
        root = Path(tempfile.mkdtemp()).resolve()
        store = self._store(root)
        first = store.create_run("first")
        self._steps(store, first, ["alpha"])

        store.clear_step_detail([first])
        self.assertEqual(store.clear_step_detail([first]), (0, 0))


class TestPruneDryRunKeepsStepDetail(unittest.TestCase):
    """`forge prune --dry-run` reports and stops; the database is untouched."""

    def test_dry_run_leaves_every_detail_in_place(self):
        root = Path(tempfile.mkdtemp()).resolve()
        (root / ".hybridforge").mkdir()
        (root / ".hybridforge" / "config.json").write_text(
            '{"models": {"m": {"kind": "openai", "model": "x"}}, '
            '"roles": {"planner": "m", "executor": "m", "tester": "m", '
            '"reviewer": "m"}}',
            encoding="utf-8",
        )
        store = Store(root / ".hybridforge" / "run.db")
        first = store.create_run("first")
        store.create_run("second")
        step_id = store.start_step(first, "T-001", "build")
        store.end_step(step_id, "ok", "transcript of the doomed run")
        for name in ("run-1", "run-2"):
            (root / ".hybridforge" / "artifacts" / name).mkdir(parents=True)

        args = type("Args", (), {"root": str(root), "keep": 1, "dry_run": True})()
        self.assertEqual(cmd_prune(args), 0)

        row = store._connection.execute(
            "SELECT detail FROM steps WHERE id = ?", (step_id,)
        ).fetchone()
        self.assertEqual(row["detail"], "transcript of the doomed run")
        self.assertTrue((root / ".hybridforge" / "artifacts" / "run-1").is_dir())
