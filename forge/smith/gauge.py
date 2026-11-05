"""gauge — calipers. Measures images and the working tree, so nothing is guessed.

Needs Pillow for images. forge itself stays standard-library only: gauge
imports Pillow when it is asked to measure a picture, and says plainly when it
is not installed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Sequence

from ..api import ApiError, Forge
from ..artifacts import Artifacts
from ..patch import is_safe_path

THUMB = 220
SHEET_COLUMNS = 4


def _pil() -> Any:
    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:  # pragma: no cover - depends on the machine
        raise ApiError("gauge needs Pillow to measure images: pip install pillow") from exc
    return Image, ImageDraw


def _data(img: Any) -> Any:
    """Pixel data across Pillow versions (`getdata` is deprecated from 12)."""
    flattened = getattr(img, "get_flattened_data", None)
    return flattened() if flattened else img.getdata()


def measure(path: Path) -> dict[str, Any]:
    """What an image is, in numbers a smith can act on."""
    Image, _ = _pil()
    with Image.open(path) as opened:
        img = opened.convert("RGBA")
        mode = opened.mode
    w, h = img.size
    pixels = list(_data(img))
    opaque = [p for p in pixels if p[3] > 128]
    corners = [img.getpixel(c)[3] for c in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1))]
    result: dict[str, Any] = {
        "size": [w, h],
        "mode": mode,
        "corners_alpha": corners,
        "transparent_corners": all(a == 0 for a in corners),
        "fully_opaque": len(opaque) == len(pixels) and min(p[3] for p in pixels) == 255,
        "opaque_share": round(len(opaque) / len(pixels), 4),
    }
    if opaque:
        n = len(opaque)
        result["opaque_mean_rgb"] = [round(sum(p[i] for p in opaque) / n) for i in range(3)]
        bbox = img.getchannel("A").point(lambda a: 255 if a > 128 else 0).getbbox()
        if bbox:
            bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]
            result["opaque_bbox"] = list(bbox)
            result["bbox_aspect"] = round(bw / bh, 3) if bh else None
        result["colours"] = _dominant(img, opaque)
        cx, cy = w // 2, h // 2
        result["centre_alpha"] = img.getpixel((cx, cy))[3]
    result["bright_columns"], result["bright_rows"] = _lines(img)
    return result


def _dominant(img: Any, opaque: list[tuple[int, int, int, int]], k: int = 5) -> list[str]:
    Image, _ = _pil()
    sample = opaque[:: max(1, len(opaque) // 20000)]
    strip = Image.new("RGB", (len(sample), 1))
    strip.putdata([p[:3] for p in sample])
    quant = strip.quantize(colors=k)
    palette = quant.getpalette() or []
    counts = sorted(quant.getcolors() or [], reverse=True)
    return ["#%02x%02x%02x" % tuple(palette[i * 3:i * 3 + 3]) for _, i in counts[:k]]


def _lines(img: Any) -> tuple[list[int], list[int]]:
    """Where drawn lines are: the brightest columns and rows, peaks kept apart."""
    grey = img.convert("L")
    w, h = grey.size
    px = grey.load()
    y0, y1, x0, x1 = h // 10, h - h // 10, w // 10, w - w // 10
    step = max(1, (y1 - y0) // 200)
    cols = [sum(px[x, y] for y in range(y0, y1, step)) for x in range(w)]
    step = max(1, (x1 - x0) // 200)
    rows = [sum(px[x, y] for x in range(x0, x1, step)) for y in range(h)]
    return _peaks(cols, w // 12), _peaks(rows, h // 12)


def _peaks(values: list[int], apart: int, count: int = 4) -> list[int]:
    chosen: list[int] = []
    for index in sorted(range(len(values)), key=lambda i: values[i], reverse=True):
        if all(abs(index - c) >= apart for c in chosen):
            chosen.append(index)
        if len(chosen) == count:
            break
    return sorted(chosen)


def contact_sheet(paths: Sequence[Path], out: Path, labels: Sequence[str]) -> Path:
    """One labelled sheet on a checkerboard, so transparency shows."""
    Image, ImageDraw = _pil()
    columns = min(SHEET_COLUMNS, max(1, len(paths)))
    rows = (len(paths) + columns - 1) // columns
    cell = THUMB + 24
    sheet = Image.new("RGB", (columns * cell, rows * cell), (255, 255, 255))
    checker = Image.new("RGB", (THUMB, THUMB), (205, 205, 205))
    draw_checker = ImageDraw.Draw(checker)
    for y in range(0, THUMB, 16):
        for x in range(0, THUMB, 16):
            if (x // 16 + y // 16) % 2:
                draw_checker.rectangle([x, y, x + 15, y + 15], fill=(240, 240, 240))
    draw = ImageDraw.Draw(sheet)
    for index, (path, label) in enumerate(zip(paths, labels)):
        x, y = (index % columns) * cell, (index // columns) * cell
        sheet.paste(checker, (x + 12, y + 4))
        with Image.open(path) as opened:
            thumb = opened.convert("RGBA")
            thumb.thumbnail((THUMB, THUMB))
        sheet.paste(thumb, (x + 12 + (THUMB - thumb.width) // 2,
                            y + 4 + (THUMB - thumb.height) // 2), thumb)
        draw.text((x + 12, y + THUMB + 6), label, fill=(0, 0, 0))
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    return out


def tree_changes(root: Path) -> list[dict[str, Any]]:
    """What differs from HEAD, by file, with line counts where git has them."""
    numstat = subprocess.run(
        ["git", "diff", "--numstat", "HEAD"], cwd=root,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).stdout
    changes = []
    for line in numstat.splitlines():
        added, removed, path = line.split("\t", 2)
        changes.append({"path": path, "added": added, "removed": removed})
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"], cwd=root,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).stdout
    for path in untracked.splitlines():
        changes.append({"path": path, "added": "new", "removed": "0"})
    return [c for c in changes if not c["path"].startswith(".hybridforge/")]


def gauge(
    forge: Forge,
    paths: Sequence[str] = (),
    sheet: bool = False,
    changes: bool = False,
) -> dict[str, Any]:
    """Measure images, lay several out on one sheet, or list the tree's changes."""
    root = forge.config.root
    if not paths and not changes:
        raise ApiError("gauge needs `paths` (images to measure) or `changes: true`")
    result: dict[str, Any] = {}
    resolved = []
    for path in paths:
        if not is_safe_path(root, path) or not (root / path).is_file():
            raise ApiError(f"{path} is not a file in this project")
        resolved.append(root / path)
    if resolved:
        result["images"] = {path: measure(file) for path, file in zip(paths, resolved)}
    if sheet and resolved:
        Artifacts(forge.config.config_dir, 0)  # keeps gauge/ out of git
        out = forge.config.config_dir / "gauge" / "sheet.png"
        result["sheet"] = str(contact_sheet(resolved, out, list(paths)))
        result["look"] = "Read the sheet image to see them side by side."
    if changes:
        result["changes"] = tree_changes(root)
    return result
