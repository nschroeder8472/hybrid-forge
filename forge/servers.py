"""Starting, keeping and stopping the long-lived servers a run depends on.

Every other process forge starts is a command: it runs, it ends, and
`processes.run_command` bounds it. A server is the other shape. It is started
once, outlives the call that started it, holds the GPU while it lives, and is
only noticed when it is gone — usually as an unreachable model on the next
ticket. This module is the one place that knows which of those processes forge
is responsible for.

The rules, each one a failure this design exists to avoid
(docs/CREATIVE-TOOLS.md, *Forge runs the servers*):

**Adopt, never steal.** A port that already answers as the right kind of server
serving the right models is used and never stopped. One answering with the
wrong models is refused by name, and one held by something else entirely is
refused by port. Forge only stops what it started.

**Ownership is written down.** The daemon survives being killed, and on Windows
its children outlive it. Without a record, a resumed daemon would see a
forge-started server only as "something answering on 8080", adopt it, never
stop it, and leave 25 GiB of checkpoint on the card until somebody noticed. So
every start writes the pid to `servers.json` beside the run database, and a
process matching the record is still forge's after a restart.

**Ready means serving what was asked for.** A listening socket is not a server
that can answer the run's first call; the spec's `ready` probe says what is.

**One restart, then stop.** A server that dies mid-run is started again once.
The second death is reported with the tail of its log, because every later call
would fail the same way and the log is the only place that says why.

Everything here is stdlib, like the rest of the daemon. Nothing reads what a
server serves beyond the probes its spec hands in.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .processes import WINDOWS, kill_tree

RECORD_FILE = "servers.json"
LOGS_DIR = "logs"

# Who started a server, which decides who stops it. A run stops what a run
# started, including a run whose daemon died before it could; `forge llama
# start` is a person asking for a server that outlives any one run, so only
# `forge llama stop` ends it.
OWNER_RUN = "run"
OWNER_CLI = "cli"

# Between readiness probes while a server comes up.
_POLL_SECONDS = 0.5
# How much of a dead server's log is worth quoting. The end is where the reason
# is; the start is a banner.
_TAIL_LINES = 30
# How long a killed server gets to actually go before stop() gives up waiting.
_REAP_SECONDS = 15.0


# Every server this interpreter started, by pid. Held so the `Popen` is not
# garbage-collected while its process deliberately keeps running -- which
# `subprocess` reports as a ResourceWarning -- and so a stop can reap the
# process rather than leave a zombie on POSIX.
_CHILDREN: dict[int, subprocess.Popen[bytes]] = {}


def _reap(pid: int) -> None:
    """Collect a child that has already exited, if this interpreter started it."""
    child = _CHILDREN.pop(pid, None)
    if child is not None:
        child.poll()


class ServerError(Exception):
    """A server could not be started, kept, or reached as forge needs it."""


@dataclass
class ServerSpec:
    """Everything the supervisor needs to know about one kind of server.

    `identify` answers whether whatever holds the port is this kind of server
    at all. `ready` answers whether it is serving what this project needs, and
    returns why not — a reason, rather than a bool, because the refusal for an
    adopted server that lacks a model has to name the model.
    """

    name: str
    argv: list[str]
    port: int
    identify: Callable[[], bool]
    ready: Callable[[], str | None]
    startup_seconds: float = 120.0
    cwd: Path | None = None
    host: str = "127.0.0.1"
    # What the server was started from, as a digest: its argv and the files
    # it reads at startup. A server forge owns whose fingerprint no longer
    # matches is serving a configuration that has since changed, and is
    # restarted rather than trusted. Empty never mismatches.
    fingerprint: str = ""


@dataclass
class Started:
    """What `ensure` did: started it, found ours, or adopted someone else's."""

    how: str  # "started" | "replaced" | "owned" | "adopted"
    pid: int | None = None
    log: Path | None = None


def port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """Whether anything accepts a connection on `host:port`."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def pid_alive(pid: int) -> bool:
    """Whether process `pid` still exists. Never signals it."""
    if pid <= 0:
        return False
    if WINDOWS:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        process_query_limited_information = 0x1000
        still_active = 259
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def tail(path: Path, lines: int = _TAIL_LINES) -> str:
    """The last `lines` lines of a log, or a note saying there was none."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return f"(no log at {path})"
    kept = text.rstrip().splitlines()[-lines:]
    return "\n".join(kept) if kept else f"({path} is empty)"


