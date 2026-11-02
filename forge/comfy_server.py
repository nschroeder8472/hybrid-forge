"""Forge running ComfyUI for the image models a project declares.

The same rules as `llama-server` (docs/CREATIVE-TOOLS.md, *Forge runs the
servers*), through the same supervisor (`forge.servers`). Opt-in by the machine
profile:

    "comfyui": {
      "root": "C:/Users/me/Documents/ComfyUI",
      "python": "C:/Users/me/Documents/ComfyUI/.venv/Scripts/python.exe",
      "port": 8188,
      "args": []
    }

**The Python is ComfyUI's own.** Phase 0 found ComfyUI unable to start on a
shared interpreter after an unrelated upgrade; it now runs from its own venv,
and `python` names that venv's interpreter.

**An image model with no `url` is served by the ComfyUI forge runs.** One with
a `url` is external: forge talks to it and never starts or stops it.

**Ready means every workflow can run.** ComfyUI answers `/system_stats` before
it has loaded its custom nodes, so readiness asks `/object_info` for every node
class each workflow uses, and checks every model file a workflow names against
the files that node's loader lists. A workflow naming a checkpoint that is not
on disk fails here, at startup, rather than as `node_errors` on the first
render of a run.
"""

from __future__ import annotations

import hashlib
import json
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .servers import ServerSpec

NAME = "comfyui"
DEFAULT_PORT = 8188
# Measured in phase 0: 18 s on a warm start, 35 s on the first after an
# update while ComfyUI-Manager installed dependencies.
STARTUP_SECONDS = 240.0
# Flags forge sets itself. `--disable-auto-launch` keeps a daemon from opening
# a browser tab every time it starts ComfyUI.
OWNED_FLAGS = ("--listen", "--port", "--disable-auto-launch")
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]")


class ComfyServerError(Exception):
    """The `comfyui` block, or what it would serve, is not usable."""


@dataclass
class Settings:
    """The profile's `comfyui` block, checked."""

    root: Path
    python: Path
    port: int = DEFAULT_PORT
    args: list[str] = field(default_factory=list)

    @classmethod
    def from_block(cls, block: dict[str, Any]) -> "Settings":
        unknown = sorted(set(block) - {"root", "python", "port", "args"})
        if unknown:
            raise ComfyServerError(
                f"comfyui has unknown key(s) {', '.join(unknown)}; it takes "
                f"root, python, port and args."
            )
        if not block.get("root") or not block.get("python"):
            raise ComfyServerError(
                "comfyui needs `root` (the ComfyUI checkout, holding main.py) and "
                "`python` (the interpreter ComfyUI's dependencies are installed in)."
            )
        try:
            port = int(block.get("port", DEFAULT_PORT))
        except (TypeError, ValueError) as exc:
            raise ComfyServerError(f"comfyui.port: {exc}") from exc
        args = block.get("args", [])
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise ComfyServerError('comfyui.args must be a list of strings, e.g. ["--lowvram"].')
        for arg in args:
            if arg.split("=", 1)[0] in OWNED_FLAGS:
                raise ComfyServerError(
                    f"comfyui.args sets {arg.split('=', 1)[0]}, which forge sets "
                    f"itself ({', '.join(OWNED_FLAGS)}). Use port for the port."
                )
        return cls(Path(block["root"]), Path(block["python"]), port, list(args))


def settings_for(config: Any) -> Settings | None:
    block = getattr(config, "comfyui", None) or {}
    return Settings.from_block(block) if block else None


def url_for(config: Any) -> str:
    """Where the ComfyUI forge runs answers, from the profile's port."""
    block = getattr(config, "comfyui", None) or {}
    port = int(block.get("port", DEFAULT_PORT)) if block else DEFAULT_PORT
    return f"http://127.0.0.1:{port}"


def is_managed(block: dict[str, Any], port: int) -> bool:
    """Whether an image model is served by the ComfyUI forge runs."""
    if str(block.get("kind", "")).lower() != "comfyui":
        return False
    url = str(block.get("url") or "").strip()
    if not url:
        return True
    parsed = urllib.parse.urlparse(url)
    return (parsed.hostname or "").lower() in _LOCAL_HOSTS and (parsed.port or 80) == port


def managed_models(config: Any, settings: Settings) -> list[str]:
    return sorted(
        name for name, block in config.image_models.items()
        if is_managed(block, settings.port)
    )


def argv(settings: Settings) -> list[str]:
    return [
        str(settings.python), "main.py",
        "--listen", "127.0.0.1",
        "--port", str(settings.port),
        "--disable-auto-launch",
        *settings.args,
    ]


def _get(port: int, path: str) -> Any:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def missing(config: Any, models: list[str], info: Any) -> list[str]:
    """What the workflows need that this ComfyUI lacks. `info(cls)` reads one class.

    Node classes first, then every model file a workflow names, checked
    against the options its loader node lists.
    """
    problems: list[str] = []
    classes: dict[str, Any] = {}
    for name in models:
        provider = config.image_provider(name)
        for kind, graph in provider.graphs.items():
            for node in graph.values():
                cls = node["class_type"]
                if cls not in classes:
                    classes[cls] = info(cls)
                if not classes[cls]:
                    problems.append(f"{name}/{kind}: node class {cls} is not installed")
            for cls, field_name, value in provider.model_files().get(kind, []):
                spec = classes.get(cls) or {}
                inputs = {**spec.get("input", {}).get("required", {}),
                          **spec.get("input", {}).get("optional", {})}
                options = (inputs.get(field_name) or [None])[0]
                if isinstance(options, list) and value not in options:
                    problems.append(f"{name}/{kind}: {cls}.{field_name} {value} is not on disk")
    return sorted(set(problems))


def spec(config: Any, settings: Settings) -> ServerSpec | None:
    """The `ServerSpec` for this project's ComfyUI, or None when nothing is managed."""
    models = managed_models(config, settings)
    if not models:
        return None
    if not (settings.root / "main.py").is_file():
        raise ComfyServerError(f"comfyui.root {settings.root} has no main.py.")
    if not settings.python.is_file():
        raise ComfyServerError(f"comfyui.python {settings.python} does not exist.")
    port = settings.port

    def identify() -> bool:
        try:
            stats = _get(port, "/system_stats")
        except Exception:  # noqa: BLE001 - anything but stats is "not ComfyUI"
            return False
        return isinstance(stats, dict) and "comfyui_version" in (stats.get("system") or {})

    def info(cls: str) -> Any:
        try:
            reply = _get(port, f"/object_info/{urllib.parse.quote(cls)}")
        except Exception:  # noqa: BLE001
            return None
        return reply.get(cls) if isinstance(reply, dict) else None

    def ready() -> str | None:
        if not identify():
            return "it is not answering /system_stats as ComfyUI yet"
        problems = missing(config, models, info)
        return "; ".join(problems) if problems else None

    # The command line only: workflows travel with each request, so editing
    # one is no reason to restart the server.
    digest = hashlib.sha256(" ".join(argv(settings)).encode())
    return ServerSpec(
        name=NAME,
        argv=argv(settings),
        port=port,
        identify=identify,
        ready=ready,
        startup_seconds=STARTUP_SECONDS,
        cwd=settings.root,
        fingerprint=digest.hexdigest(),
    )
