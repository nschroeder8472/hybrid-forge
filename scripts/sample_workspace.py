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
import subprocess
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


def init_repo(target: Path) -> bool:
    """Make the copy a git repository with the fixture as its first commit.

    Without one, `Orchestrator._snapshot` returns `""`, so `baseline_tree` is
    never recorded and `_quarantine` cannot revert what a failed ticket wrote.
    Those files stay on disk, the next cycle's baseline reads them as
    pre-existing, and whatever they break is excused for every later ticket
    rather than fixed — the loop says exactly that in a warning nobody sees
    until after the first failure.

    Nine fixture runs went that way before anyone noticed, which is what makes
    this the copy's job rather than the reader's. Review's diff falls back to
    the whole tree for the same reason.

    Returns whether it worked. A machine without git still gets a usable copy;
    it gets one with quarantine off, which `forge doctor` now says out loud.
    """
    steps = (
        ("init", "-q"),
        ("add", "-A"),
        (
            "-c", "user.email=forge@example.invalid",
            "-c", "user.name=forge",
            "commit", "-qm", "the fixture, as committed",
        ),
    )
    for arguments in steps:
        try:
            result = subprocess.run(  # noqa: S603 - fixed arguments, no shell
                ["git", *arguments],
                cwd=target,
                capture_output=True,
                text=True,
                check=False,
            )
        except (FileNotFoundError, OSError):
            return False
        if result.returncode != 0:
            return False
    return True


def copy_sample(destination: Path | str | None = None, repo: bool = True) -> Path:
    """Copy the fixture to `destination` and return where it landed.

    An existing destination is an error rather than a merge: the copy is meant
    to be a clean starting tree, and a merge into a used one is the state this
    script exists to avoid.

    `repo` initialises git in the copy, which is what a forge run needs to
    quarantine a failed ticket's files. Pass `False` only to inspect the copy
    itself.
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
    if repo:
        init_repo(target)
    return target


def main(argv: list[str]) -> int:
    try:
        target = copy_sample(argv[0] if argv else None)
    except (FileNotFoundError, FileExistsError) as exc:
        print(f"error: {exc}")
        return 1
    if not (target / ".git").exists():
        print(
            "warning: git is unavailable, so the copy is not a repository. A "
            "failed ticket's files will stay in the tree and be excused for "
            "every ticket after it."
        )
    print(target)
    print(f"\n  cd {target}")
    print("  forge --root . doctor")
    print("  forge ingest SPEC.md")
    print("  forge go")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
