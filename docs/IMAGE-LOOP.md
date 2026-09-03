# Image generation loop — design spec

**Status:** phase 1 built; phases 2-6 designed, not built. Multimodal messages
ship on their own — a reviewer can be handed a screenshot on an ordinary code
ticket, with no image generation anywhere in the picture. Nothing else in this
document exists in the code: there is no image provider, no `kind: image`, and
no image ticket has ever run.

The loop's steps are named after code — build, apply, verify, review — but only
two of them are actually about code. The rest is a shape: *produce an artifact
under a scope, check it mechanically, have something that gains nothing from
passing rule on it, and refine until a criterion is met or the budget runs out.*
Image generation is that shape with a different artifact.

The claim this spec tests is that the shape is the valuable part. If it is, an
image ticket is a `kind`, the way a bug ticket is — not a sibling tool with its
own state machine.

---

## What has no evidence yet

[LANGUAGE-COVERAGE.md](LANGUAGE-COVERAGE.md) opens with three failing runs. This
document opens with none. Every claim in it is derived from reading the loop, not
from watching an image ticket fail, and the repo's own rule — §9, validate
against recorded artifacts and not against reasoning — says that is the weak
kind of argument.

So the phases below are ordered so that the first one is worth building whether
or not the rest of the spec survives contact: multimodal messages are how a
reviewer reads a screenshot, and that is useful on a pure-code run with no image
generation anywhere near it.

---

## Why here and not in a fork

What an image loop would need and this repo already has, none of which knows
what code is:

| | |
|---|---|
| `state.py` | runs, tickets, steps, events, usage, control |
| `budget.py` | rate-limit gate, `waiting_budget`, wake-on-reset |
| `loop.py` retry/respec | attempts, `attempt_base`, drift anchors, `_park` |
| `patch.py` | `enforce_scope`, `is_safe_path`, path normalisation |
| `artifacts.py` | durable per-step recording |
| `memory.py` | retrieved context, droppable under pressure |
| `ui/` | backlog, event stream, per-model spend |

What it needs that does not exist: a provider that returns pixels, a message that
can carry them, and something that plays the part the compiler plays. The first
two are additive. The third is the whole risk, and §"Verification" below is
mostly about how little of it can be automated.

---

## The ticket

```markdown
## IMG-001 — hero illustration for the landing page

**Route:** delegate
**Kind:** image

## Spec

A single wide illustration for the top of the landing page: an anvil on a
workbench, warm light from the left, no text anywhere in the frame.

## Allowed files

- assets/hero.png

## Reference files

- assets/brand/palette.json
- assets/brand/logo.svg

## Acceptance criteria

- 2400x1000 px, PNG, sRGB, no alpha channel
- Every dominant colour within ΔE 5 of an entry in assets/brand/palette.json
- No glyphs anywhere in the frame
- Under 900 KB
- The anvil is the largest object and sits left of centre
- The light source reads as coming from the left
```

`ingest` already parses `**Kind:**` into `Ticket.kind` without validating it
(`ingest.py:409`), so an `image` ticket parses today and then runs down the
feature path, which would do something incoherent. **The gate has to be
explicit**: an unknown or unsupported kind blocks before any model is called,
rather than being interpreted as `feature`.

The criteria list above is deliberately split. The first four are assertions a
script can make. The last two are things only a judge can rule on. Nothing in
between is allowed — see §"The criteria gate".

---

## Step mapping

| code | image |
|---|---|
| BUILD — executor writes files against the spec | generate, or edit the previous attempt |
| APPLY — edits land, scope enforced | bytes land at one allowed path, same enforcement |
| TESTS — tester encodes the criteria | tester writes a check script from the mechanical criteria |
| VERIFY — lint / typecheck / test | unchanged: `commands.test[".png"]` runs the script |
| REVIEW — reviewer reads the diff | reviewer *sees* the image and rules per criterion |
| RECORD / COMMIT | unchanged, with one binary caveat below |

