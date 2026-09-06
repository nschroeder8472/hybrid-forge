# A verify command that hangs stops the run, not the daemon

`Orchestrator._shell` runs every one of the project's own commands — lint,
typecheck, build, test, the baseline sweeps — through `subprocess.run(...,
shell=True, capture_output=True, timeout=1800)`. That timeout does not bound
what it looks like it bounds.

On 2026-09-06 a run sat in one `test` step for eight hours and fifteen minutes.
The suite it was running had a test that built an HTTP server and never served
it, so the request blocked forever. The 1800-second timeout expired at the
thirty-minute mark and changed nothing: `shell=True` means the child is the
platform shell, killing it leaves the grandchild — the `python -m unittest`
process — alive, and that grandchild still held the write end of the pipes
`capture_output` created. `subprocess.run` re-enters `communicate()` after
raising `TimeoutExpired`, so the daemon blocked draining a pipe that would
never close. The step never ended, the ticket never failed, nothing was
recorded, and every brake downstream of the step — attempts, convergence, the
escalation ladder — was waiting on a step result that could not arrive.

This is a general shape, not a Windows curiosity. A process group leader that
outlives the shell has the same effect on macOS and Linux; the pipe is what
turns "the command is slow" into "the daemon is gone". Both halves are fixed
here: output goes to a file rather than a pipe, so nothing can block on a
reader, and the timeout kills the whole process tree rather than the shell that
happens to be its root.

## Design decisions

These are settled. A retry cycle may not revise them away.

Decision: the process handling moves into a new module, `forge/processes.py`,
so it can be tested against real child processes without an `Orchestrator`, a
`Store` or a run. `Orchestrator._shell` keeps its signature, its step
bookkeeping, its `strip_ansi` and its `reroot`, and calls the new module for
the part that actually runs the command.

Decision: standard library only, and one code path per platform family chosen
at call time. On POSIX the child is started with `start_new_session=True` and
the tree is killed with `os.killpg`. On Windows the child is started with
`subprocess.CREATE_NEW_PROCESS_GROUP` and the tree is killed with `taskkill /T
/F /PID`, which ships with every supported Windows. No third-party dependency —
`psutil` would do this in one line and is not worth a runtime dependency in a
tool whose install story is `pip install hybrid-forge`.

Decision: stdout and stderr are redirected to one temporary file, opened by
this module, rather than to pipes. This is what makes the timeout real: a file
has no reader to block, so a surviving grandchild holding the handle cannot
stop the parent from moving on. The two streams are interleaved into one file
because every reader downstream of `_shell` already consumes them as one
string.

Decision: a timed-out command returns the output it produced before it was
killed, and `_shell` puts that text in the step detail. Today a timeout records
`test timed out after 1800s` and nothing else, so the executor is told a
command failed and shown none of what it said. What a suite printed in the
thirty minutes before it wedged is the most useful evidence there is about
where it wedged.

Decision: the limit becomes configurable as `loop.commandTimeoutSeconds`,
default 1800 — the value in the code today, so no existing repository changes
behaviour by upgrading.

## Out of scope

- **Detecting that a command is likely to hang.** The fix is a bound that
  holds, not a prediction.
- **Streaming a running command's output to the dashboard.** The file this
  writes makes that possible later; nothing here reads it while the command
  runs.
- **The suite that caused the incident.** `tests/test_ui_write_endpoint.py` was
  repaired by hand on 2026-09-06: it serves its `ThreadingHTTPServer` on a
  daemon thread and bounds every request at ten seconds.

---

# HT-001: Run a command under a bound that survives a wedged grandchild

**Route:** delegate
**Kind:** feature

## Spec

Create `forge/processes.py`. It holds one dataclass and one function, and it
imports nothing outside the standard library.

```python
@dataclass
class Outcome:
    returncode: int
    output: str
    timed_out: bool


def run_command(command: str, cwd: Path, timeout: float) -> Outcome:
```

`run_command` starts `command` through the platform shell with `shell=True`,
from the directory `cwd`, and returns when it has finished or when `timeout`
seconds have passed, whichever is first.

