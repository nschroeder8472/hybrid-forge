"""Run one of the project's own commands under a bound that actually holds.

`subprocess.run(..., shell=True, capture_output=True, timeout=N)` looks like it
bounds a command and does not. Two things defeat it, and they compound:

The child is the platform shell, so killing it on timeout leaves whatever the
shell started. A test runner that has wedged is a grandchild, and it keeps
running.

That surviving grandchild inherited the write ends of the pipes
`capture_output` created. `subprocess.run` re-enters `communicate()` after
raising `TimeoutExpired`, and blocks there until every writer closes — which
the grandchild will not do, because it is the thing that hung. So the parent
hangs on the child it just tried to kill.

On 2026-09-06 that cost this repository a run: a verify step stayed open for
eight hours and fifteen minutes against a 1,800-second timeout, the step never
ended, the ticket never failed, and every brake downstream of the step was
waiting on a result that could not arrive.

Both halves are fixed here. Output goes to a file, which has no reader to
block, so a surviving grandchild cannot hold the parent. And the timeout kills
the process group rather than the shell that happens to lead it — `os.killpg`
where there are process groups, `taskkill /T` where there are not.
"""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import IO

WINDOWS = os.name == "nt"

# How long to wait for a killed tree to actually go. Reached only when the kill
# itself did not take, which is a stuck kernel-side handle rather than a slow
# process; the output is already readable by then and the caller still needs an
# answer, so this is a bound and not a guarantee.
REAP_SECONDS = 10


@dataclass
class Outcome:
    """What a command did: its code, everything it said, and whether it was cut.

    `output` is stdout and stderr interleaved, which is how every reader in the
    loop already consumes them, and it is populated on the timeout path too —
    what a suite printed before it wedged is the most useful evidence there is
    about where it wedged.
    """

    returncode: int
    output: str
    timed_out: bool


def _start(
    command: str,
    cwd: Path,
    sink: IO[bytes],
    env: dict[str, str] | None = None,
) -> subprocess.Popen[bytes]:
    """Start `command` as the leader of its own group, writing into `sink`.

    `env` is merged over the current environment rather than replacing it: a
    command written against this machine needs its PATH, and `None` keeps the
    inherited environment exactly as it was.
    """
    merged = {**os.environ, **env} if env else None
    if WINDOWS:
        # Its own group, so `taskkill /T` has a tree to walk.
        creation = subprocess.CREATE_NEW_PROCESS_GROUP
        return subprocess.Popen(  # noqa: S602 - the user's own configured command
            command, shell=True, cwd=str(cwd), stdout=sink, stderr=sink,
            creationflags=creation, env=merged,
        )
    return subprocess.Popen(  # noqa: S602 - the user's own configured command
        command, shell=True, cwd=str(cwd), stdout=sink, stderr=sink,
        start_new_session=True, env=merged,
    )


def _kill_tree(proc: subprocess.Popen) -> None:
    """End the process and everything it started, on either platform.

    Falls back to killing the immediate child wherever the group-wide kill
    could not be delivered. That is worse — it is the behaviour this module
    exists to replace — but it is better than returning while the shell itself
    still runs, and the caller is told the command timed out either way.
    """
    if WINDOWS:
        try:
            done = subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=30,
                check=False,
            )
            if done.returncode == 0:
                return
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            # Ignored for the type checker rather than for the reader: these
            # three exist on every platform this branch runs on, and do not
            # exist in the stubs when it is checked from Windows. A
            # `sys.platform` test would narrow them, and `WINDOWS` is one
            # constant read by both branches — worth more than the annotation.
            os.killpg(  # type: ignore[attr-defined]
                os.getpgid(proc.pid),  # type: ignore[attr-defined]
                signal.SIGKILL,  # type: ignore[attr-defined]
            )
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass

    try:
        proc.kill()
    except OSError:
        pass


def run_command(
    command: str,
    cwd: Path,
    timeout: float,
    env: dict[str, str] | None = None,
) -> Outcome:
    """Run `command` from `cwd`, and return within `timeout` seconds.

    `env` adds to the environment rather than replacing it: a project's own
    command needs the PATH and the toolchain variables it was written
    against, and a capture step adding one name must not take those away.

    Returns what the command said either way. A command that finished reports
    its own exit code with `timed_out` false; one that was killed reports
    `timed_out` true and whatever it had written by then.
    """
    handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 - closed in the finally
        mode="wb", prefix="forge-cmd-", suffix=".log", delete=False
    )
    path = Path(handle.name)
    timed_out = False
    try:
        proc = _start(command, cwd, handle, env)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(proc)
            try:
                proc.wait(timeout=REAP_SECONDS)
            except subprocess.TimeoutExpired:
                # The output is readable and the caller needs an answer. Say it
                # timed out, which is true, rather than block on the reaping.
                pass
        handle.close()
        output = path.read_text(encoding="utf-8", errors="replace").strip()
    finally:
        if not handle.closed:
            handle.close()
        try:
            path.unlink()
        except OSError:
            # Already gone, or held open on Windows by something that outlived
            # the kill. A leftover file in the temp directory is not worth
            # failing a verify step over.
            pass

    code = proc.returncode
    return Outcome(
        returncode=code if code is not None else -1,
        output=output,
        timed_out=timed_out,
    )