The middle three are the interesting result: **verification needs no new loop
machinery at all.** `commands` is already a map from language to shell string,
and `_verify_plan` already builds the cross product of step and language. `.png`
is a language with a runner, and `python tools/check_IMG-001.py` is that runner.

---

## Problem 1 — `Message.content` is a `str` — built

A reviewer that cannot see the image is not a reviewer. `Message.content` was
typed `str` and every adapter formatted it as one.

The change is to make content a sequence of parts:

```python
@dataclass
class ImagePart:
    media_type: str          # "image/png"
    data: bytes              # inlined; base64 at the wire edge, per provider

@dataclass
class TextPart:
    text: str

@dataclass
class Message:
    role: Literal["system", "user", "assistant"]
    content: str | list[TextPart | ImagePart]
```

A `str` stays legal and means `[TextPart(...)]`, so nothing that builds a prompt
today changes. `split_system` is unaffected. Three consequences that are not
optional:

**`count_tokens` has to price an image, or the budget gate lies.** `base.py`
already says the gate "is only as good as this number". Anthropic and Gemini both
bill image input in tokens — roughly `(width × height) / 750` for Anthropic — and
a review prompt carrying a 2400×1000 image is several thousand tokens the
estimator would report as zero. Which means the daemon has to know an image's
dimensions to price it, and §"No decoding in the daemon" says it must not read
pixels. The resolution: the *provider* reports the size it requested, the
`ImagePart` carries `width`/`height` as metadata supplied at construction, and an
`ImagePart` with no dimensions is priced at the provider's worst case.

**A provider must declare whether it can see.** `Capabilities` gains
`supports_images: bool`, defaulting to `False`. It is per *checkpoint* on
`llamacpp`, not per adapter: a GGUF with a projector beside it can, one without
cannot, and forge turns the projector off by default (`mmproj-auto = false`)
precisely because it costs VRAM no text-only role uses. An image ticket has to
set `multimodal: true` on the model that reviews it and regenerate the preset.
`claude-cli` defaults to no tools, so it cannot open a file path either — a
prompt that names `assets/hero.png` to a tool-less CLI is a reviewer being asked
to rule on a filename. Seating a provider without `supports_images` as the
reviewer of an image ticket is refused at `config.validate()`, not discovered at
review time.

That is the roadmap's deferred **per-role provider guarantees** entry becoming
load-bearing. A role declaring what it needs stops being a nicety the moment one
role needs a capability most of the adapters lack.

**Artifacts must not inline megabytes.** `Artifacts.write` records step detail as
readable text. An `ImagePart` is recorded as a path into the run's artifact
directory plus its digest, never as base64 in the step record.

### What was built, and where it differs from the sketch above

`TextPart`, `ImagePart` and a `Message.content` of `str | list[Part]`, with
`Message.parts`, `.text` and `.images` in `providers/base.py`. A string is read
as one `TextPart`, so every prompt the loop builds produces the request body it
produced before — checked per adapter rather than asserted, because that is the
regression this change was most likely to cause.

`Capabilities.supports_images` defaults to `False` and each adapter declares
it: the Anthropic and Gemini adapters report `True` unless the model block says
`vision: false`, the OpenAI-compatible and llamacpp adapters report `False`
unless it says `multimodal: true` — the same key `presets` already reads to
decide whether to write `mmproj-auto = false` — and `claude-cli` reports
`False` and cannot be talked out of it.

The refusal is `ProviderCannotSee`, raised by `Provider._require_vision` at the
top of every adapter's `complete`, before the request is built. Not retryable:
a checkpoint does not grow a projector between attempts.

Pricing is in `tokens.py`: `(width x height) / 750`, from the dimensions the
part carries, with an unmeasured image charged what a provider's maximum
resize (about 1568px on the longest edge) would cost. The estimate for a
string-content message is unchanged to the token.

