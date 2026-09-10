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

## 5. The seat that looks — one flag, and it was already paid for

**The first answer was wrong and the checkpoints settled it.** `claude-cli`
declares `supports_images = False`; there is no `ANTHROPIC_API_KEY`,
`OPENAI_API_KEY` or `GEMINI_API_KEY` set; and the executor's checkpoint,
`Qwen3.8-27B-UD-Q4_K_M.gguf`, is `arch=qwen35` with no vision or clip keys in
its metadata and **no `mmproj-*.gguf` anywhere in its repository**. Nothing to
switch on.

The reviewer's checkpoint is a different story. `nemotron-3-nano-omni` ships
`mmproj-Nemotron-3-Nano-Omni-30B-A3B-Reasoning-BF16.gguf` beside its weights —
`arch=clip`, `clip.has_vision_encoder`, 390 tensors — **already downloaded, and
switched off on purpose.** `presets.py` writes `mmproj-auto = false` for any
model whose block does not say `"multimodal": true`, and the router preset's own
header gives the reason: *"spends VRAM no role here uses"*. That reason expired
the moment a role wanted to look.

So the seeing seat cost one flag in `.hybridforge/config.json`, a regenerated
preset, and a router restart — no key, no download, no code. It also lands on
the seat the design already wanted: nemotron serves `reviewer`, which is
`loop.viewRole`'s default, so nothing is reassigned.

What it costs is the projector's VRAM on every load of that checkpoint, which
under `--models-max 1` is whenever the planner or the reviewer runs.

**`scripts/ui_replay.py --review <repo> --role reviewer`** is the judge half,
and it asks the config which model looks rather than deciding for itself. A role
whose provider cannot see is refused before a prompt is built.

`claude-cli`'s declaration has a reason, and it also names a second way out,
worth keeping for a machine with no local vision model. From `claude_cli.py`:

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

**Whichever route supplies the model, the seat is configuration.** The roadmap
entry now settles that: `loop.viewRole` names which of the four roles is shown
the capture, `roleNeeds` declares that it must be able to see, and a project
with no model that can does not get the step. None of the three routes above is
a change to that design — they are three ways to have a model behind the seat,
and the seat is the same in all of them.

## 6. The judge half ran, and the answer is *partly*

2026-09-10, `nemotron-3-nano-omni` with its projector loaded, `reviewer` seat,
temperature 0. Run twice, byte-identical both times — same verdicts, same 706
prompt and 1,415 completion tokens.

Of the four defects it was shown:

| defect | verdict |
|---|---|
| the file input cannot be reached | **found** — *"no file input element for .json is visible"* |
| every label 95px from its column | not named; criteria 1–5 all passed |
| the minimap is never drawn | **missed** — never raised, though the spec names it |
| the readout reads `1600%` | **missed** — read correctly and passed |

And one miscall: it failed *"the first label is half a cell from the ruler's own
left edge"*, which is true — label 0 sits at 8px and the ruler's left edge is 0.
It hedged with *"not clearly"*, which reads like the visual oddity showing
through the wrong criterion.

So one real find, one false failure, two misses. **Not theatre, and not
sufficient.**

**The find is the interesting part, because of where it came from.** Criterion 7
— *"the page contains a file input that accepts .json"* — is the kind of
mechanical wiring assertion `spec-contract` already asks for, and it **passes as
a source check and fails as a render check**. Same sentence, same ticket,
opposite verdict, and the render's verdict is the true one: the input is in the
DOM and no person can reach it. That is the entire mechanism the step would add,
demonstrated on the defect that made the original editor unusable.

**The FINDINGS section added nothing.** Both runs listed exactly what the failed
criteria already said. Every defect the reviewer named, it named because a
criterion asked. That is §4's prediction with evidence under it: the step is
worth building as *re-asking the existing criteria of the rendering*, and not
worth building as "look at this and tell me what is wrong".

