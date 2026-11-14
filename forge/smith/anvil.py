"""anvil — what the work is tested against.

The project's verify commands, attributed against a baseline the smith marks,
plus the checks that need no model, over what changed since HEAD: relative
imports that resolve to nothing (LOOP-INVARIANTS section 15) and dependencies a
manifest no longer declares (section 18).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .. import imports, manifests
from ..api import STEP_DETAIL_LIMIT, ApiError, Forge

LABEL = "smith"
FAILURE_LIMIT = 1500


def anvil(forge: Forge, mark: bool = False, step: int = 0) -> dict[str, Any]:
    """Strike the work against the anvil.

    - `mark`: record what is failing now as the baseline, then report.
    - `step`: return one earlier step's full output instead of running anything.
    - neither: run and attribute against the baseline; with none recorded yet,
      this call records it and says so.
    """
    if step:
        return forge.inspect(step=int(step), limit=STEP_DETAIL_LIMIT)

    marked_before = LABEL in forge._baselines()
    if mark or not marked_before:
        result = forge.verify(capture=LABEL)
        baseline = (
            "recorded now" if mark else
            "none was recorded, so this call recorded one: failures below are "
            "the baseline, not attributed. Call anvil(mark=true) before editing"
        )
    else:
        result = forge.verify(against=LABEL)
        baseline = "attributed against the marked baseline"

    steps = []
    for entry in result["steps"]:
        compact: dict[str, Any] = {"step": entry["step"], "ok": entry["ok"]}
        if "tests" in entry:
            compact["tests"] = entry["tests"]
        if not entry["ok"]:
            for key in ("introduced", "already", "environment", "unattributed"):
                if entry.get(key):
                    value = entry[key]
                    compact[key] = value[:10] if isinstance(value, list) else value
            if isinstance(entry.get("already"), list):
                compact["already"] = len(entry["already"])
            compact["failure"] = (entry.get("failure") or "")[:FAILURE_LIMIT]
            compact["step_id"] = entry["step_id"]
        steps.append(compact)

    changed = changed_files(forge.config.root)
    gates: dict[str, Any] = {}
    misses = imports.unresolved(forge.config.root, changed)
    if misses:
        gates["unresolved_imports"] = [f"{path}: {target}" for path, target in misses][:20]
    lost = dropped_dependencies(forge.config.root, changed)
    if lost:
        gates["dropped_dependencies"] = lost

    return {
        "ok": result["ok"] and not gates,
        "clean": result["clean"] and not gates,
        "baseline": baseline,
        "steps": steps,
        **({"gates": gates} if gates else {}),
        "changed_since_head": len(changed),
    }


def changed_files(root: Path) -> list[str]:
    """Files that differ from HEAD, untracked ones included, repo-relative."""
    proc = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        return []
    paths = []
    for line in proc.stdout.splitlines():
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        path = path.strip('"')
        if path.startswith(".hybridforge/"):
            continue
        if (root / path).is_file():
            paths.append(path)
    return paths


def dropped_dependencies(root: Path, changed: list[str]) -> list[str]:
    found = []
    for path in changed:
        if not manifests.is_manifest(path):
            continue
        before = subprocess.run(
            ["git", "show", f"HEAD:{path}"], cwd=root,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if before.returncode != 0:
            continue
        try:
            after = (root / path).read_text(encoding="utf-8")
        except OSError:
            continue
        gone = manifests.dropped(before.stdout, after, Path(path).name)
        if gone:
            found.append(f"{path}: no longer declares {', '.join(gone)}")
    return found


__all__ = ["anvil", "ApiError"]