**Where the output goes.** Open one temporary file with
`tempfile.NamedTemporaryFile(delete=False)` in binary write mode and pass the
same handle as both `stdout` and `stderr`. Close the handle once the process
has ended, read the file back with `encoding="utf-8"` and `errors="replace"`,
strip trailing whitespace, and remove the file with `os.unlink` inside a
`finally`, ignoring `OSError` so a file already gone is not an error. Pipes are
what made the current timeout unenforceable, so nothing here creates one.

**How it starts the child.** On POSIX — `os.name != "nt"` — pass
`start_new_session=True`, which puts the shell and everything it spawns in a
new process group. On Windows pass
`creationflags=subprocess.CREATE_NEW_PROCESS_GROUP`.

**How it ends the child.** Use `subprocess.Popen` and `proc.wait(timeout=
timeout)`. On `subprocess.TimeoutExpired`, kill the whole tree:

- POSIX: `os.killpg(os.getpgid(proc.pid), signal.SIGKILL)`, wrapped so that a
  `ProcessLookupError` — the group is already gone — is treated as success.
- Windows: `subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
  capture_output=True, timeout=30)`. When that raises `OSError` or
  `subprocess.TimeoutExpired`, or exits non-zero, call `proc.kill()` so the
  immediate child dies even where `taskkill` could not be reached.

Then `proc.wait(timeout=10)`, and where that expires as well, carry on: the
output is still readable and the caller still needs an answer. Return
`Outcome(returncode=proc.returncode if proc.returncode is not None else -1,
output=<what the file holds>, timed_out=True)`.

The ordinary path returns `Outcome(returncode=proc.returncode, output=<the
file>, timed_out=False)`.

## Allowed files

- `forge/processes.py`
- `tests/test_processes.py`

## Reference files

- `forge/ui/writes.py`

## Acceptance criteria

- `run_command` with a command that runs `sys.executable` with
  `-c "print('hi')"`, a `cwd` of a temporary directory and a timeout of 60,
  returns an `Outcome` whose `returncode` is `0`, whose `output` contains
  `hi`, and whose `timed_out` is `False`.
- `run_command` with a command whose script writes `boom` to `sys.stderr`
  returns an `Outcome` whose `output` contains `boom`, so the two streams
  arrive together.
- `run_command` with a command whose script calls `sys.exit(3)` returns an
  `Outcome` whose `returncode` is `3` and whose `timed_out` is `False`.
- `run_command` with a `cwd` of a temporary directory containing a file named
  `marker.txt`, and a command whose script prints `os.listdir(".")`, returns an
  `Outcome` whose `output` contains `marker.txt`.
- Given a script that prints `working`, spawns a detached grandchild with
  `subprocess.Popen`, and then sleeps 30 seconds — where the grandchild writes
  `started.txt` into `cwd` at once, sleeps 30 seconds, and then writes
  `survived.txt` — `run_command` on that script with a timeout of 3 returns
  within 25 seconds of being called, with `timed_out` equal to `True` and
  `output` containing `working`.
- After that call returns, `started.txt` exists in `cwd`, which is what shows
  the grandchild really ran; and five seconds later `survived.txt` still does
  not exist, which is what shows the tree was killed rather than the shell
  alone.
- `run_command` on a script that sleeps 30 seconds, with a timeout of 3,
  returns an `Outcome` whose `timed_out` is `True` and whose `returncode` is
  not `0`.

## Context

The repository is Python 3.10+ and runs on Windows, macOS and Linux; the suite
runs on all three, so every test here has to pass on all three. The project's
own lint is `python -m flake8 .` with a 100-character line limit.

## Notes

The grandchild criterion is the one the whole ticket exists for, and it is the
one whose phrasing could hide the bug. `survived.txt` asserts an absence, which
an implementation that never starts anything also satisfies — that is why
`started.txt` is asserted beside it, and why the output has to contain
`working`. All three together say the command ran, produced output, spawned
something that outlived its shell, and was stopped anyway.