**What the misses have in common** is that both need the spec held against the
picture rather than the criteria: the minimap is a sentence in the spec with no
criterion, and `1600%` is only wrong if you carry the *percentage of scale 32*
rule while reading it. The reviewer was given both and used neither. Two
readings are open — a stronger seat would cross-reference, or prose in a spec is
not something any reviewer reliably checks and these needed criteria of their
own. This experiment cannot separate them, and the second reading is the one
this repository already holds — *Prose in a spec body is not a contract;
only criteria are*, in [EXECUTOR-CEILINGS.md](EXECUTOR-CEILINGS.md).

**And the seat is the weakest one available.** A 30B-A3B local checkpoint
missing a 95-pixel offset is evidence about that checkpoint at least as much as
about the step. The same page, the same prompt and a frontier vision model is a
one-call experiment that is still unrun, and it is what should decide how much
of the framework to build.

**Cost, since a false failure is not free.** One of eight criteria was failed
wrongly. On a live ticket that is an attempt spent chasing a defect that is not
there, which is the same cost a wrong reviewer rejection has today.

## 7. The same page, a stronger seat — and the answer changes

The section above ends by saying a 30B local checkpoint missing a 95-pixel
offset is evidence about the checkpoint. That was worth one call to settle, and
`claude-cli` now takes images: `supports_images` is true exactly when the role
has read tools, and an image reaches the CLI as a path it opens rather than as
bytes it has nowhere to put. The adapter's own comment said a filename would be
worse than a refusal — it still is, and having `Read` is the difference.

Same page, same prompt, same criteria. Two runs.

| defect | nemotron-omni | claude (opus, `--tools Read`) |
|---|---|---|
| file input cannot be reached | found | **found**, both runs |
| minimap never drawn | missed | **found**, both runs |
| readout reads `1600%` | missed, passed it | **found**, both runs, with the arithmetic: *"1600% of scale 32 = 512px per cell contradicts the ~15px cells drawn"* |
| every label 95px from its column | not named | **symptom named**, both runs — *"ruler labels 8–11 have no tiles beneath them"*, *"a single row of ~7 tiles"* against 12 labels — and criterion 1 refused rather than passed |
| miscalls | 1 (failed a criterion that holds) | 0 |

**So the misses were the seat.** Three of four found outright, the fourth's
symptom named both times and the criterion it would have passed marked
`UNKNOWN` instead — which is the honest verdict for a picture that cannot show
the offset directly. It also passed criterion 6 correctly, the one nemotron
failed wrongly.

That settles what §6 could not: the step is not limited by what a rendering can
carry, and the framework the entry describes is worth building. It also keeps
§6's other finding intact — every one of these came from a criterion or from the
spec sentence beside it. The minimap was found because the spec named a minimap.

## 8. The capture can invent a defect, and this one did

Both Claude runs reported the zoom readout *"rendered red, styling of an
error/invalid state"*. The page sets no colour on it: `#zoom` declares only
`font-variant-numeric`, and the body colour is `#e6e6e6`.

Decoding the PNG settles it. Over the readout, beside the header fill
`rgb(28,31,38)` and the declared `rgb(230,230,230)`, the glyphs carry
`rgb(217,180,122)` and `rgb(116,180,218)` — warm and cool fringes on opposite
edges, which is subpixel antialiasing. The capture rendered grey text with
coloured edges and the reviewer read the colour as meaning.

**That is a capture-configuration defect, not a model defect, and it is the
concrete form of the entry's "determinism" question.** It is not only fonts and
DPI moving pixels: a renderer's default text antialiasing can manufacture a
finding that is nowhere in the page, and on a live ticket that is an attempt
spent on a colour nobody chose. Whatever command the project supplies should
disable subpixel antialiasing, and the entry should say so rather than leaving
"determinism" as a word.

## What this does not claim

The fixture is a reconstruction, so a reviewer naming its defects would be
evidence about a page written to carry them rather than about the run that
shipped them. The stronger version is a UI ticket run live and reviewed blind,
which is the thing the entry is for and which this cannot substitute for.

And the author of the fixture cannot be its judge. Whoever wrote the answer key
knows where to look — which is why both seats were sent the same prompt with the
answer key withheld, and why the extra findings in §7 that are *not* among the
four are reported here rather than counted: no grid drawn, a ruler strip that
stops at the stage's width, tiles hugging x=0. Those are true of the fixture and
say more about how crudely it was built than about either model.
