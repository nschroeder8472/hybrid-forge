"""Forge running `llama-server` for the models a project serves locally.

Until this existed, SETUP §1.3 left the server to the person: copy a command
line, pick a bind address, remember `--models-max 1`. Forge already had every
input that command line needs — the binary (`llama.resolve_server`) and the
preset (`presets.write`) — and the two mistakes it invites, a wrong
`--models-max` and a `0.0.0.0` bind, both fail silently. So forge builds it.

**Opt-in, per machine.** The machine profile's `llamaServer` block turns it on:

    "llamaServer": { "port": 8080, "modelsMax": 1, "args": [] }

With no block, nothing changes: forge talks to whatever answers at each model's
`baseUrl`, as it always has.

**The address decides which models are forge's.** A `llamacpp` model whose
`baseUrl` is absent, or names loopback at the managed port, is served by the
server forge runs. Any other address — a GPU host on the network, a container —
is external, and forge only talks to it. An absent `baseUrl` already meant
`127.0.0.1:8080`, so existing configs mean the same thing they did.

**Ready means the router lists every model the project needs.** Not loaded —
the router loads on first call, and the loop already waits for that — but
declared, because a model missing from the preset is a refusal at the first
call rather than at startup.
"""

from __future__ import annotations

import hashlib
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import llama, presets
from .providers._http import get_json
from .servers import ServerSpec

NAME = "llama-server"
DEFAULT_PORT = 8080
# One checkpoint resident at a time, not llama.cpp's own default of four. Four
# is right on a card with room for four and fatal on one without: the router
# keeps the last role's checkpoint loaded and the next one's child exits trying
# to allocate. Nothing reports it. A machine with room raises this on purpose.
DEFAULT_MODELS_MAX = 1
# Router start was measured at 0.5 s; the bound is for a cold disk and a slow
# antivirus scan of a fresh binary, not for loading a checkpoint, which the
# router does lazily on first call.
STARTUP_SECONDS = 60.0

# Flags forge sets and a profile's `args` may not repeat. Two `--models-max`
# values on one command line is a silent last-one-wins, which is exactly the
# class of mistake this module exists to remove.
OWNED_FLAGS = ("--host", "--port", "--models-preset", "--models-max", "--models-dir")

_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]")
_KINDS = ("llamacpp", "llama.cpp", "llama-cpp", "llama-server", "llama", "local")


class LlamaServerError(Exception):
    """The `llamaServer` block, or what it would serve, is not usable."""


@dataclass
class Settings:
    """The profile's `llamaServer` block, checked."""

    port: int = DEFAULT_PORT
    models_max: int = DEFAULT_MODELS_MAX
    args: list[str] = field(default_factory=list)

    @classmethod
    def from_block(cls, block: dict[str, Any]) -> "Settings":
        """Parse and check a `llamaServer` block. Raises `LlamaServerError`."""
        unknown = sorted(set(block) - {"port", "modelsMax", "args"})
        if unknown:
            raise LlamaServerError(
                f"llamaServer has unknown key(s) {', '.join(unknown)}; it takes "
                f"port, modelsMax and args."
            )
        try:
            port = int(block.get("port", DEFAULT_PORT))
            models_max = int(block.get("modelsMax", DEFAULT_MODELS_MAX))
        except (TypeError, ValueError) as exc:
            raise LlamaServerError(f"llamaServer: {exc}") from exc
        if not 0 < port < 65536:
            raise LlamaServerError(f"llamaServer.port is {port}; expected 1-65535.")
        if models_max < 1:
            raise LlamaServerError(
                f"llamaServer.modelsMax is {models_max}; expected 1 or more."
            )
        args = block.get("args", [])
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise LlamaServerError(
                'llamaServer.args must be a list of strings, e.g. ["--threads", "8"].'
            )
        for arg in args:
            flag = arg.split("=", 1)[0]
            if flag in OWNED_FLAGS:
                raise LlamaServerError(
                    f"llamaServer.args sets {flag}, which forge sets itself "
                    f"({', '.join(OWNED_FLAGS)}). Use port or modelsMax for "
                    f"those; the preset is written from config."
                )
        return cls(port=port, models_max=models_max, args=list(args))


