"""Hidden acceptance checks for TASK.md's pinned assets. Prints JSON.

    python acceptance/assets_accept.py <repo>

Only what the task pins, plus the colour words it states, measured loosely:
an asset that passes here can still be ugly, which is what the screenshots
and the blind visual rating are for.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image


# Fixed: a missing asset fails every check it would have had.
TOTAL = 16


def mean_rgb(img: Image.Image) -> tuple[float, float, float] | None:
    rgba = img.convert("RGBA")
    pixels = [p for p in rgba.get_flattened_data() if p[3] > 128]
    if not pixels:
        return None
    n = len(pixels)
    return (sum(p[0] for p in pixels) / n, sum(p[1] for p in pixels) / n,
            sum(p[2] for p in pixels) / n)


def check(root: Path) -> dict:
    results: dict[str, bool] = {}
    notes: dict[str, str] = {}
    images: dict[str, Image.Image] = {}
    for name in ("x", "o", "board"):
        path = root / "assets" / f"{name}.png"
        try:
            images[name] = Image.open(path)
            images[name].load()
            results[f"{name}: exists and opens"] = True
        except Exception as exc:  # noqa: BLE001
            results[f"{name}: exists and opens"] = False
            notes[name] = str(exc)

    for name in ("x", "o"):
        img = images.get(name)
        if img is None:
            continue
        w, h = img.size
        results[f"{name}: square, at least 128"] = w == h and w >= 128
        rgba = img.convert("RGBA")
        corners = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]
        results[f"{name}: transparent corners"] = all(rgba.getpixel(c)[3] == 0 for c in corners)
        opaque = sum(1 for p in rgba.get_flattened_data() if p[3] > 128) / (w * h)
        results[f"{name}: mark covers 5-60% of the square"] = 0.05 <= opaque <= 0.60
        mean = mean_rgb(img)
        notes[f"{name} mean rgb"] = str(tuple(round(v) for v in mean) if mean else None)
        if mean:
            r, g, b = mean
            if name == "x":
                results["x: reads warm red"] = r > g + 30 and r > b + 30
            else:
                results["o: reads teal"] = g > r + 30 and b > r + 30

    board = images.get("board")
    if board is not None:
        w, h = board.size
        results["board: square, at least 512"] = w == h and w >= 512
        rgba = board.convert("RGBA")
        results["board: fully opaque"] = min(p[3] for p in rgba.get_flattened_data()) == 255
        grey = board.convert("L")
        values = list(grey.get_flattened_data())
        mean = sum(values) / len(values)
        bright = sum(1 for v in values if v > 170) / len(values)
        notes["board mean L"] = f"{mean:.0f}, bright share {bright:.3f}"
        results["board: dark slate"] = 15 <= mean <= 110
        results["board: has light chalk lines"] = bright >= 0.01

    if "x" in images and "o" in images:
        results["x and o differ"] = images["x"].tobytes() != images["o"].tobytes()
    return {"passed": sum(results.values()), "total": TOTAL,
            "checks": results, "notes": notes}


if __name__ == "__main__":
    print(json.dumps(check(Path(sys.argv[1])), indent=1))
