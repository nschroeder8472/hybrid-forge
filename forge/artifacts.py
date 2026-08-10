"""A durable, readable record of what each step actually did.

The database already knows what happened, but it knows it in a shape you can
only ask questions of with a SQL client, and it clips step detail at 20k
characters — which is exactly the review text you want when a ticket failed at
2am for reasons nobody watched.

So every step also lands on disk, one directory per attempt:

    .hybridforge/artifacts/run-1/SL-001/attempt-2/
        01-build.json     what the call cost and how it ended
        01-build.md       the completion, untruncated
        05-review.json    the verdict, parsed and raw

`grep -l '"approved": false'` across a night's run answers "which tickets did
the reviewer refuse" without a query, and the per-step JSON is the bake-off
data — empty-parse rate, first-try pass rate, false BLOCKED — as a by-product
of running rather than something to reconstruct afterwards.

Two rules this module keeps:

**It never ends a run.** A full disk, a read-only mount, a path the filesystem
dislikes — all of them lose the record, none of them stop the work. Every write
is best-effort and failures are reported once, not per step.

**It stays out of the diff.** `_diff()` builds the reviewer's changeset with
`git add -N .`, so anything untracked in the working tree becomes part of what
the reviewer is asked to approve. An artifact directory that leaked into that
would put the reviewer's own previous verdict in front of it. The gitignore is
written before the first artifact is, and repaired on every run rather than
only at `forge init`, so repos initialized by an older version are covered too.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

ARTIFACTS_DIR = "artifacts"

# Entries `.hybridforge/.gitignore` must carry. `run.db*` is what `forge init`
# has always written; artifacts are new, and older repos will not have it.
_GITIGNORE_LINES = ("run.db", "run.db-wal", "run.db-shm", ARTIFACTS_DIR + "/")

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_name(value: str, fallback: str = "unnamed") -> str:
    """A path segment that cannot escape its directory or upset Windows.

    Ticket ids come from a planner model, so they are text a model chose, not
    an identifier this project controls.
    """
    cleaned = _UNSAFE.sub("-", str(value)).strip("-.")
    return cleaned[:64] or fallback


class Artifacts:
    """Writes step records under `.hybridforge/artifacts/run-<id>/`."""

    def __init__(self, config_dir: Path, run_id: int, *, enabled: bool = True):
        self.base = Path(config_dir) / ARTIFACTS_DIR / f"run-{run_id}"
        self.enabled = enabled
        # Set on the first failure so a broken disk reports once rather than
        # once per step for the rest of the night.
        self.failure: str = ""
        self._sequence: dict[Path, int] = {}
        if self.enabled:
            self._ensure_gitignore(Path(config_dir))

    # ------------------------------------------------------------------

    def _ensure_gitignore(self, config_dir: Path) -> None:
        path = config_dir / ".gitignore"
        try:
            config_dir.mkdir(parents=True, exist_ok=True)
            existing = path.read_text(encoding="utf-8") if path.exists() else ""
            present = {line.strip() for line in existing.splitlines()}
            missing = [line for line in _GITIGNORE_LINES if line not in present]
            if not missing:
                return
            prefix = "" if not existing or existing.endswith("\n") else "\n"
            path.write_text(existing + prefix + "\n".join(missing) + "\n", encoding="utf-8")
        except OSError as exc:
            # Losing the record is survivable. Writing artifacts into a diff
            # the reviewer reads is not, so the directory stays unused.
            self.enabled = False
            self.failure = f"could not secure {path}: {exc}"

    def _attempt_dir(self, ticket_id: str, attempt: int) -> Path:
        return self.base / safe_name(ticket_id, "ticket") / f"attempt-{max(1, attempt)}"

    def _next_index(self, directory: Path) -> int:
        self._sequence[directory] = self._sequence.get(directory, 0) + 1
        return self._sequence[directory]

    # ------------------------------------------------------------------

    def record(
        self,
        ticket_id: str,
        attempt: int,
        name: str,
        payload: dict[str, Any],
        *,
        raw: str = "",
    ) -> None:
        """Write one step's record. Never raises."""
        if not self.enabled:
            return

        directory = self._attempt_dir(ticket_id, attempt)
        index = self._next_index(directory)
        stem = f"{index:02d}-{safe_name(name, 'step')}"

        document = {
            "ticket": ticket_id,
            "attempt": max(1, attempt),
            "step": name,
            "recorded_at": time.time(),
            **payload,
        }

        try:
            directory.mkdir(parents=True, exist_ok=True)
            (directory / f"{stem}.json").write_text(
                json.dumps(document, indent=2, default=str) + "\n", encoding="utf-8"
            )
            if raw:
                # Beside the envelope rather than inside it: a 20k-character
                # completion embedded in JSON is unreadable, and reading these
                # by eye is the entire point.
                (directory / f"{stem}.md").write_text(raw, encoding="utf-8")
            self._append_manifest(document)
        except OSError as exc:
            if not self.failure:
                self.failure = f"could not write {directory}: {exc}"

    def _append_manifest(self, document: dict[str, Any]) -> None:
        """One line per step for the whole run, for counting rather than reading."""
        line = json.dumps({k: v for k, v in document.items() if k != "raw"}, default=str)
        with (self.base / "steps.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
