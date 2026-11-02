"""Characterise the parsers against every recorded run, before and after a refactor.

`forge replay` reads a project's recorded artifacts with today's parsers. This
runs it over a list of evidence projects and either writes what it read
(`save`) or compares what it reads now against a saved snapshot (`check`).

The restructure in docs/FORGE-AS-A-TOOL.md moves the attempt pipeline out of
`Orchestrator` without changing behaviour. Unit tests assert what we thought
the output looked like; this asserts that the same recordings still read the
same way (LOOP-INVARIANTS §9).

    python scripts/replay_snapshot.py save  --out build/replay-snapshot.json ../new_forge_test ../sheet_forge_test
    python scripts/replay_snapshot.py check --snapshot build/replay-snapshot.json

Exit status 1 when anything reads differently than the snapshot.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Fields of a replay record that describe what was read. `origin` is an
# absolute path, kept only as the record's identity.
_READING = ("lens", "now", "note")


def replay(project: Path) -> list[dict]:
    """Run `forge replay --json` in `project` with this checkout's forge."""
    proc = subprocess.run(
        [sys.executable, "-m", "forge", "replay", "--json"],
        cwd=project,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_env(),
    )
    if proc.returncode not in (0, 1):
        raise SystemExit(f"replay failed in {project}:\n{proc.stderr[-2000:]}")
    return json.loads(proc.stdout or "[]")


def _env() -> dict[str, str]:
    import os

    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _key(record: dict) -> str:
    return f"{record.get('origin', '')}#{record.get('lens', '')}"


def reading(records: list[dict]) -> dict[str, dict]:
    return {_key(r): {f: r.get(f) for f in _READING} for r in records}


def save(out: Path, projects: list[Path]) -> int:
    snapshot = {}
    for project in projects:
        records = replay(project)
        snapshot[str(project.resolve())] = reading(records)
        print(f"{project}: {len(records)} records")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snapshot, indent=1, sort_keys=True), encoding="utf-8")
    print(f"wrote {out}")
    return 0


def check(snapshot_path: Path) -> int:
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    differences = 0
    for project, then in snapshot.items():
        now = reading(replay(Path(project)))
        for key in sorted(set(then) | set(now)):
            if then.get(key) != now.get(key):
                differences += 1
                print(f"{key}\n  then: {then.get(key)}\n  now:  {now.get(key)}")
        print(f"{project}: {len(now)} records")
    print(f"{differences} difference(s)")
    return 1 if differences else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("save")
    s.add_argument("--out", type=Path, required=True)
    s.add_argument("projects", type=Path, nargs="+")
    c = sub.add_parser("check")
    c.add_argument("--snapshot", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.cmd == "save":
        return save(args.out, args.projects)
    return check(args.snapshot)


if __name__ == "__main__":
    sys.exit(main())