Two things the sketch did not mention and the code needed. `ratify`'s prompt
fingerprint hashes each image's digest alongside the text, because two prompts
differing only in the picture they carry are two different questions and
"have we asked this before" would otherwise answer yes to the second. And
`Artifacts.record` reduces any part-like or `bytes` value in a payload to
media type, size and digest on the way to disk, rather than trusting callers
not to hand it bytes — `json.dumps(default=str)` would have written the repr of
the image into the one file whose purpose is being read by eye.

---

## Problem 2 — the `Provider` ABC counts tokens

Do not widen `Completion` to carry bytes. The budget gate reasons in tokens
throughout, and an image call folded into that accounting is a call it treats as
free.

A parallel family, registered the same way:

```python
@dataclass
class Rendering:
    data: bytes
    media_type: str
    width: int
    height: int
    seed: int | None            # what was actually used, not what was asked
    revised_prompt: str = ""    # backends that rewrite the prompt must say so
    usage: ImageUsage = ...

@dataclass
class ImageUsage:
    images: int = 0
    cost_usd: float = 0.0
    estimated: bool = False

@dataclass
class ImageCapabilities:
    sizes: list[tuple[int, int]]
    supports_edit: bool = False
    supports_mask: bool = False
    supports_seed: bool = False
    max_batch: int = 1
    cost_per_image_usd: float = 0.0

class ImageProvider(ABC):
    kind: str = "abstract"

    @abstractmethod
    def generate(self, prompt: str, *, size, seed=None, negative="", timeout) -> Rendering: ...

    def edit(self, prompt, *, image: bytes, mask: bytes | None, strength: float,
             seed=None, timeout=600) -> Rendering:
        raise NotImplementedError   # declared through supports_edit

    @abstractmethod
    def capabilities(self) -> ImageCapabilities: ...

    def health(self) -> str: ...
```

`ImageProvider` shares the normalised error hierarchy with `Provider` —
`RateLimited` carrying `reset_at` is exactly as load-bearing here, and it is the
reason `waiting_budget` works without changes.

**Accounting.** The `usage` table gains one column through the existing additive
migration list in `state.py`:

```python
("usage", "images", "INTEGER NOT NULL DEFAULT 0"),
```

`cost_usd` is already there and is authoritative where the backend reports it.
`tokens_since` is the wrong gate for an image model, so `RateLimitPolicy` gains
an `images_per_window` alongside `tokens_per_window`; a policy that sets neither
is ungated, as now.

**A rendering that costs money must be recorded before it is judged.** The
existing order — call, record usage, then parse — already does this, and it
matters more here: a rejected image has already been paid for.

---

## Problem 3 — verification has no compiler

This is the design risk, and it should be stated at full strength: **a
six-fingered hand passes every mechanical check that can be written.** The
README's stated worry about code is "plausible code that quietly does the wrong
thing". For images that is not the failure mode, it is the ordinary output.

So the mechanical layer gets pushed as far as it goes, because everything it
misses falls on a paid reviewer's opinion. What is genuinely deterministic:

- dimensions, aspect ratio, format, colour mode, bit depth, DPI, file size
- alpha channel present or absent; background fully transparent
- dominant-colour extraction, asserted within ΔE of a brand palette
- occupancy of a safe area, a bleed margin, or a must-stay-empty region
- OCR: contains exactly this string, or contains no glyphs at all
- perceptual-hash distance from a reference — character or style consistency
  across a set
- perceptual-hash distance from *the previous attempt* — proof the generator
  moved at all

None of that is new loop machinery. It is a script that reads a file and exits
non-zero, which is what `commands.test` has always been.

### The tester writes the script

Same contract as everywhere else: **the tester encodes criteria decided
upstream and never authors its own.** For an image ticket it is handed the
mechanical criteria and writes one script:

```python
# tools/check_IMG-001.py — written by the tester, from IMG-001's criteria
from PIL import Image
import json, sys, pathlib

img = Image.open("assets/hero.png")
assert img.size == (2400, 1000), f"expected 2400x1000, got {img.size}"
assert img.mode == "RGB", f"expected no alpha, got mode {img.mode}"
assert pathlib.Path("assets/hero.png").stat().st_size < 900_000, "over 900 KB"
...
```

