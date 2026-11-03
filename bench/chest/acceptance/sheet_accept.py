"""Hidden checks for TASK.md's pinned sprite sheet. Prints JSON.

    python acceptance/sheet_accept.py <repo>

Mechanical and deliberately loose: the format, a chest in every frame at a
steady scale, angles that actually differ, positions that actually differ, and
an open chest with more gold and gem colour than a closed one. Whether it is a
*good* chest is the blind rating's job.
"""

from __future__ import annotations

import colorsys
import json
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

ROWS = ["closed", "ajar", "open"]
COLS = ["iso_0", "iso_45", "iso_90", "iso_135", "iso_180", "iso_225", "iso_270",
        "iso_315", "flat_front", "flat_right", "flat_back", "flat_left"]
F = 128
TOTAL = 14


def pixels(img: Image.Image) -> list:
    flat = getattr(img, "get_flattened_data", None)
    return list(flat() if flat else img.getdata())


def frame(sheet: Image.Image, r: int, c: int) -> Image.Image:
    return sheet.crop((c * F, r * F, (c + 1) * F, (r + 1) * F))


def gold_and_gems(img: Image.Image) -> tuple[int, int]:
    gold = gems = 0
    for r, g, b, a in pixels(img):
        if a < 128:
            continue
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        if s > 0.45 and v > 0.55:
            if 0.08 <= h <= 0.17:
                gold += 1
            elif h < 0.04 or h > 0.25:
                gems += 1
    return gold, gems


def check(root: Path) -> dict:
    results: dict[str, object] = {}
    notes: dict[str, object] = {}
    try:
        sheet = Image.open(root / "sheets" / "chest.png")
        sheet.load()
    except Exception as exc:  # noqa: BLE001
        return {"passed": 0, "total": TOTAL, "checks": {"sheet opens": str(exc)}}
    results["sheet opens"] = True
    results["RGBA, 1536 x 384"] = sheet.mode == "RGBA" and sheet.size == (1536, 384)
    sheet = sheet.convert("RGBA")

    try:
        meta = json.loads((root / "sheets" / "chest.json").read_text(encoding="utf-8"))
        frames = meta.get("frames", {})
        want = {f"{row}/{col}": [c * F, r * F, F, F]
                for r, row in enumerate(ROWS) for c, col in enumerate(COLS)}
        results["frame map: 36 frames at the pinned places"] = (
            meta.get("frame") == [F, F]
            and {k: list(v) for k, v in frames.items()} == want)
    except Exception as exc:  # noqa: BLE001
        results["frame map: 36 frames at the pinned places"] = False
        notes["frame map"] = str(exc)

    shares, sizes, corners_ok = [], [], True
    for r in range(3):
        for c in range(12):
            f = frame(sheet, r, c)
            alpha = f.getchannel("A")
            corners_ok &= all(alpha.getpixel(p) == 0 for p in ((0, 0), (F - 1, 0), (0, F - 1), (F - 1, F - 1)))
            share = sum(1 for a in pixels(alpha) if a > 128) / (F * F)
            shares.append(share)
            box = alpha.point(lambda a: 255 if a > 128 else 0).getbbox()
            sizes.append(max(box[2] - box[0], box[3] - box[1]) if box else 0)
    results["every frame has transparent corners"] = corners_ok
    results["every frame holds a chest (3-85% opaque)"] = all(0.03 <= s <= 0.85 for s in shares)
    iso_sizes = [sizes[r * 12 + c] for r in range(3) for c in range(8)]
    closed_iso = [sizes[c] for c in range(8)]
    median = sorted(closed_iso)[4] if closed_iso else 0
    results["steady scale across the closed iso turn (within 30%)"] = bool(median) and all(
        abs(s - median) <= 0.3 * median for s in closed_iso)
    notes["opaque share range"] = [round(min(shares), 3), round(max(shares), 3)]
    notes["closed iso sizes"] = closed_iso

    def differ(a: Image.Image, b: Image.Image) -> float:
        return sum(ImageStat.Stat(ImageChops.difference(a, b)).mean[:4]) / 4

    for r, row in enumerate(ROWS):
        changes = [differ(frame(sheet, r, c), frame(sheet, r, c + 1)) for c in range(7)]
        results[f"{row}: the iso turn changes the view"] = sum(d > 1.5 for d in changes) >= 6
    flats = [frame(sheet, 0, c) for c in range(8, 12)]
    results["closed: the four flat views differ"] = sum(
        differ(flats[i], flats[j]) > 1.5 for i in range(4) for j in range(i + 1, 4)) >= 5
    results["positions differ (closed vs open, every column)"] = all(
        differ(frame(sheet, 0, c), frame(sheet, 2, c)) > 2 for c in range(12))

    closed = [gold_and_gems(frame(sheet, 0, c)) for c in range(8)]
    opened = [gold_and_gems(frame(sheet, 2, c)) for c in range(8)]
    notes["iso gold / gem pixels, closed vs open"] = [
        [sum(g for g, _ in closed), sum(m for _, m in closed)],
        [sum(g for g, _ in opened), sum(m for _, m in opened)]]
    results["open shows more gold than closed"] = sum(g for g, _ in opened) > 1.3 * max(
        1, sum(g for g, _ in closed))
    results["open shows gem colours"] = sum(m for _, m in opened) > max(
        200, 1.5 * sum(m for _, m in closed))

    passed = sum(1 for v in results.values() if v is True)
    return {"passed": passed, "total": TOTAL, "checks": results, "notes": notes}


if __name__ == "__main__":
    print(json.dumps(check(Path(sys.argv[1])), indent=1))
