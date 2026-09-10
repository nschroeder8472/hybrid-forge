# The replay that asks whether looking at a UI is worth building

[ROADMAP.md](ROADMAP.md) *Looking at what it built* proposes a step that starts
a UI, captures it, and puts the capture to a role that can see. It also says
what should happen before any of that is built, and this is that: one question,
answered as cheaply as it can be answered.

**Does a reviewer, shown the rendering and the ticket's own criteria, name
defects that a green suite missed?**

Run 2026-09-10. `scripts/ui_replay.py` is the experiment; three of its four
parts ran, and the fourth is blocked on something smaller than the framework.

---

## 1. The original artifact is gone

[CANVAS-POSTMORTEM.md](CANVAS-POSTMORTEM.md) §7 is the case this is about: four
defects that shipped together in one page with 146 tests green and four
commands clean. The intent was to replay against that run's own delivered HTML.

It is not on this machine. Every `run.db` under `Projects/` was opened and none
holds PF-011 to PF-015 — the three `forge-evidence/puzzle-path-*` trees are the
2026-08-29 backlog, PF-002 to PF-010 — and a search for `renderRuler` and
`composeFrame` across every tracked tree returns only this repository's own
prose about them.

So the fixture is the postmortem's description rebuilt: a page carrying the same
four defects, and criteria of the shape that backlog actually carried. That is
weaker evidence than the original in one specific way — the criteria are written
here rather than recovered — and the next section is about not making that
weakness worse.

## 2. The mechanical half reproduces exactly

The page has all four defects. Every column label is painted at
`cx * scale + scale/2` with the viewport origin never subtracted, so at
`origin 95` each label sits 95 pixels from the column it names. The minimap has
a module and nothing imports it, so it is never drawn. The zoom readout shows
`scale * 100` where the spec asked for a percentage of scale 32. The file input
is `display:none` with nothing that opens it.

Eight criteria were written against it, and they were written the way the
postmortem says the real ones were written — counts, indices, gaps, presence,
stated positively, each checkable without looking at anything:

```
PASS  the ruler emits one label per column
PASS  the labels carry the column indices in order, starting at 0
PASS  no two labels share a position
PASS  the labels are ordered left to right
PASS  consecutive labels are one cell apart
PASS  the first label is half a cell from the ruler's own left edge
PASS  the page contains a file input that accepts .json
PASS  the header contains a zoom readout

8/8 criteria pass.
```

**That is the argument, and it needs no model.** Six of those eight are about
the ruler, which is the number the real backlog had, and none of them is false
about a ruler whose every label is in the wrong place. A criterion about a count
cannot fail on a position.

## 3. The capture half needs nothing new

`python scripts/ui_replay.py --capture` renders the page with a browser the
machine already had — headless, one command, exit — and writes a 3.4 KB PNG. No
driver, no runtime dependency, no library.

That bounds what the roadmap entry's first missing piece is actually for. **A
static page needs no long-lived process at all**; the process primitive is for
an app with a server behind it, and a project that renders to a file needs only
a command, which `commands` can already hold. The cheap case is cheaper than the
entry implied.

What it does not settle is the hard cases the entry names — a desktop toolkit, a
UI that cannot run headless, anything needing interaction before the state worth
looking at exists.

## 4. Looking at the capture changed the question

Reading the rendering back, only one of the four defects is visible as an
*internal inconsistency*: the labels sit over nothing, because the cells they
name are 95 pixels away. A reviewer could name that from the picture alone.

The other three cannot be found by looking:

- a minimap that was never drawn is an **absence**, and absences are invisible
  unless you know what should be there;
- an input that is `display:none` is the same, and worse — it renders as nothing
  at all;
- `1600%` is a perfectly plausible number. It is wrong only against a spec that
  asked for a percentage of scale 32.

**So the step must hand the reviewer the spec and the criteria beside the image,
and ask whether the render satisfies them.** Asking a model to "find what is
wrong with this screenshot" would find the one defect that is self-evident and
miss the three that are not — and those three are the ones a suite also missed,
which is the entire reason for the step.

That is the same question the code reviewer is already asked, about a different
artifact. It is an argument for one step and one role rather than a new kind of
review.

## 5. What is blocked, and it is not the framework

**No configured role on this machine can see.** `claude-cli` declares
`supports_images = False`; both llama.cpp models are text-only (`multimodal`
false or absent); there is no `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` or
`GEMINI_API_KEY` set. The strongest model available here is unusable as a vision
reviewer by its adapter's own declaration.

That declaration has a reason, and it also names the way out. From
`claude_cli.py`:

> The prompt reaches the CLI as text on stdin and there is nowhere to put bytes.
> Naming a file path instead would not work either: this adapter defaults to no
> tools, which is what makes it behave like the completion endpoint the loop
> assumes, so a reviewer sent `assets/hero.png` would be ruling on a filename.

The second half is a consequence of a default rather than a limit of the CLI. A
role that is given the read tool *can* open a path it is handed. So there are
three routes to a seeing reviewer, in ascending order of work:

1. **Point a role at a vision endpoint** — an API key, or a llama.cpp checkpoint
   with a projector and `multimodal: true`. No code.
2. **Let `claude-cli` take images by path**, for a role that has tools. It is a
   capability that becomes true under a condition the adapter already knows
   about, which is exactly the shape `roleNeeds` was built for.
3. Everything in the roadmap entry.

Route 1 finishes this experiment. Until then the answer key is in
`scripts/ui_replay.py` under `UNSEEN`, deliberately not printed beside the page:
handing a reviewer the verdict is not a test of the reviewer.

## What this does not claim

The fixture is a reconstruction, so a reviewer naming its defects would be
evidence about a page written to carry them rather than about the run that
shipped them. The stronger version is a UI ticket run live and reviewed blind,
which is the thing the entry is for and which this cannot substitute for.

And the author of the fixture cannot be its judge. Whoever wrote the answer key
knows where to look.