and `commands.test` gains `".png": "python tools/check_IMG-001.py"`. From the
loop's side nothing is new: `_verify_plan` builds the step, `_shell` runs it,
`failures._blocks` parses the traceback, `_baseline_failures` gives it amnesty
for what was already broken.

A per-ticket check script does not fit `_test_target`'s naming, which derives one
test path from the ticket's scope. An image ticket's check script is a second
writable path the tester owns and the executor cannot touch — the same split as a
bug ticket's reproduction, and enforced the same way.

### No decoding in the daemon

`Pillow`, `imagehash` and `pytesseract` are the check script's dependencies, and
the check script is a shell string the *project* owns. **The daemon never imports
any of them.** That keeps "stdlib-only Python, `pip install -e .` not a
deployment" true, and it is the same reasoning that already lets a verify command
be `docker run`.

The consequence is a rule worth stating on its own, because the obvious place to
put a perceptual-hash stall check is in `loop.py`: **the daemon moves image bytes
and never looks inside them.** It writes them, hashes them with `hashlib`,
base64s them for a provider, and asks a shell command every question that
requires knowing what they depict. Anything the daemon appears to need pixels for
is either a criterion the tester should have asserted or a question for the
reviewer.

---

## Problem 4 — the refinement input is spatial

In code, attempt N+1 rewrites the file from the spec plus the failure text. That
works because the failure text names a line.

An image failure names a *place* — "the hand is wrong" is a mask, not a sentence.
So an image ticket's next attempt has two possible shapes, and the reviewer picks
which:

- **regenerate** — new seed, revised prompt, nothing inherited. Right for a
  composition fault: the subject is in the wrong place, the light is wrong, the
  whole frame is wrong.
- **edit** — the previous attempt goes back in as input, with a region named,
  through `ImageProvider.edit`. Right for a local fault in an otherwise correct
  frame.

Which is the same distinction the bug loop draws when re-diagnosis rules that the
first explanation was wrong, and it wants the same treatment: the choice is
recorded, and a run that alternates between them without converging is a run that
has stalled.

A backend without `supports_edit` collapses this to regenerate-only. That is a
worse loop but a working one, and it must degrade rather than block — the same
way a memory outage degrades a run to "no context".

### What must be recorded per attempt

Prompt as sent, prompt as the backend revised it, model, seed, sampler and
strength, size, and the bytes. `.hybridforge/artifacts/` already exists for
exactly this, and it is the whole reproducibility story for an artifact that has
no source. A rendering whose seed was not reported is recorded as unknown, never
as zero.

---

## Problem 5 — an unverifiable criterion never terminates

"Make it look professional" has no failing state and no passing one. A ticket
carrying it burns `maxAttempts` and then burns every retry cycle.

### The criteria gate

At ingest, every criterion on an image ticket is classified:

- **mechanical** — a check script can assert it. Goes to the tester.
- **visual** — a judge can answer yes or no by looking. "The anvil is left of
  centre." Goes to the reviewer.
- **unverifiable** — neither. "Looks premium", "feels warm", "is beautiful."

One unverifiable criterion blocks the ticket, before any model is called, naming
the criterion and asking for a rewrite. This is `BLOCKED:` never retrying,
applied one step earlier: an underspecified image spec does not improve by being
asked again, and unlike an underspecified code spec it will not fail loudly — it
will produce something, forever.

The planner proposes the classification; a human accepts it. Nothing about what
verification means gets decided by the loop on its own, which is already the rule
`forge toolchain` follows.

### The stall detector

The roadmap lists cross-ticket oscillation detection as deferred. For image
tickets it is not deferrable, because the terminating condition is usually an
opinion.

Two signals, both text, both stdlib:

- **The same criteria fail N attempts running.** The reviewer's verdict is
  per-criterion (below), so the failing set is comparable across attempts the way
  `signatures()` already compares failure signatures across tickets.
- **The mode alternates without the failing set shrinking** — edit, regenerate,
  edit, on the same criterion.

