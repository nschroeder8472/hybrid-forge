"""Run a Python script inside Blender 5.2, headless.

    python tools/blender.py path/to/script.py [-- script arguments]

Blender is installed at the path below and is not on PATH. This launcher runs
`blender -b --factory-startup --python-exit-code 1 --python <script>`, from the
project root, and passes anything after `--` to the script (read it from
`sys.argv` after the `--`).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BLENDER = r"C:\Program Files\Blender Foundation\Blender 5.2\blender.exe"


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    script, rest = argv[0], argv[1:]
    command = [BLENDER, "-b", "--factory-startup", "--python-exit-code", "1",
               "--python", str(Path(script).resolve())]
    if rest:
        command += rest if rest[0] == "--" else ["--", *rest]
    return subprocess.run(command, cwd=Path(__file__).resolve().parents[1]).returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
