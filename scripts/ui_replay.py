"""The experiment that asks whether looking at a UI would have caught anything.

[ROADMAP.md] *Looking at what it built* proposes a step that starts a UI,
captures it, and puts the capture to a role that can see. Before any of that is
built, one question decides whether it is worth building: **shown the rendering
and the ticket's own criteria, does a reviewer name defects that a green suite
missed?**

`docs/CANVAS-POSTMORTEM.md` §7 is the case. Four defects shipped together in one
page with 146 tests green and four commands clean: a ruler whose labels sat 95
pixels from the columns they named, a component nothing imported and so never
drew, a zoom readout showing `1500%` where the spec said a percentage of scale
32, and a file input `hidden` with nothing to open it.

**That run's tree is not on this machine.** No `run.db` here holds PF-011 to
PF-015 and nothing on disk carries `renderRuler`, so the replay cannot be run
against the original artifact. What this builds instead is the same page from
the postmortem's description: the four defects, and criteria of the shape the
backlog actually carried -- counts and indices, positively phrased, none of them
about where anything is on the screen.

The suite that ships with it passes. That is the point. It is the mechanical
half of the experiment and it needs no model at all:

    python scripts/ui_replay.py            # build the page, check the criteria
    python scripts/ui_replay.py --page     # print the page's path and stop
    python scripts/ui_replay.py --capture  # and write page.png beside it

`--capture` is the smallest possible prototype of the capture step the entry
proposes: a browser the machine already has, run headless, told to write one
PNG and exit. No driver, no runtime dependency, no long-lived process -- which
is as far as a *static* page goes, and exactly as far as this experiment needs
to go. `FORGE_BROWSER` names the binary when the defaults below miss.

The other half needs a role that can see, and this machine has none -- the
`claude-cli` adapter declares `supports_images = False`, both llama.cpp models
are text-only, and there is no key for a vision endpoint. When there is one, the
criteria this prints are what to put beside the screenshot.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent / "ui-replay"

# The viewport the postmortem describes: `fit` produces `scale 16` with an
# origin that is not the world origin, and the ruler's labels were positioned at
# `cx * scale + scale / 2` with `view.origin` never subtracted.
SCALE = 16
ORIGIN_X = 95
COLUMNS = 12

# What the spec said the readout shows: a percentage of scale 32. At `scale 16`
# that is 50%. What shipped was `scale * 100`.
NOMINAL_SCALE = 32


def label_positions(scale: int, origin_x: int, columns: int) -> list[int]:
    """Where each column label is painted, as the shipped code painted it.

    `origin_x` is accepted and never subtracted, which is the defect. Every
    number this returns is a world coordinate in a viewport that is not at the
    world origin, so at the default `fit` the label for column 0 sits `origin_x`
    pixels from column 0.
    """
    return [cx * scale + scale // 2 for cx in range(columns)]


def column_positions(scale: int, origin_x: int, columns: int) -> list[int]:
    """Where the columns themselves are painted. The subtraction happens here."""
    return [cx * scale + scale // 2 - origin_x for cx in range(columns)]


def zoom_readout(scale: int) -> str:
    """What the readout shows. The spec asked for a percentage of scale 32."""
    return f"{scale * 100}%"


def page(scale: int, origin_x: int, columns: int) -> str:
    """One page carrying all four defects, drawn from the same numbers."""
    labels = label_positions(scale, origin_x, columns)
    cells = column_positions(scale, origin_x, columns)
    width = columns * scale + 40
    ticks = "\n".join(
        f'    <div class="tick" style="left:{x}px">{index}</div>'
        for index, x in enumerate(labels)
    )
    grid = "\n".join(
        f'    <div class="cell" style="left:{x}px"></div>' for x in cells
    )
    return f"""<!doctype html>
