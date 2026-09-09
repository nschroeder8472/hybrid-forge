"""RT-002: `forge prune` clears the step detail of the runs it removes.

The step rows stay — only their `detail` goes — and the dry run reports what
would be cleared without touching the database.

    python -m unittest discover tests
"""

from __future__ import annotations

import argparse
import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path

from forge.artifacts import ARTIFACTS_DIR
from forge.cli import _megabytes, cmd_prune
from forge.config import default_config
from forge.state import Store

# Just under `state.DETAIL_CHARS`, so nothing is clipped, and enough of it
# that a vacuum has something to reclaim.
OLD_DETAIL = "old step transcript " * 900
NEW_DETAIL = "new step transcript " * 900


def _workspace(root: Path) -> tuple[object, Store]:
    config = default_config(root)
    config.write()
    return config, Store(config.db_path)


def _seed(config, store) -> tuple[int, int]:
    old = store.create_run(goal="older run")
    new = store.create_run(goal="newer run")
    for run_id, detail in ((old, OLD_DETAIL), (new, NEW_DETAIL)):
        for step in ("lint", "test"):
            step_id = store.start_step(run_id, "TT-001", step)
            store.end_step(step_id, "ok", detail)
    base = config.config_dir / ARTIFACTS_DIR
    for run_id in (old, new):
        directory = base / f"run-{run_id}"
        directory.mkdir(parents=True)
        (directory / "attempt-1.txt").write_text("transcript", encoding="utf-8")
    return old, new


def _args(root: Path, *, keep: int, dry_run: bool) -> argparse.Namespace:
    return argparse.Namespace(root=str(root), keep=keep, dry_run=dry_run)


def _steps(store, run_id) -> list[tuple[str, str]]:
    connection = sqlite3.connect(str(store.path))
    try:
        rows = connection.execute(
            "SELECT status, detail FROM steps WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
    finally:
        connection.close()
    return rows


class PruneRetentionTest(unittest.TestCase):
    def test_prune_clears_only_the_doomed_run(self):
        root = Path(tempfile.mkdtemp()).resolve()
        config, store = _workspace(root)
        old, new = _seed(config, store)
        self.assertEqual(_steps(store, old)[0][1], OLD_DETAIL)

        self.assertEqual(cmd_prune(_args(root, keep=1, dry_run=False)), 0)

        old_rows = _steps(store, old)
        self.assertTrue(old_rows)
        self.assertTrue(all(detail == "" for _status, detail in old_rows))
        self.assertTrue(all(status == "ok" for status, _detail in old_rows))
        new_rows = _steps(store, new)
        self.assertTrue(all(detail == NEW_DETAIL for _status, detail in new_rows))
        self.assertTrue(all(status == "ok" for status, _detail in new_rows))

    def test_dry_run_changes_nothing(self):
        root = Path(tempfile.mkdtemp()).resolve()
        config, store = _workspace(root)
        old, new = _seed(config, store)
        before = {run_id: _steps(store, run_id) for run_id in (old, new)}

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cmd_prune(_args(root, keep=1, dry_run=True)), 0)
        printed = out.getvalue()

        self.assertEqual(_steps(store, old), before[old])
        self.assertEqual(_steps(store, new), before[new])
        line = next((line for line in printed.splitlines() if "detail" in line), "")
        self.assertIn(str(len(before[old])), line)

    def test_prune_reports_and_shrinks(self):
        root = Path(tempfile.mkdtemp()).resolve()
        config, store = _workspace(root)
        old, _new = _seed(config, store)
        doomed_rows = len(_steps(store, old))
        # Closed before the size is taken: an open connection keeps the seed
        # in the WAL, and the main file alone would not be the size the
        # prune is being asked to shrink.
        store.close()
        before = config.db_path.stat().st_size

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cmd_prune(_args(root, keep=1, dry_run=False)), 0)
        printed = out.getvalue()

        self.assertIn(str(doomed_rows), printed)
        self.assertLess(config.db_path.stat().st_size, before)

    def test_the_size_it_reports_is_the_size_it_leaves_behind(self):
        """The report used to be about where the bytes were, not how many.

        `VACUUM` in WAL mode rebuilds the database into the log, so the main
        file kept its old size until something closed the connection — which
        for this command was interpreter exit, after it had already printed
        `860 KB -> 860 KB` about a file that shrank to 464 KB a moment later.
        """
        root = Path(tempfile.mkdtemp()).resolve()
        config, store = _workspace(root)
        _seed(config, store)
        store.close()

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cmd_prune(_args(root, keep=1, dry_run=False)), 0)
        line = next(
            line for line in out.getvalue().splitlines() if line.startswith("Vacuumed")
        )

        # Nothing else touches the file between the command and this read, so
        # the number it printed has to be the number on disk.
        left = config.db_path.stat().st_size
        self.assertIn(_megabytes(left), line.rsplit("->", 1)[1])
        self.assertNotEqual(
            line.rsplit("->", 1)[1].strip().rstrip("."),
            line.rsplit("->", 1)[0].split(":", 1)[1].strip(),
        )


if __name__ == "__main__":
    unittest.main()
