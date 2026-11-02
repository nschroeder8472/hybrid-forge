"""The servers one `forge go` brings up, keeps alive, and takes down.

`servers.Supervisor` knows how to run a server; this knows which servers a run
needs. Today that is `llama-server`, when the machine profile opts in and a
role is served by it. ComfyUI joins it here rather than anywhere in the loop
(docs/CREATIVE-TOOLS.md).

The lifecycle is the run's, not the daemon's: started before the first run in
the queue, checked between tickets, stopped when the queue ends however it
ends. A daemon killed outright skips the stop, which is what `servers.json` is
for — the next `forge go` finds the server it left, owns it, and uses it
rather than paying a reload.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import comfy_server, llama_server
from .servers import OWNER_RUN, ServerError, ServerSpec, Started, Supervisor


@dataclass
class RunServers:
    supervisor: Supervisor
    specs: list[ServerSpec] = field(default_factory=list)

    @classmethod
    def for_config(cls, config: Any, *, images: bool = False) -> "RunServers | None":
        """What this run must start, or None when forge runs nothing here.

        `images` is whether the backlog has work for an image model. ComfyUI is
        started only then: a backlog of code tickets pays nothing for it.

        Raises `ServerError` for a configuration that cannot be started — a
        managed model with no path, no binary — so the run refuses before any
        model is called rather than failing at its first one.
        """
        specs: list[ServerSpec] = []
        try:
            settings = llama_server.settings_for(config)
            spec = llama_server.spec(config, settings) if settings else None
            if spec is not None:
                specs.append(spec)
            comfy = comfy_server.settings_for(config) if images else None
            comfy_spec = comfy_server.spec(config, comfy) if comfy else None
            if comfy_spec is not None:
                specs.append(comfy_spec)
        except (llama_server.LlamaServerError, comfy_server.ComfyServerError) as exc:
            raise ServerError(str(exc)) from exc
        if not specs:
            return None
        return cls(Supervisor(config.config_dir, owner=OWNER_RUN), specs)

    def start(self) -> list[str]:
        """Bring every server up. Returns one line per server to print."""
        lines = []
        for spec in self.specs:
            started = self.supervisor.ensure(spec)
            lines.append(_describe(spec, started))
        return lines

    def keep(self) -> list[str]:
        """Restart any server forge started that has died since the last check.

        Raises `ServerError` on a second death; returns what it restarted.
        """
        notes = []
        for spec in self.specs:
            note = self.supervisor.check(spec)
            if note:
                notes.append(note)
        return notes

    def restart(self, name: str) -> str | None:
        """Kill a server forge started and start it again. None if it is not ours.

        The GPU lease's fallback: a ComfyUI that will not give the card back
        after `/free` is restarted, which always does.
        """
        for spec in self.specs:
            if spec.name == name and self.supervisor.record(name) is not None:
                self.supervisor.stop(name)
                started = self.supervisor.ensure(spec)
                return f"{name}: restarted to release the GPU (pid {started.pid})."
        return None

    def stop(self) -> list[str]:
        """Stop what this run is responsible for. Never an adopted server."""
        return self.supervisor.stop_owned()


def _describe(spec: ServerSpec, started: Started) -> str:
    where = f"127.0.0.1:{spec.port}"
    if started.how == "adopted":
        return (
            f"{spec.name}: using the one already on {where}; forge did not "
            f"start it and will not stop it."
        )
    if started.how == "owned":
        return f"{spec.name}: already running on {where} (pid {started.pid}), started by forge."
    if started.how == "replaced":
        return f"{spec.name}: the preset changed, restarted on {where} (pid {started.pid})."
    return f"{spec.name}: started on {where} (pid {started.pid}); log {started.log}"