<meta charset="utf-8">
<title>Puzzle-Path editor</title>
<style>
  body {{ font: 13px system-ui, sans-serif; margin: 0; background: #14161a; color: #e6e6e6; }}
  header {{ display: flex; gap: 16px; align-items: center; padding: 10px 14px;
            background: #1c1f26; border-bottom: 1px solid #2a2e37; }}
  #zoom {{ font-variant-numeric: tabular-nums; }}
  /* The file input the editor loads a level with. Hidden, with nothing that
     opens it -- the fourth defect, exactly as it shipped. */
  #open {{ display: none; }}
  #stage {{ position: relative; height: 260px; overflow: hidden; }}
  .ruler {{ position: absolute; top: 0; left: 0; right: 0; height: 22px;
            background: #21252e; border-bottom: 1px solid #2a2e37; }}
  .tick {{ position: absolute; top: 4px; transform: translateX(-50%);
           font-size: 11px; color: #9aa3b2; }}
  .cell {{ position: absolute; top: 40px; width: {scale - 2}px; height: {scale - 2}px;
           background: #3b82f6; border-radius: 2px; }}
  .grid {{ position: absolute; top: 0; left: 0; right: 0; bottom: 0; }}
</style>
<header>
  <strong>Puzzle-Path</strong>
  <span id="zoom">{zoom_readout(scale)}</span>
  <input id="open" type="file" accept=".json">
</header>
<div id="stage" style="width:{width}px">
  <div class="ruler">
{ticks}
  </div>
  <div class="grid">
{grid}
  </div>
  <!-- The minimap has a module of its own and nothing imports it, so it is
       never drawn. The second defect: present in the spec, absent on screen. -->
</div>
"""


# The criteria the backlog actually carried, in the shape it carried them:
# counts and indices, stated positively, each one checkable without looking.
# Every one of these passes against the page above.
CRITERIA = [
    ("the ruler emits one label per column",
     lambda: len(label_positions(SCALE, ORIGIN_X, COLUMNS)) == COLUMNS),
    ("the labels carry the column indices in order, starting at 0",
     lambda: list(range(COLUMNS)) == list(range(len(label_positions(SCALE, ORIGIN_X, COLUMNS))))),
    ("no two labels share a position",
     lambda: len(set(label_positions(SCALE, ORIGIN_X, COLUMNS))) == COLUMNS),
    ("the labels are ordered left to right",
     lambda: label_positions(SCALE, ORIGIN_X, COLUMNS)
     == sorted(label_positions(SCALE, ORIGIN_X, COLUMNS))),
    ("consecutive labels are one cell apart",
     lambda: all(
         b - a == SCALE
         for a, b in zip(
             label_positions(SCALE, ORIGIN_X, COLUMNS),
             label_positions(SCALE, ORIGIN_X, COLUMNS)[1:],
         )
     )),
    ("the first label is half a cell from the ruler's own left edge",
     lambda: label_positions(SCALE, ORIGIN_X, COLUMNS)[0] == SCALE // 2),
    ("the page contains a file input that accepts .json",
     lambda: 'id="open" type="file" accept=".json"' in page(SCALE, ORIGIN_X, COLUMNS)),
    ("the header contains a zoom readout",
     lambda: 'id="zoom"' in page(SCALE, ORIGIN_X, COLUMNS)),
]

# What is true of the page and no criterion above can see. Not checked here --
# this is the answer key for the experiment, and printing it beside the page
# would be handing the reviewer the verdict.
UNSEEN = [
    f"every column label is painted {ORIGIN_X}px from the column it names, "
    f"because the viewport origin is never subtracted",
    "the minimap is never drawn, because nothing imports it",
    f"the zoom readout shows {zoom_readout(SCALE)} where the spec asked for a "
    f"percentage of scale {NOMINAL_SCALE} ({SCALE * 100 // NOMINAL_SCALE}%)",
    "the file input is display:none with nothing that opens it, so no level "
    "can be loaded",
]


def build() -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "page.html"
    path.write_text(page(SCALE, ORIGIN_X, COLUMNS), encoding="utf-8")
    return path


# Where a headless-capable browser usually is, tried in order. Every one of
# them is something a machine already has: the point of the capture step is
# that the project supplies the command, not that this repository acquires a
# driver.
BROWSERS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/chromium",
    "/usr/bin/google-chrome",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)


def capture(page_path: Path, size: tuple[int, int] = (460, 300)) -> Path:
    """Render `page_path` to a PNG beside it, and return where it went."""
    named = os.environ.get("FORGE_BROWSER", "")
    candidates = [named] if named else [b for b in BROWSERS if Path(b).exists()]
    if not candidates:
        raise SystemExit(
            f"no headless-capable browser found. Set FORGE_BROWSER to one, or "
            f"render {page_path} by hand."
        )
    out = page_path.with_suffix(".png")
    width, height = size
    subprocess.run(
        [
            candidates[0],
            "--headless=new",
            "--disable-gpu",
            f"--screenshot={out}",
            f"--window-size={width},{height}",
            # Long enough for layout and fonts, short enough that a page which
            # never settles still returns.
            "--virtual-time-budget=2000",
            page_path.as_uri(),
        ],
        capture_output=True,
        check=False,
        timeout=120,
    )
    if not out.is_file():
        raise SystemExit(f"{candidates[0]} wrote no screenshot to {out}")
    return out


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--page", action="store_true", help="build and print the path")
    parser.add_argument("--capture", action="store_true", help="also render it to a PNG")
    parser.add_argument("--answers", action="store_true", help="print the answer key")
    args = parser.parse_args(argv)

    path = build()
    if args.page:
        print(path)
        return 0

    if args.capture:
        print(f"shot: {capture(path)}")
    print(f"page: {path}\n")
    failed = 0
    for text, check in CRITERIA:
        ok = bool(check())
        failed += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {text}")
    print(f"\n{len(CRITERIA) - failed}/{len(CRITERIA)} criteria pass.")

    if args.answers:
        print("\nWhat is wrong with the page anyway:")
        for item in UNSEEN:
            print(f"  - {item}")
    else:
        print(
            "\nEvery criterion above is about a count, an index or a gap, which "
            "is the shape\nthe canvas backlog's criteria actually had. Run with "
            "--answers for what they miss."
        )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
