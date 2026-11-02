"""ComfyUI as an image model: a graph executor reached over HTTP.

ComfyUI fits the `ImageProvider` abstraction because it is not one model. The
same adapter reaches SDXL, Flux Kontext and Flux.2; which one runs is a
workflow file the project owns (docs/CREATIVE-TOOLS.md, *ComfyUI — the image
model*).

    "imageModels": {
      "pixel": {
        "kind": "comfyui",
        "workflows": { "generate": ".hybridforge/comfy/sdxl-pixel.json" },
        "bind": {
          "prompt": ["6", "text"], "negative": ["7", "text"],
          "seed": ["3", "seed"], "width": ["5", "width"], "height": ["5", "height"]
        }
      }
    }

**A workflow is exported with Export (API).** The UI format is a different
schema — `nodes` and `links` — and is refused by its shape when the config
loads, not by a `node_errors` reply at render time.

**`bind` is the whole interface between forge and a graph**: which node input
receives the prompt, the seed, the size, the image to edit. A bound node or
input missing from the workflow is refused at load.

**Capabilities are read from what is declared, never probed.** An `edit`
workflow means `supports_edit`; a bound `mask` means `supports_mask`; a bound
`seed` means `supports_seed`. The seed sent is one forge chose, so the seed a
rendering reports is always known.

Stdlib only, like every other adapter: `urllib`, `json`, and a hand-built
multipart body for the one upload.
"""

from __future__ import annotations

import copy
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from .base import ProviderBadResponse, ProviderError, ProviderUnreachable
from .image import ImageCapabilities, ImageProvider, ImageUsage, Rendering, png_size

DEFAULT_URL = "http://127.0.0.1:8188"
# A cold Flux.2 render measured 61 s in phase 0; a cold SDXL one 34 s. The
# ceiling covers a first load from a cold disk.
DEFAULT_TIMEOUT = 600
_POLL_SECONDS = 0.5
# The inputs forge knows how to fill. A binding outside this set would be one
# nothing sends, which reads exactly like one that worked.
BINDABLE = ("prompt", "negative", "seed", "width", "height", "image", "mask")
WORKFLOW_KINDS = ("generate", "edit")


class ComfyConfigError(ProviderError):
    """A workflow or binding that cannot work. Never retryable."""


