"""Copy `examples/sample-project` somewhere a run may write.

A forge run writes code, a database and an artifact tree. Run one against the
committed fixture and the next test reads the last run's output as the
fixture's own state — so the fixture is copied out before it is used, and the
copy is what gets dirtied.

    python scripts/sample_workspace.py             # a temp directory
    python scripts/sample_workspace.py /tmp/try    # or a path you name
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

# Run state, caches and anything a previous run left behind. Copying these
# would hand the new run a database describing a different tree.
SKIP = shutil.ignore_patterns(
    "run.db",
    "run.db-wal",
    "run.db-shm",
    "artifacts",
    "abandoned",
    "tickets",
    "__pycache__",
    "*.pyc",
    ".git",
)

SAMPLE = Path(__file__).resolve().parent.parent / "examples" / "sample-project"


def copy_sample(destination: Path | str | None = None) -> Path:
    """Copy the fixture to `destination` and return where it landed.

    An existing destination is an error rather than a merge: the copy is meant
    to be a clean starting tree, and a merge into a used one is the state this
    script exists to avoid.
    """
    if not (SAMPLE / ".hybridforge" / "config.json").exists():
        raise FileNotFoundError(f"no sample project at {SAMPLE}")

    target = (
        Path(tempfile.mkdtemp(prefix="forge-sample-")) / "sample-project"
        if destination is None
        else Path(destination)
    )
    if target.exists():
        raise FileExistsError(f"{target} already exists; name a path that does not")

    shutil.copytree(SAMPLE, target, ignore=SKIP)
    return target


def main(argv: list[str]) -> int:
    try:
        target = copy_sample(argv[0] if argv else None)
    except (FileNotFoundError, FileExistsError) as exc:
        print(f"error: {exc}")
        return 1
    print(target)
    print(f"\n  cd {target}")
    print("  forge --root . doctor")
    print("  forge ingest SPEC.md")
    print("  forge go")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