Either one parks the ticket with the attempts it spent and the last image, rather
than spending the rest of the budget. A perceptual "did the image even change"
check is *not* here: it belongs in the check script, per §"No decoding in the
daemon".

---

## Review

The reviewer sees the image and the criteria, and — because there is no compiler
output to parse — its verdict must be structured, not prose:

```
CRITERION 1: PASS
CRITERION 2: PASS
CRITERION 5: FAIL — the anvil sits right of centre and the vice is larger
CRITERION 6: PASS
MODE: edit
REGION: left half, the anvil and the vice
VERDICT: REJECT
```

`parse_verdict` today returns `(ok, text)`. The image variant returns a per-
criterion map, because everything downstream needs it: the stall detector
compares failing sets, the next attempt needs the mode and the region, and
`strip_prompt_echo` needs something with a shape.

**The diff is the pair of images, not `git diff`.** `_diff` on a binary path
yields `Binary files a/… and b/… differ`, which tells a reviewer nothing. For an
image ticket `_sources_for` supplies the previous accepted version and the new
one as `ImagePart`s; on a first attempt, just the new one.
`_written_but_unchanged` must be checked against a binary path before it is
trusted — it reasons about a diff's text.

---

## Invariants, read against images

[LOOP-INVARIANTS.md](LOOP-INVARIANTS.md) is the file this design is most
constrained by. Four of the nine read differently.

**§3 — the party that benefits does not rule.** The generating model may not also
be the reviewing model. In code this is a strong preference; here it is
structural, because the mechanical layer is weak enough that the reviewer is
nearly the whole decision. Enforced at `config.validate()`: an image role
assignment where executor and reviewer resolve to the same model block is a
configuration error, not a warning.

**§4 — "mentions the path" is not "is blamed for the path".** The image analogue:
"the reviewer mentioned the background" is not "the background failed". In code
there are diagnostic blocks to attribute from. Here there is only the verdict,
which is why it must be structured — the per-criterion form *is* the diagnostic
block, and prose must never be scanned for a criterion's text.

**§6 — a prompt is a promise the harness has to keep.** Do not tell a reviewer it
can ask for a region unless `supports_edit` is true for the configured backend
and the region actually reaches `edit()`.

**§7 — recover the content before refusing the format.** The recovery machinery
is text-shaped: `_recover_unlabeled` accepts a reply when it retains 80% of the
top-level lines already on disk. There is no analogue for pixels, and a
similarity threshold is not one. Image tickets skip recovery entirely rather than
getting a version of it that guesses.

**§9 — validate against recorded artifacts.** `forge replay`'s two lenses read
model replies as files and command output as diagnostics. A third lens would read
recorded verdicts against recorded renderings — a parser change on the verdict
format is exactly the class of thing replay exists to catch. Not in v1.

---

## Cost

The roadmap notes review is already ~100% of the money on a hybrid run, one call
per ticket, and lists skipping it for trivial diffs as an idea needing evidence.

An image ticket inverts that in a way worth knowing before running one: review is
one vision call per *attempt*, not per ticket, because every attempt produces
something only a judge can rule on. Plus the renderings themselves, which are
billed per image and are not cheap. A ticket that takes six attempts costs six
generations and six vision reviews.

Which makes the criteria gate the cost control, not an ergonomic nicety. Every
criterion pushed from visual to mechanical is a rejection that costs a shell
command instead of a review.

---

## Decisions to confirm

1. **Which backend.** This gates phase 2 and shapes phase 4. `supports_edit`
   decides whether the refinement loop is the interesting one or degrades to
   regenerate-with-a-better-prompt. Unanswered; the spec is written so that a
   generate-only backend still produces a working loop.
2. **A separate `ImageProvider`, not a widened `Completion`.** Keeps the
   token-based budget gate honest at the cost of a second registry.
3. **The daemon never decodes an image.** Preserves stdlib-only, and forces every
   pixel question into a check script or a reviewer prompt. The cost is that a
   stall detector cannot use perceptual distance directly.