Write the child and grandchild as `.py` files into the temporary directory and
invoke them as `f'"{sys.executable}" child.py'` rather than building a `-c`
string. Quoting a multi-statement `-c` argument that works in both `cmd.exe`
and `sh` is its own puzzle, and it is not this ticket's puzzle.

Twenty-five seconds of slack on a three-second timeout is deliberate: a cold
interpreter start on a loaded CI box is slow, and a criterion that fails
intermittently teaches the loop nothing.

---

# HT-002: Give `_shell` the new bound and a configured limit

**Route:** withheld:interface
**Kind:** feature
**Needs:** HT-001

## Spec

Withheld for one reason, and it is not the difficulty: the executor emits whole
files, and this ticket's two files are `forge/loop.py` at roughly 6,400 lines
and `forge/config.py` at roughly 1,600. Re-emitting either from a model is a
silent-omission risk out of all proportion to a change that is about a dozen
lines, in the module that runs every command the harness has. A person makes
these edits.

**`forge/config.py`.** `LoopSettings` gains `command_timeout_seconds: int =
1800`. `Config.load` reads it from the `loop` block as
`int(loop.get("commandTimeoutSeconds", 1800))`, beside the other `loop` keys.
`Config.write` serialises it into the `loop` dictionary as
`"commandTimeoutSeconds": self.loop.command_timeout_seconds`.

**`forge/loop.py`.** In `_shell`, replace the `subprocess.run(...)` call and its
`except subprocess.TimeoutExpired` block with a call to
`processes.run_command(command, workspace.path(self.config.root),
self.config.loop.command_timeout_seconds)`.

Keep everything around it as it is, character for character: the blank-command
short circuit, `start_step`, the `strip_ansi` and `reroot` of the output, the
`end_step` with `"ok"` or `"failed"`, and the returned `StepResult`.

`ok` becomes `outcome.returncode == 0 and not outcome.timed_out`.

When `outcome.timed_out` is true, the detail recorded on the step and returned
in the `StepResult` opens with a sentence naming the command and the limit it
passed — for example `f"{name} was killed after
{self.config.loop.command_timeout_seconds}s"` — and is followed by the output
the command produced before it was killed, rerooted like any other. A timeout
today records one sentence and discards everything the command said, which
leaves the executor with a failure it cannot read.

## Allowed files

- `forge/loop.py`
- `forge/config.py`
- `tests/test_command_timeout.py`

## Reference files

- `forge/processes.py`

## Acceptance criteria

- `LoopSettings()` constructed with no arguments has `command_timeout_seconds`
  equal to `1800`.
- `Config.load` over a root whose config declares `loop` as
  `{"commandTimeoutSeconds": 45}` returns a config whose
  `loop.command_timeout_seconds` is `45`; a config whose `loop` block says
  nothing about it returns `1800`.
- A config passed through `Config.write()` and read back with `json.loads` has
  `payload["loop"]["commandTimeoutSeconds"]` equal to the value the config
  held.
- `Orchestrator._shell` given a command that prints `working` and then sleeps
  30 seconds, on a config whose `loop.command_timeout_seconds` is `3`, returns
  a `StepResult` whose `ok` is `False` and whose `detail` contains both `3` and
  `working`, within 25 seconds.
- That same call records a step whose status is `failed`, readable back through
  the store, rather than leaving a step at `running`.
- `Orchestrator._shell` given a command that prints `fine` and exits 0 returns
  a `StepResult` whose `ok` is `True` and whose `detail` contains `fine`, and
  records a step whose status is `ok`.

## Notes

`tests/test_forge.py` has `_stub_orchestrator`, which builds an `Orchestrator`
over a temporary repository with the project commands blanked out; the last
three criteria are reachable by calling `_shell` on that fixture directly with
a command of the test's own. Put them in `tests/test_command_timeout.py` and
import the fixture, or add them beside it — either is fine, but a new file
keeps the diff readable.

The step-status criterion is worth its line. The incident this ticket comes
from left a step at `running` for eight hours, and every brake in the loop was
waiting on that row.
