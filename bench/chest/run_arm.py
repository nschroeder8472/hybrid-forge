"""Run one arm of the chest sprite-sheet bench. Reuses the tic-tac-toe harness.

    python run_arm.py B|D [--rep N] [--budget USD]

  B  Opus alone: Python with Pillow, and Blender 5.2. No image model.
  D  The same, plus the smith's tools: mold (Flux 2, Kontext), gauge, anvil.

Results in D:\\Users\\Nik\\Documents\\Projects\\forge-bench\\chest\\<arm>-<rep>.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tictactoe"))

import run_arm as base  # noqa: E402

base.HERE = HERE
base.DEFAULT_OUT = Path(r"D:\Users\Nik\Documents\Projects\forge-bench\chest")
base.ARMS = {
    "B": {"forge": False},
    "D": {"forge": True, "config": "D-smith.config.json", "kit": "smith"},
}


def score(repo: Path, run_dir: Path) -> dict:
    sheet = json.loads(subprocess.run(
        [sys.executable, str(HERE / "acceptance" / "sheet_accept.py"), str(repo)],
        capture_output=True, text=True, encoding="utf-8").stdout or "{}")
    built = repo / "sheets" / "chest.png"
    if built.is_file():
        shutil.copy(built, run_dir / "chest.png")
    return {"sheet": sheet,
            "total": {"passed": sheet.get("passed", 0), "of": sheet.get("total", 14)}}


base.score = score

if __name__ == "__main__":
    sys.exit(base.main())