4. **`kind: image`, not a separate command.** `forge bug` earned its own entry
   point because a report arrives before any ticket exists. An image ticket
   arrives as a ticket, so it goes through `ingest`.
5. **One writable image per ticket.** Not a technical limit — it is what makes
   scope enforcement, the check script, and the before/after pair unambiguous. A
   set of five variants is five tickets, or a later revision of this spec.
6. **An unverifiable criterion blocks at ingest.** The alternative is a ticket
   that always produces something and never finishes.

---

## Phases

Each lands on its own, with tests, in this order.

**1 — Multimodal messages — built.** `TextPart`/`ImagePart`, `str` still legal,
`supports_images` on `Capabilities`, image-aware `count_tokens`, all five
adapters, artifact recording by path-and-digest. *Ships useful with no image
generation anywhere: a reviewer can be handed a screenshot on a code ticket.*
Tests in `tests/test_multimodal.py`: every adapter still sends the body it sent
for a string prompt, each wire format decodes back to the original bytes, a
blind model is refused before the request is made, an image-carrying prompt is
priced above zero, and no record holds the bytes.

**2 — `ImageProvider` and accounting.** Base class, one adapter, registry,
`usage.images`, `images_per_window`, `forge doctor` probing it. *Tests: a
rendering records cost before it is judged; a rate-limited render parks the run
and wakes on reset.*

**3 — `kind: image` and the criteria gate.** The kind, the ingest classification,
the block for an unverifiable criterion, the `validate()` refusal when executor
and reviewer share a model, the block for an unknown kind. *Tests: an image
ticket with a prose criterion blocks before any model call; today's unknown-kind
passthrough no longer silently becomes `feature`.*

**4 — Generate, apply, check.** BUILD calling `generate`, APPLY enforcing scope
on bytes, the tester writing the check script, `commands.test[".png"]` running
it. *Tests: bytes outside `allowed_files` are refused exactly as text edits are;
a failing check script fails the attempt with the traceback attributed.*

**5 — Vision review and the edit loop.** Structured per-criterion verdict, the
before/after pair as `ImagePart`s, `MODE`/`REGION` driving `edit` where the
backend supports it. *Tests: a verdict naming a criterion in prose is not parsed
as that criterion failing; a generate-only backend degrades to regenerate.*

**6 — Stall detection.** Repeated failing sets, alternating modes, park with the
last rendering. *Tests: three attempts failing the same criterion park; a
shrinking failing set does not.*

---

## Risks

**The reviewer is the whole loop, and it is the expensive part.** Every risk
below is a version of this one.

**A vision model is a worse judge than a test suite, and confident about it.**
A criterion like "the light reads as coming from the left" is one a model will
answer decisively and inconsistently. Mitigation is the criteria gate pushing
everything it can into the check script, and the stall detector bounding what an
inconsistent judge can spend. Neither makes the judge better.

**The spec has never met a model.** No image ticket has run, so the failure modes
this design guards against are the ones that were predictable from the code.
Phase 1 is deliberately independent of the rest for this reason, and the first
real ticket should be one whose criteria are almost entirely mechanical — a
sprite sheet, an icon at fixed dimensions on a fixed palette — before anything
subjective is attempted.

**Binary artifacts in git.** Attempts live under `.hybridforge/artifacts/` and
never enter the tree; only the accepted rendering is committed, and `autoCommit`
must be checked against that before it is enabled for an image run. A loop that
commits every attempt turns a six-attempt ticket into six megabytes of history
that `git` cannot delta.

**Prompt injection through a reference image.** A reviewer reading an image is a
model reading attacker-controllable pixels, and "ignore your criteria" rendered
as text in a reference asset is a real input. The verdict parser must accept only
its own structured form and treat anything else as a malformed reply, which is
the behaviour `parse_verdict` already has for text.

**Scope creep into a second product.** Image tickets double the surface of every
prompt, role and doc. The line held here: no new step, no new role, no new
command, and no new state. If a phase needs one, that is evidence the generalised
loop was the wrong claim and this belongs somewhere else.