def settings_for(config: Any) -> Settings | None:
    """This machine's `llamaServer` settings, or None when it has opted out."""
    block = getattr(config, "llama_server", None) or {}
    if not block:
        return None
    return Settings.from_block(block)


def is_managed(block: dict[str, Any], port: int) -> bool:
    """Whether a model block is served by the `llama-server` forge runs."""
    kind = str(block.get("kind", "llamacpp")).lower()
    if kind not in _KINDS:
        return False
    base = str(block.get("baseUrl") or "").strip()
    if not base:
        return port == DEFAULT_PORT
    parsed = urllib.parse.urlparse(base)
    host = (parsed.hostname or "").lower()
    return host in _LOCAL_HOSTS and (parsed.port or 80) == port


def managed_models(config: Any, settings: Settings) -> dict[str, str]:
    """Alias -> router model id, for every model forge's server must serve."""
    out: dict[str, str] = {}
    for alias in sorted(config.models):
        block = config.models[alias]
        if is_managed(block, settings.port):
            out[alias] = str(block.get("model") or alias)
    return out


def _unpathed(config: Any, models: dict[str, str]) -> list[str]:
    return [
        alias
        for alias in models
        if not str(config.models[alias].get("modelPath") or "").strip()
    ]


def argv(binary: Path, preset: Path, settings: Settings) -> list[str]:
    """The command line forge starts `llama-server` with."""
    return [
        str(binary),
        "--host", "127.0.0.1",
        "--port", str(settings.port),
        "--models-preset", str(preset),
        "--models-max", str(settings.models_max),
        *settings.args,
    ]


def _catalog(port: int) -> dict[str, Any]:
    return get_json(f"http://127.0.0.1:{port}/v1/models", headers={}, timeout=5)


def spec(config: Any, settings: Settings) -> ServerSpec | None:
    """The `ServerSpec` for this project, or None when nothing is managed.

    Writes the preset as a side effect, every time. A server started from a
    preset older than the config would bring back the drift `forge models`
    exists to prevent, and this is the last moment before a server reads it.
    Raises `LlamaServerError` for a managed model with no `modelPath` — the
    preset cannot serve what it cannot name — and for a missing binary.
    """
    models = managed_models(config, settings)
    if not models:
        return None
    unpathed = _unpathed(config, models)
    if unpathed:
        raise LlamaServerError(
            f"forge runs llama-server on this machine, but "
            f"{', '.join(unpathed)} {'has' if len(unpathed) == 1 else 'have'} "
            f"no modelPath, so the preset cannot serve "
            f"{'it' if len(unpathed) == 1 else 'them'}. Add the .gguf's path "
            f'to the model block, e.g. "modelPath": "C:/AIModels/model.gguf". '
            f"For a Hugging Face cache, name the file under snapshots/, not "
            f"the blob."
        )
    binary, _source = llama.resolve_server(llama.PINNED_BUILD)
    if binary is None:
        raise LlamaServerError(
            f"no llama-server to start. `forge llama install` fetches "
            f"{llama.PINNED_BUILD}."
        )
    preset = presets.write(config)
    if preset is None:  # unreachable while every managed model has a path
        raise LlamaServerError("no llama.cpp models to write a preset for.")

    port = settings.port
    wanted = sorted(set(models.values()))

    def identify() -> bool:
        try:
            doc = _catalog(port)
        except Exception:  # noqa: BLE001 - anything but a catalogue is "not us"
            return False
        return isinstance(doc.get("data"), list)

    def ready() -> str | None:
        try:
            doc = _catalog(port)
        except Exception as exc:  # noqa: BLE001
            return f"its catalogue did not answer: {exc}"
        have = {str(entry.get("id")) for entry in doc.get("data", []) if entry.get("id")}
        missing = [model for model in wanted if model not in have]
        if missing:
            return (
                f"it does not serve {', '.join(missing)} "
                f"(it serves {', '.join(sorted(have)) or 'nothing'})"
            )
        return None

    return ServerSpec(
        name=NAME,
        argv=argv(binary, preset, settings),
        port=port,
        identify=identify,
        ready=ready,
        startup_seconds=STARTUP_SECONDS,
        cwd=config.root,
        fingerprint=hashlib.sha256(
            preset.read_bytes() + "\0".join(argv(binary, preset, settings)).encode()
        ).hexdigest(),
    )