def load_workflow(path: Path) -> dict[str, Any]:
    """Read an API-format workflow, refusing the UI format by its shape."""
    try:
        graph = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ComfyConfigError(f"cannot read workflow {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ComfyConfigError(f"workflow {path} is not JSON: {exc}") from exc
    if isinstance(graph, dict) and ("nodes" in graph and "links" in graph):
        raise ComfyConfigError(
            f"workflow {path} is in ComfyUI's UI format (it has `nodes` and "
            f"`links`). Export it with Workflow > Export (API) instead."
        )
    nodes_ok = isinstance(graph, dict) and graph and all(
        isinstance(node, dict) and "class_type" in node and isinstance(node.get("inputs"), dict)
        for node in graph.values()
    )
    if not nodes_ok:
        raise ComfyConfigError(
            f"workflow {path} is not an API-format graph: expected an object "
            f'mapping node ids to {{"class_type": ..., "inputs": {{...}}}}.'
        )
    return graph


def check_binding(
    graph: dict[str, Any], bind: dict[str, Any], workflow: str, path: Path
) -> list[str]:
    """Which bindings this workflow carries. Raises on one that points nowhere.

    A binding names a node and an input. One whose node is not in this
    workflow belongs to another workflow and is skipped; one whose node is here
    but whose input is not is a mistake, and refused.
    """
    present = []
    for key, target in bind.items():
        node_id, field_name = str(target[0]), str(target[1])
        node = graph.get(node_id)
        if node is None:
            continue
        if field_name not in node["inputs"]:
            raise ComfyConfigError(
                f"bind.{key} points at input {field_name!r} of node {node_id} "
                f"({node['class_type']}) in the {workflow} workflow {path}, which "
                f"has no such input; it has {', '.join(sorted(node['inputs']))}."
            )
        present.append(key)
    return present


class ComfyUIProvider(ImageProvider):
    kind = "comfyui"

    def __init__(self, name: str, config: dict[str, Any]):
        super().__init__(name, config)
        self.url = str(config.get("url") or DEFAULT_URL).rstrip("/")
        self.timeout = int(config.get("timeoutSeconds", DEFAULT_TIMEOUT))
        root = Path(str(config.get("root") or "."))
        workflows = config.get("workflows") or {}
        if not isinstance(workflows, dict) or not workflows:
            raise ComfyConfigError(
                f"image model {name!r} names no workflows; give it at least "
                f'"workflows": {{"generate": "<path to an API-format JSON>"}}.'
            )
        unknown = sorted(set(workflows) - set(WORKFLOW_KINDS))
        if unknown:
            raise ComfyConfigError(
                f"image model {name!r} has workflow(s) {', '.join(unknown)}; "
                f"forge runs {', '.join(WORKFLOW_KINDS)}."
            )
        bind = config.get("bind") or {}
        bad = sorted(key for key in bind if key not in BINDABLE)
        if bad:
            raise ComfyConfigError(
                f"image model {name!r} binds {', '.join(bad)}, which forge never "
                f"sends; it fills {', '.join(BINDABLE)}."
            )
        for key, target in bind.items():
            if not (isinstance(target, list) and len(target) == 2):
                raise ComfyConfigError(
                    f'bind.{key} of image model {name!r} must be ["<node id>", "<input>"].'
                )
        self.bind: dict[str, list[str]] = {k: [str(v[0]), str(v[1])] for k, v in bind.items()}
        self.paths: dict[str, Path] = {}
        self.graphs: dict[str, dict[str, Any]] = {}
        self.bound: dict[str, list[str]] = {}
        for kind, relative in workflows.items():
            path = (root / str(relative)).resolve()
            graph = load_workflow(path)
            self.paths[kind] = path
            self.graphs[kind] = graph
            self.bound[kind] = check_binding(graph, self.bind, kind, path)
        if "prompt" not in self.bound.get("generate", ["prompt"]):
            raise ComfyConfigError(
                f"image model {name!r}: bind.prompt does not reach the generate "
                f"workflow, so every render would ignore its prompt."
            )
        if "edit" in self.graphs and "image" not in self.bound["edit"]:
            raise ComfyConfigError(
                f"image model {name!r}: the edit workflow has no bound `image`, "
                f"so an edit could not be given the image it is editing."
            )

    # -- what it can do ------------------------------------------------------

    def capabilities(self) -> ImageCapabilities:
        edit = self.bound.get("edit", [])
        return ImageCapabilities(
            supports_edit="edit" in self.graphs,
            supports_mask="mask" in edit,
            supports_seed=any("seed" in keys for keys in self.bound.values()),
        )

    def model_files(self) -> dict[str, list[tuple[str, str, str]]]:
        """Per workflow, (node class, input, file) for every model file it loads."""
        found: dict[str, list[tuple[str, str, str]]] = {}
        suffixes = (".safetensors", ".gguf", ".ckpt", ".pt", ".pth", ".bin")
        for kind, graph in self.graphs.items():
            for node in graph.values():
                for field_name, value in node["inputs"].items():
                    if isinstance(value, str) and value.lower().endswith(suffixes):
                        found.setdefault(kind, []).append(
                            (node["class_type"], field_name, value)
                        )
        return found

    # -- HTTP ----------------------------------------------------------------

    def _request(
        self,
        path: str,
        *,
        data: bytes | None = None,
        content_type: str = "application/json",
        timeout: int = 30,
    ) -> bytes:
        request = urllib.request.Request(
            self.url + path,
            data=data,
            headers={"Content-Type": content_type} if data is not None else {},
            method="POST" if data is not None else "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return bytes(response.read())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:2000]
            raise ProviderBadResponse(
                f"ComfyUI {path} answered {exc.code}: {body}"
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise ProviderUnreachable(f"ComfyUI at {self.url} did not answer: {exc}") from exc

    def _json(self, path: str, payload: Any = None, timeout: int = 30) -> Any:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        raw = self._request(path, data=data, timeout=timeout)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderBadResponse(f"ComfyUI {path} returned non-JSON") from exc

    def health(self) -> str:
        stats = self._json("/system_stats", timeout=10)
        system = stats.get("system", {})
        devices = stats.get("devices") or [{}]
        free = devices[0].get("vram_free", 0) / 2**30
        return f"ComfyUI {system.get('comfyui_version', '?')}, {free:.1f} GiB VRAM free"

    def system_stats(self) -> dict[str, Any]:
        stats = self._json("/system_stats", timeout=10)
        return stats if isinstance(stats, dict) else {}

    def free(self) -> None:
        # `/free` and `/interrupt` answer with an empty body, not JSON.
        self._request(
            "/free",
            data=json.dumps({"unload_models": True, "free_memory": True}).encode(),
            timeout=60,
        )

    def interrupt(self) -> None:
        try:
            self._request("/interrupt", data=b"{}", timeout=10)
        except ProviderError:
            pass

    def upload(self, data: bytes, name: str) -> str:
        """Upload an image for a workflow to read. Returns the name ComfyUI gave it."""
        boundary = uuid.uuid4().hex
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; "
            f'filename="{name}"\r\nContent-Type: image/png\r\n\r\n'
        ).encode() + data + f"\r\n--{boundary}\r\nContent-Disposition: form-data; " \
            f'name="overwrite"\r\n\r\ntrue\r\n--{boundary}--\r\n'.encode()
        raw = self._request(
            "/upload/image", data=body,
            content_type=f"multipart/form-data; boundary={boundary}", timeout=60,
        )
        reply = json.loads(raw.decode("utf-8"))
        subfolder = reply.get("subfolder") or ""
        return f"{subfolder}/{reply['name']}" if subfolder else str(reply["name"])

    # -- rendering -----------------------------------------------------------

    def _fill(self, kind: str, values: dict[str, Any]) -> dict[str, Any]:
        graph = copy.deepcopy(self.graphs[kind])
        for key in self.bound[kind]:
            if key in values and values[key] is not None:
                node_id, field_name = self.bind[key]
                graph[node_id]["inputs"][field_name] = values[key]
        return graph

    def _run(self, kind: str, values: dict[str, Any]) -> Rendering:
        graph = self._fill(kind, values)
        started = time.time()
        reply = self._json(
            "/prompt", {"prompt": graph, "client_id": uuid.uuid4().hex}, timeout=60
        )
        if reply.get("node_errors"):
            raise ComfyConfigError(
                f"ComfyUI refused the {kind} workflow {self.paths[kind]}: "
                f"{json.dumps(reply['node_errors'])[:1500]}"
            )
        prompt_id = reply.get("prompt_id")
        if not prompt_id:
            raise ProviderBadResponse(f"ComfyUI /prompt returned no prompt_id: {reply}")

        deadline = started + self.timeout
        entry: dict[str, Any] = {}
        while True:
            history = self._json(f"/history/{prompt_id}", timeout=30)
            entry = history.get(prompt_id) or {}
            status = entry.get("status") or {}
            if status.get("status_str") == "error":
                raise ProviderBadResponse(
                    f"ComfyUI failed rendering the {kind} workflow: "
                    f"{json.dumps(status.get('messages'))[:1500]}"
                )
            if status.get("completed"):
                break
            if time.time() > deadline:
                self.interrupt()
                raise ProviderUnreachable(
                    f"ComfyUI did not finish the {kind} workflow within {self.timeout}s"
                )
            time.sleep(_POLL_SECONDS)

        for node in (entry.get("outputs") or {}).values():
            for image in node.get("images") or []:
                query = urllib.parse.urlencode({
                    "filename": image["filename"],
                    "subfolder": image.get("subfolder", ""),
                    "type": image.get("type", "output"),
                })
                data = self._request(f"/view?{query}", timeout=60)
                width, height = png_size(data)
                return Rendering(
                    data=data,
                    media_type="image/png",
                    width=width,
                    height=height,
                    seed=values.get("seed") if "seed" in self.bound[kind] else None,
                    usage=ImageUsage(images=1, seconds=round(time.time() - started, 2)),
                )
        raise ProviderBadResponse(
            f"ComfyUI finished the {kind} workflow but it saved no image; "
            f"the workflow needs a SaveImage node."
        )

    def generate(
        self,
        prompt: str,
        *,
        width: int = 0,
        height: int = 0,
        seed: int | None = None,
        negative: str = "",
    ) -> Rendering:
        return self._run("generate", {
            "prompt": prompt,
            "negative": negative or None,
            "seed": seed if seed is not None else random.randrange(2**32),
            "width": width or None,
            "height": height or None,
        })

    def edit(
        self,
        prompt: str,
        *,
        image: bytes,
        mask: bytes | None = None,
        seed: int | None = None,
    ) -> Rendering:
        if "edit" not in self.graphs:
            return super().edit(prompt, image=image, mask=mask, seed=seed)
        values: dict[str, Any] = {
            "prompt": prompt,
            "seed": seed if seed is not None else random.randrange(2**32),
            "image": self.upload(image, f"forge-{uuid.uuid4().hex[:12]}.png"),
        }
        if mask is not None and "mask" in self.bound["edit"]:
            values["mask"] = self.upload(mask, f"forge-mask-{uuid.uuid4().hex[:12]}.png")
        return self._run("edit", values)
