"""Run a Python-Fu script in GIMP from ordinary Python.

    from tools.gimp import runner
    runner.run("tools/gimp/grayscale.py", input="in.png", output="out.png")

The script runs inside `gimp-console` with no window, through the
`python-fu-eval` batch interpreter. Keyword arguments reach it as JSON in the
`GIMP_TOOL_ARGS` environment variable, which the script reads with
`ops.args()`. The script's directory is put on `sys.path` and `__file__` is
set, so a script can `import ops`.

Three things this does because GIMP does not:

- **`--quit`.** Without it a batch session that finished its work waits for
  more, and a test that called it never returns.
- **A non-zero exit is an error here.** GIMP exits 64 when the script raises;
  `run` raises `GimpError` with GIMP's output, so a test fails on the line
  that broke rather than on a missing output file.
- **A bound.** A wedged GIMP is killed after `timeout` seconds.

Which GIMP: `GIMP_CONSOLE` if set, else `gimp-console-3` on PATH, else the
default Windows install location.
"""

import json
import os
import shutil
import subprocess

TIMEOUT = 300


class GimpError(Exception):
    """GIMP could not be found, or the script failed inside it."""


def console():
    """The `gimp-console` binary to run."""
    explicit = os.environ.get("GIMP_CONSOLE")
    if explicit:
        return explicit
    found = shutil.which("gimp-console-3")
    if found:
        return found
    default = os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "Programs", "GIMP 3", "bin", "gimp-console-3.exe"
    )
    if os.path.isfile(default):
        return default
    raise GimpError("gimp-console-3 not found: set GIMP_CONSOLE or put it on PATH")


def run(script, timeout=TIMEOUT, **args):
    """Run `script` in GIMP with `args`. Returns GIMP's output; raises on failure."""
    script = os.path.abspath(script).replace("\\", "/")
    folder = os.path.dirname(script)
    code = (
        f"import sys; sys.path.insert(0, {folder!r}); "
        f"__file__ = {script!r}; exec(open(__file__).read())"
    )
    env = dict(os.environ, GIMP_TOOL_ARGS=json.dumps(args))
    try:
        done = subprocess.run(
            [console(), "-i", "-n", "--quit", "--batch-interpreter=python-fu-eval", "-b", code],
            env=env, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        raise GimpError(f"{script} did not finish within {timeout}s") from exc
    output = (done.stdout or "") + (done.stderr or "")
    if done.returncode != 0:
        raise GimpError(f"{script} failed in GIMP (exit {done.returncode}):\n{output}")
    return output
