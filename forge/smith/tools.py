"""The smith's tool table, as the MCP server and `forge tool` present it.

Descriptions say when a tool pays and when it does not, because the smith
decides whether to pick one up at all.
"""

from __future__ import annotations

from ..mcp.tools import Tool, _object
from .anvil import anvil
from .gauge import gauge
from .mold import mold

_STRING = {"type": "string"}

SMITH_TOOLS: tuple[Tool, ...] = (
    Tool(
        name="anvil",
        description=(
            "Run the project's lint, typecheck and test commands and attribute every "
            "failure: `introduced` (since your baseline), `already` (failing before), "
            "`environment` (the command never reached the code). Also flags relative "
            "imports that resolve to nothing and dependencies a manifest dropped, in "
            "files changed since HEAD. Call `mark: true` once before you edit; after "
            "that, call it whenever you want to know where you stand. `step: <id>` "
            "returns one step's full output. Cheap; use it instead of running the "
            "commands yourself when attribution matters."
        ),
        schema=_object({"mark": {"type": "boolean"}, "step": {"type": "integer"}}),
        handler=lambda forge, a: anvil(
            forge, mark=bool(a.get("mark", False)), step=int(a.get("step", 0) or 0)
        ),
    ),
    Tool(
        name="gauge",
        description=(
            "Measure instead of guessing. `paths`: images in the project — size, "
            "transparent corners, opaque share and bounding box, colours of what is "
            "opaque, centre alpha, and the brightest columns and rows (where drawn "
            "lines are). `sheet: true` also lays them out on one labelled contact "
            "sheet on a checkerboard; Read that one image to see them all. "
            "`changes: true` lists what differs from HEAD. Use it before judging a "
            "picture by eye, or when a number matters."
        ),
        schema=_object({
            "paths": {"type": "array", "items": _STRING},
            "sheet": {"type": "boolean"},
            "changes": {"type": "boolean"},
        }),
        handler=lambda forge, a: gauge(
            forge, paths=list(a.get("paths") or []), sheet=bool(a.get("sheet")),
            changes=bool(a.get("changes")),
        ),
    ),
    Tool(
        name="mold",
        description=(
            "Make art you cannot draw: render `n` candidates (default 4, at most 8) "
            "from your `prompt` with the project's image model, run the project's "
            "`.png` formatter on each, measure each, and check `require` — measured "
            "predicates such as {\"transparent_corners\": true, \"opaque_share\": "
            "[0.05, 0.6], \"min_size\": 256}. Returns the numbers and one contact "
            "sheet; Read it, then place your choice with `batch`, `pick` and `to`. "
            "`name` is the asset's role (x, o, board): the formatter sees "
            "`<name>-<k>.png`. `edit: <path>` changes an existing image by "
            "instruction instead. A batch takes about 30 s per candidate. Nothing "
            "here judges the picture; you do."
        ),
        schema=_object({
            "prompt": _STRING,
            "n": {"type": "integer", "minimum": 1, "maximum": 8},
            "name": _STRING,
            "require": {"type": "object"},
            "edit": _STRING,
            "negative": _STRING,
            "batch": _STRING,
            "pick": {"type": "integer"},
            "to": _STRING,
        }),
        handler=lambda forge, a: mold(forge, **a),
    ),
)

SMITH_BY_NAME = {tool.name: tool for tool in SMITH_TOOLS}
