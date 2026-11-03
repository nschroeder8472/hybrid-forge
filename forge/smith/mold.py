"""mold — casting. Several renders, finished and measured, on one sheet.

`mold(prompt=..., n=4, name="x")` renders n candidates with the project's
image model (`loop.imageModel`), runs the project's `.png` formatter on each —
named `<name>-<k>.png`, so a finisher that keys on the filename sees what the
asset is — measures each with gauge, and checks `require`. The smith looks at
the one sheet and picks: `mold(batch=..., pick=k, to="assets/x.png")`.

`edit` names an existing image to change by instruction, with `loop.editModel`
(for example Flux Kontext), instead of rendering from nothing.

The image model only renders. Nothing here judges a picture.
"""

from __future__ import annotations

import json
import random
import shutil
import time
from pathlib import Path
from typing import Any

from .. import gpu
from ..artifacts import Artifacts
from ..api import ApiError, Forge
from ..config import ConfigError
from ..engine import servers_for
from ..patch import is_safe_path
from ..providers import ProviderError
from ..servers import ServerError
from ..state import Ticket
from .gauge import contact_sheet, measure

MAX_CANDIDATES = 8


def mold(
    forge: Forge,
    prompt: str = "",
    n: int = 4,
    name: str = "image",
    require: dict[str, Any] | None = None,
    edit: str = "",
    negative: str = "",
    batch: str = "",
    pick: int = 0,
    to: str = "",
) -> dict[str, Any]:
    if pick or batch:
        return _place(forge, batch, pick, to)
    if not prompt.strip():
        raise ApiError("mold needs a `prompt` (or `batch`, `pick` and `to` to place one)")
    n = max(1, min(int(n), MAX_CANDIDATES))
    config = forge.config
    model = config.loop.edit_model if edit else config.loop.image_model
    if not model:
        raise ApiError(
            "this project names no image model (loop.editModel)" if edit
            else "this project names no image model (loop.imageModel)"
        )
    source = b""
    if edit:
        if not is_safe_path(config.root, edit) or not (config.root / edit).is_file():
            raise ApiError(f"{edit} is not an image in this project")
        source = (config.root / edit).read_bytes()

    try:
        provider = config.image_provider(model)
        servers_for(config, images=True, llama=False)  # mold needs no text model
    except (ConfigError, ServerError) as exc:
        raise ApiError(f"the image model cannot run: {exc}") from exc

    batch = time.strftime("%Y%m%d-%H%M%S")
    folder = config.config_dir / "mold" / batch
    Artifacts(config.config_dir, 0)  # keeps mold/ out of git
    folder.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in name) or "image"

    started = time.time()
    unloaded, _ = gpu.clear_llama(config)
    candidates: list[dict[str, Any]] = []
    try:
        for k in range(1, n + 1):
            seed = random.randrange(2**31)
            try:
                rendering = (provider.edit(prompt, image=source, seed=seed) if edit
                             else provider.generate(prompt, seed=seed, negative=negative))
            except ProviderError as exc:
                candidates.append({"k": k, "error": str(exc)})
                continue
            raw = folder / f"{safe}-{k}.raw.png"
            raw.write_bytes(rendering.data)
            final = folder / f"{safe}-{k}.png"
            final.write_bytes(rendering.data)
            candidates.append({"k": k, "seed": rendering.seed, "file": final})
    finally:
        gpu.release_images(config)

    formatted = _format(forge, [c["file"] for c in candidates if "file" in c])
    for candidate in candidates:
        if "file" not in candidate:
            continue
        metrics = measure(candidate["file"])
        failed = _check(metrics, require or {})
        candidate.update(passed=not failed, failed=failed, metrics=_compact(metrics))

    shown = [c for c in candidates if "file" in c]
    sheet = folder / "sheet.png"
    if shown:
        contact_sheet(
            [c["file"] for c in shown], sheet,
            [f"{c['k']}" + ("" if c["passed"] else " (fails require)") for c in shown],
        )
    record = {
        "prompt": prompt, "model": model, "edit": edit, "name": safe, "require": require,
        "candidates": [{**c, "file": str(c.get("file", ""))} for c in candidates],
        "seconds": round(time.time() - started, 1), "unloaded": unloaded,
    }
    (folder / "batch.json").write_text(json.dumps(record, indent=1), encoding="utf-8")
    forge.store.log(forge._host_run(), f"mold {batch}: {len(shown)} of {n} rendered",
                    kind="smith", data={"tool": "mold", "seconds": record["seconds"]})

    passing = [c["k"] for c in shown if c["passed"]]
    return {
        "batch": batch,
        "model": model,
        "seconds": record["seconds"],
        "formatted": formatted,
        "candidates": [
            {key: value for key, value in c.items() if key != "file"} for c in candidates
        ],
        "sheet": str(sheet) if shown else None,
        "next": (
            f"Read the sheet, then mold(batch=\"{batch}\", pick=<k>, to=\"assets/{safe}.png\")"
            if passing else "nothing met `require`; change the prompt and cast again"
        ),
    }


def _format(forge: Forge, files: list[Path]) -> bool:
    """Run the project's `.png` formatter on the candidates, as the loop does."""
    if not files or not forge.config.commands_for("format"):
        return False
    runner = forge._runner()
    root = forge.config.root
    relative = [f.relative_to(root).as_posix() for f in files]
    runner._format_pass(forge._host_run(), Ticket("mold"), relative)
    return True


def _check(metrics: dict[str, Any], require: dict[str, Any]) -> list[str]:
    """Which of the measured predicates a candidate fails."""
    failed = []
    for key, want in require.items():
        have = metrics.get(key)
        if isinstance(want, list) and len(want) == 2 and all(
            isinstance(v, (int, float)) for v in want
        ) and isinstance(have, (int, float)):
            if not want[0] <= have <= want[1]:
                failed.append(f"{key} {have} not in {want}")
        elif key == "min_size":
            if min(metrics.get("size", [0, 0])) < int(want):
                failed.append(f"size {metrics.get('size')} below {want}")
        elif have != want:
            failed.append(f"{key} is {have}, wanted {want}")
    return failed


def _compact(metrics: dict[str, Any]) -> dict[str, Any]:
    keep = ("size", "transparent_corners", "fully_opaque", "opaque_share",
            "opaque_mean_rgb", "bbox_aspect", "centre_alpha", "colours",
            "bright_columns", "bright_rows")
    return {k: metrics[k] for k in keep if k in metrics}


def _place(forge: Forge, batch: str, pick: int, to: str) -> dict[str, Any]:
    root = forge.config.root
    folder = forge.config.config_dir / "mold" / batch
    record_path = folder / "batch.json"
    if not batch or not record_path.is_file():
        raise ApiError(f"no mold batch {batch!r}")
    if not to or not is_safe_path(root, to):
        raise ApiError("`to` must be a path inside the project")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    chosen = next((c for c in record["candidates"] if c.get("k") == int(pick)), None)
    if chosen is None or not chosen.get("file"):
        raise ApiError(f"batch {batch} has no rendered candidate {pick}")
    target = root / to
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(chosen["file"], target)
    return {"placed": to, "from": f"{batch}#{pick}", "metrics": _compact(measure(target))}
