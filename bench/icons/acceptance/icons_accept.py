"""Hidden checks for TASK.md's pinned icons. Prints JSON.

    python acceptance/icons_accept.py <repo>

Format and loose colour cues only; how good they look is the blind rating's job.
"""

from __future__ import annotations

import colorsys
import json
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

ITEMS = ["sword", "shield", "potion", "scroll", "ring", "helmet"]


def pixels(img: Image.Image) -> list:
    flat = getattr(img, "get_flattened_data", None)
    return list(flat() if flat else img.getdata())


def hues(img: Image.Image) -> dict[str, float]:
    """Share of opaque pixels that read red, blue, gold, or grey-bright."""
    counts = {"red": 0, "blue": 0, "gold": 0, "steel": 0}
    opaque = [p for p in pixels(img) if p[3] > 128]
    for r, g, b, _ in opaque:
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        if s < 0.2 and v > 0.45:
            counts["steel"] += 1
        elif s > 0.4 and v > 0.35:
            if h < 0.05 or h > 0.93:
                counts["red"] += 1
            elif 0.08 <= h <= 0.17:
                counts["gold"] += 1
            elif 0.52 <= h <= 0.72:
                counts["blue"] += 1
    n = max(1, len(opaque))
    return {k: v / n for k, v in counts.items()}


def check(root: Path) -> dict:
    results: dict[str, object] = {}
    images: dict[str, Image.Image] = {}
    for item in ITEMS:
        try:
            img = Image.open(root / "icons" / f"{item}.png")
            img.load()
            images[item] = img.convert("RGBA")
            ok = img.mode == "RGBA" and img.size == (256, 256)
        except Exception:  # noqa: BLE001
            ok = False
        results[f"{item}: RGBA 256 x 256"] = ok
    for item, img in images.items():
        alpha = img.getchannel("A")
        corners = all(alpha.getpixel(p) == 0 for p in ((0, 0), (255, 0), (0, 255), (255, 255)))
        share = sum(1 for a in pixels(alpha) if a > 128) / 65536
        results[f"{item}: transparent background, item present"] = corners and 0.08 <= share <= 0.8
    cues = {item: hues(img) for item, img in images.items()}
    if "potion" in cues:
        results["potion reads red"] = cues["potion"]["red"] >= 0.12
    if "ring" in cues:
        ring = cues["ring"]
        results["ring shows gold and blue"] = ring["gold"] >= 0.08 and ring["blue"] >= 0.04
    if "helmet" in cues:
        results["helmet reads steel"] = cues["helmet"]["steel"] >= 0.2
    distinct = [
        sum(ImageStat.Stat(ImageChops.difference(images[a], images[b])).mean) / 4 > 8
        for i, a in enumerate(images) for b in list(images)[i + 1:]
    ]
    results["the six icons differ"] = len(images) == 6 and all(distinct)
    try:
        sheet = Image.open(root / "icons" / "sheet.png")
        results["sheet: 1536 x 256"] = sheet.size == (1536, 256)
    except Exception:  # noqa: BLE001
        results["sheet: 1536 x 256"] = False
    total = 6 + 6 + 3 + 1 + 1
    passed = sum(1 for v in results.values() if v is True)
    return {"passed": passed, "total": total, "checks": results,
            "notes": {k: {h: round(x, 3) for h, x in v.items()} for k, v in cues.items()}}


if __name__ == "__main__":
    print(json.dumps(check(Path(sys.argv[1])), indent=1))