def _spawn(argv: list[str], cwd: Path | None, log: Path) -> subprocess.Popen[bytes]:
    """Start a server detached from this console, writing into `log`.

    Its own process group, so a Ctrl-C meant for `forge go` does not reach it
    — the run decides when its servers stop, and a server killed by the same
    keystroke that asked the loop to finish its step would take the step with
    it. No window, because a daemon opening consoles is noise.
    """
    sink = log.open("ab")
    try:
        if WINDOWS:
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            return subprocess.Popen(
                argv, cwd=str(cwd) if cwd else None, stdout=sink, stderr=sink,
                stdin=subprocess.DEVNULL, creationflags=flags,
            )
        return subprocess.Popen(
            argv, cwd=str(cwd) if cwd else None, stdout=sink, stderr=sink,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
    finally:
        # The child holds its own handle; ours only needs to outlive the call.
        sink.close()


@dataclass
class Supervisor:
    """The servers one project's runs depend on, and which of them are forge's.

    `owner` is who this supervisor starts servers on behalf of. `restarts`
    counts per server for this supervisor's lifetime, which is one run.
    """

    config_dir: Path
    owner: str = OWNER_RUN
    restarts: dict[str, int] = field(default_factory=dict)

    # -- the record ------------------------------------------------------

    @property
    def record_path(self) -> Path:
        return self.config_dir / RECORD_FILE

    def log_path(self, name: str) -> Path:
        return self.config_dir / LOGS_DIR / f"{name}.log"

    def records(self) -> dict[str, dict[str, Any]]:
        """Every server this project's record says forge started.

        An unreadable record is treated as empty. Losing it costs one thing —
        a running server is adopted rather than owned, so the run leaves it up
        — which is the safe direction: the alternative is killing a process on
        the strength of a file that could not be read.
        """
        try:
            data = json.loads(self.record_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, records: dict[str, dict[str, Any]]) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        if not records:
            try:
                self.record_path.unlink()
            except OSError:
                pass
            return
        self.record_path.write_text(
            json.dumps(records, indent=2) + "\n", encoding="utf-8"
        )

    def record(self, name: str) -> dict[str, Any] | None:
        """This server's record, if its process is still the one recorded.

        A record whose pid is gone is dropped here: the server it describes has
        exited, and a pid the OS hands to something else later must never be
        mistaken for it.
        """
        records = self.records()
        entry = records.get(name)
        if entry is None:
            return None
        if pid_alive(int(entry.get("pid", 0))):
            return entry
        _reap(int(entry.get("pid", 0)))
        del records[name]
        self._write(records)
        return None

    # -- starting --------------------------------------------------------

    def ensure(self, spec: ServerSpec) -> Started:
        """Make sure `spec`'s server is up and serving, starting it if need be.

        Raises `ServerError` with the reason when the port is held by something
        else, when an adopted server is missing what the project needs, or when
        a server forge started does not become ready.
        """
        mine = self.record(spec.name)
        if mine is not None and not port_open(spec.host, spec.port):
            # Ours, alive, and not listening: hung, or still coming up from a
            # start a dead daemon never saw finish. Either way a second copy
            # beside it would fight it for the port and the GPU, so it goes.
            self._kill(spec.name, int(mine["pid"]))
            mine = None
        if mine is not None and spec.fingerprint and mine.get("fingerprint") != spec.fingerprint:
            # Ours, and started from a preset or command line that has since
            # changed. Restarting it is the only way it serves what the config
            # now says; it is forge's, so forge may.
            self._kill(spec.name, int(mine["pid"]))
            started = self._start(spec)
            return Started("replaced", started.pid, started.log)
        if mine is not None:
            reason = spec.ready()
            if reason is None:
                return Started("owned", int(mine["pid"]), self.log_path(spec.name))
            raise ServerError(
                f"{spec.name} on port {spec.port} was started by forge "
                f"(pid {mine['pid']}) but {reason}. `forge llama stop` ends it, "
                f"and the next run starts one from the current preset."
            )

        if port_open(spec.host, spec.port):
            if not spec.identify():
                raise ServerError(
                    f"port {spec.port} is in use by something that does not "
                    f"answer as {spec.name}. Stop it, or give forge a different "
                    f"port in the machine profile."
                )
            reason = spec.ready()
            if reason is not None:
                raise ServerError(
                    f"a {spec.name} forge did not start is already on port "
                    f"{spec.port}, and {reason}. Forge does not restart a "
                    f"server it did not start; stop that one, or point this "
                    f"project at it with the right preset."
                )
            return Started("adopted")

        return self._start(spec)

    def _start(self, spec: ServerSpec) -> Started:
        log = self.log_path(spec.name)
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as handle:
            handle.write(
                f"\n--- forge starting {spec.name} at "
                f"{time.strftime('%Y-%m-%d %H:%M:%S')}: {' '.join(spec.argv)}\n"
            )
        try:
            proc = _spawn(spec.argv, spec.cwd, log)
        except OSError as exc:
            raise ServerError(
                f"could not start {spec.name} ({spec.argv[0]}): {exc}"
            ) from exc
        _CHILDREN[proc.pid] = proc

        records = self.records()
        records[spec.name] = {
            "pid": proc.pid,
            "port": spec.port,
            "owner": self.owner,
            "argv": spec.argv,
            "fingerprint": spec.fingerprint,
            "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "log": str(log),
        }
        self._write(records)

        deadline = time.time() + spec.startup_seconds
        reason: str | None = "it never answered"
        while time.time() < deadline:
            if proc.poll() is not None:
                _CHILDREN.pop(proc.pid, None)
                self._forget(spec.name)
                raise ServerError(
                    f"{spec.name} exited with code {proc.returncode} while "
                    f"starting. The end of {log}:\n{tail(log)}"
                )
            if port_open(spec.host, spec.port):
                reason = spec.ready()
                if reason is None:
                    return Started("started", proc.pid, log)
            time.sleep(_POLL_SECONDS)

        self._kill(spec.name, proc.pid)
        raise ServerError(
            f"{spec.name} was not ready within {spec.startup_seconds:g}s: "
            f"{reason}. It has been stopped. The end of {log}:\n{tail(log)}"
        )

    # -- keeping ---------------------------------------------------------

    def check(self, spec: ServerSpec) -> str | None:
        """Restart a server forge started that has died. Once per run.

        Returns a line worth logging when it restarted one, None when there was
        nothing to do. An adopted server is not forge's to restart and is left
        alone; a call against it that fails is the provider's to report.
        """
        entry = self.records().get(spec.name)
        if entry is None or pid_alive(int(entry.get("pid", 0))):
            return None
        _reap(int(entry.get("pid", 0)))
        self._forget(spec.name)
        log = self.log_path(spec.name)
        count = self.restarts.get(spec.name, 0)
        if count >= 1:
            raise ServerError(
                f"{spec.name} died again after one restart. Every later call "
                f"would fail the same way. The end of {log}:\n{tail(log)}"
            )
        self.restarts[spec.name] = count + 1
        started = self._start(spec)
        return f"{spec.name} had exited; restarted it (pid {started.pid})."

    # -- stopping --------------------------------------------------------

    def _forget(self, name: str) -> None:
        records = self.records()
        if records.pop(name, None) is not None:
            self._write(records)

    def _kill(self, name: str, pid: int) -> None:
        kill_tree(pid)
        child = _CHILDREN.pop(pid, None)
        if child is not None:
            try:
                child.wait(timeout=_REAP_SECONDS)
            except subprocess.TimeoutExpired:
                pass
        deadline = time.time() + _REAP_SECONDS
        while pid_alive(pid) and time.time() < deadline:
            time.sleep(0.2)
        self._forget(name)

    def stop(self, name: str) -> int | None:
        """Stop a server forge started, whoever started it. Returns its pid."""
        entry = self.record(name)
        if entry is None:
            return None
        pid = int(entry["pid"])
        self._kill(name, pid)
        return pid

    def stop_owned(self) -> list[str]:
        """Stop every server this supervisor's owner is responsible for.

        A run stops what runs started — including a server a previous daemon
        started and died holding — and never one `forge llama start` started.
        """
        stopped = []
        for name, entry in self.records().items():
            if entry.get("owner", OWNER_RUN) != self.owner:
                continue
            if self.stop(name) is not None:
                stopped.append(name)
        return stopped
