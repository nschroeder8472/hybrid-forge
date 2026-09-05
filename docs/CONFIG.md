# Configuration reference

Every key `.hybridforge/config.json` understands, what it decides, and what
happens when it is wrong. A populated example lives at
[`templates/config.sample.json`](../templates/config.sample.json) — copy it to
`.hybridforge/config.json` and edit the addresses.

`forge init` writes this file for you and probes each endpoint while you are
still sitting there; `forge init --defaults` writes a single-model local config
to edit by hand. This document is for the edit afterwards.

---

## Where it lives, and what else feeds it

| Path | Holds | Committed |
|---|---|---|
| `.hybridforge/config.json` | everything below | yes, if you like — it carries no secrets |
| `.hybridforge/run.db` | run state, steps, events, usage | no |
| `.hybridforge/tickets/` | the backlog as markdown | yes |
| `$FORGE_PROFILE`, else `%APPDATA%\hybrid-forge\profile.json` or `~/.config/hybrid-forge/profile.json` | machine-level answers reused across repos: `models`, `roles`, `memory`, the UI port | no — outside the repo |

The machine profile exists because endpoints do not change per repository. What
it deliberately does **not** carry is anything the repo decides — `commands`,
`room`, `neverDelegate`. A `cargo test` carried into a Python repo does not fail
loudly; it fails `maxAttempts` times per ticket and parks the whole backlog,
which looks like a model problem and is not.

**No credentials, ever.** Providers resolve keys through `apiKeyEnv` — the
*name* of an environment variable — and the profile strips an inline `apiKey`
on the way in rather than copying it somewhere you did not know existed. An
inline `apiKey` in `config.json` works, but it makes the file unsafe to commit.

Unknown keys are ignored rather than rejected, so a typo is silent. If a setting
seems to do nothing, check its spelling against this page first.

---

## Top level

```json
{
  "room": "",
  "models": {},
  "roles": {},
  "commands": {},
  "workspaces": [],
  "neverDelegate": [],
  "memory": {},
  "loop": {},
  "ui": {}
}
```

Only `models` and `roles` are required. Everything else has working defaults.
`commands` and `workspaces` are two spellings of the same thing — one build or
several — and declaring both is refused.

**`room`** (string, default `""`) — the project's name in whatever memory
server you point at, used when `memory.room` is not set. Empty means memory
calls carry no scope, which on a shared palace means every project reads every
other project's decisions.

---

## `models`

A map of *your* name for a model to how to reach it. The name is what appears
in the dashboard, the usage ledger and every error message, so name it after the
part it plays rather than the weights: `local`, `claude`, `local-plan`.

Declaring a model no role uses is legal and useful — it is how you swap a
reviewer between two backends by editing one line in `roles`.

### `kind` — which adapter

| `kind` | Talks to | Aliases you can also type |
|---|---|---|
| `llamacpp` | **local models.** `llama-server` in **router** mode, swapping checkpoints on demand | `llama.cpp`, `llama-cpp`, `llama-server`, `llama`, `local` |
| `openai` | OpenAI, and gateways that speak its wire | `openai-compatible`, `openrouter`, `litellm`, `together`, `deepseek` |
| `anthropic` | the Anthropic Messages API | `claude` |
| `gemini` | Google's Generative Language API | `google` |
| `claude-cli` | a local `claude` binary in `-p` mode | `claude-code` |

Default `llamacpp`. Every cloud kind needs a credential named alongside it, so
none of them is reached by leaving `kind` out. An unknown kind fails at startup
naming the ones that exist.

**Local means llama.cpp, and only llama.cpp.** `ollama`, `vllm`, `lmstudio`,
`freetoken` and `command` were all backends here once, and a config still naming
one is refused with what to do instead rather than with "unknown kind" — that
error would send you hunting for a spelling mistake that is not there:

```
  model 'plan': Ollama is no longer a forge backend. Point `llama-server` at
  the same GGUF and use `"kind": "llamacpp"` — forge writes the preset for you
  with `forge init`, and gets the context window from the argv the router will
  spawn rather than guessing between /api/ps and /api/show.
```

The narrowing is what lets the errors on this page be specific. Four local
backends meant four ways of asking what was being served and four ways of being
told to load something else, so a diagnostic could only say what all of them had
in common. One means the checks can name a preset, a `--models-max` slot, a
reasoning budget, or the argv a child server was spawned with.

### Keys every kind understands

| Key | Default | What it does |
|---|---|---|
| `model` | `""` | The model id sent to the backend. For `llamacpp` it is the router's id for a checkpoint — the preset's section name. Required by `llamacpp`, `openai`, `anthropic` and `gemini`; optional for `claude-cli` (empty = whatever the CLI defaults to). |
| `contextWindow` | see below | Total prompt+output budget in tokens. The budget gate reserves output from it and refuses — or trims — a prompt that will not fit. |
| `maxOutputTokens` | see below | Ceiling on one reply. Too low is not a silent failure: a truncated planner reply is reported as running out of output room, not as bad JSON. |
| `temperature` | unset | Overrides the temperature the loop asks for. Set it when a model ships a sampling recipe you are meant to follow — several reasoning models degenerate into repetition above their recommended value. A number overrides *every* call; an object `{"default": 0.6, "deterministic": 0.0}` overrides only the ones the loop did not ask determinism for. See below. |
| `tokensPerSecond` | `30` | What this endpoint generates at. Only used to work out how long to wait for a call — see below. The default is a floor, not a guess at your hardware. |
| `timeoutSeconds` | derived | Hard ceiling on one call, in seconds. Overrides the derivation below. `forge doctor` warns when it is too short to generate `maxOutputTokens`. |
| `rateLimit` | none | See [`rateLimit`](#ratelimit) below. |
| `cwd` | the project root | Working directory for adapters that shell out. Filled in automatically; override only to point a role at a different checkout. |

**Context-window defaults differ by kind, and it matters.** `llamacpp` reads
the `--ctx-size` the router will spawn the child server with, which is the real
number rather than an estimate of one; `openai` has nothing to ask and uses
8192; `claude-cli` assumes 200000/32000; `gemini` defaults output to 8192. A
window that collapsed to a default is the failure that reports six 1-3k-token
tickets as "too large for this model" — if you see that, set `contextWindow`
explicitly and run `forge doctor`.

**How long a call is allowed to take is derived from its budget.** It used to
be 600 seconds, hardcoded, with no way to change it — which quietly made a
large `maxOutputTokens` unusable. Generating 65,536 tokens on an endpoint doing
113 tok/s takes 576 seconds; the socket died first, and what got reported was

```
timed out after 600s reaching http://127.0.0.1:1919/v1/chat/completions
```

naming the endpoint, which was answering normally the whole time. It is also
the most expensive way to fail: no response arrives, so the handler that
diagnoses a model reasoning past its budget never runs, and a real cause is
replaced by a false one.

So the timeout now comes from the budget:

```
timeout = max(600, 120 + maxOutputTokens / tokensPerSecond)
```

At the default 30 tok/s a 65,536-token budget is allowed 2,304 seconds. That is
deliberately generous — 30 tok/s is a floor for slow local hardware, so the
derived timeout is loose on a fast box and still correct on a slow one, and the
budget you configured is always reachable. Set `tokensPerSecond` to what your
endpoint actually does to tighten it: at 113.8 the same budget gets 695
seconds. If you do not know the figure, leave it — the default only ever makes
the timeout more generous, never shorter than the call needs.

Prefer `tokensPerSecond` over `timeoutSeconds`. An absolute ceiling has to be
re-tuned by hand every time the budget moves, and forgetting to is exactly how
the budget stops being reachable again. Use `timeoutSeconds` when you would
genuinely rather abandon a call than wait for it.

**A configured temperature need not override determinism.** The loop does not
ask for one temperature. It asks **0.0** where it needs the same answer twice —
the sign-off votes, the verdict parse, the respec — and 0.1 or 0.2 where it
wants the model to reach. A scalar `temperature` overrides both, so following a
vendor's sampling recipe silently costs reproducible ratification. Measured: one
nine-ticket backlog run twice under identical configuration, two tickets
swapping verdicts, because every ratify vote ran at 0.6 where the loop had asked
for 0.

An object splits the two:

```json
"plan": { "temperature": { "default": 0.6, "deterministic": 0.0 } }
```

`deterministic` applies when the loop asked for exactly zero, `default` to
everything else. Either key may be omitted and an omitted one leaves the loop's
own number alone, which makes `{"default": 0.6}` the honest spelling of *follow
the recipe, but let determinism through*. A bare number still overrides
everything, so nothing that worked before changes.

### `kind: "openai"`

| Key | Default | Notes |
|---|---|---|
This is the **cloud** adapter: OpenAI itself, and the gateways that speak its
wire. It does not reach a local server any more — a GGUF belongs to
[`llamacpp`](#kind-llamacpp).

| Key | Default | Notes |
|---|---|---|
| `baseUrl` | `https://api.openai.com/v1` | Include the `/v1`. |
| `apiKeyEnv` | unset | Name of the env var holding the key. Preferred. |
| `apiKey` | `""` | Inline key. Works; makes the file unsafe to commit. |
| `headers` | `{}` | Extra request headers, merged over the `Authorization` header. |
| `extraBody` | `{}` | Fields merged into the request body, for a gateway's own knobs (OpenRouter routing preferences and the like). |
| `supportsTemperature` | `true` | Set false for endpoints that reject the field outright. |
| `multimodal` | `false` | Whether the model behind this endpoint can be shown an image. Off by default: "OpenAI-compatible" describes the request shape, not the model, and nothing here can ask. A prompt carrying an image to a model without it is refused before the request. |
| `topP`, `topK`, `minP`, `presencePenalty`, `frequencyPenalty` | unset | Sent only when set, so a model's own shipped recipe still applies. |

`contextWindow` is not discovered here and cannot be: a hosted endpoint does not
publish the window it will serve. Left unset it is 8192 and `forge doctor` says
so — set it from the model's documented window.

### `kind: "llamacpp"`

For `llama-server` started in **router** mode — `--models-dir` or
`--models-preset`. The router holds a catalogue, spawns a child server per
model on an ephemeral port, and proxies each request to whichever model it
names. One endpoint, several checkpoints, loaded on demand.

A single-model `llama-server` is `kind: "openai"` and always was. Use this one
only when the server can swap; pointed at a single-model server it says so
rather than failing later.

| Key | Default | Notes |
|---|---|---|
| `baseUrl` | `http://127.0.0.1:8080/v1` | Include the `/v1`. The router's `/models/load`, `/models/unload` and `/props` sit one level up. |
| `model` | `""` | The router's **id** for the checkpoint, which is not a path. See below. |
| `loadSeconds` | `300` | How long to wait for a checkpoint to become servable. |
| `exclusive` | `false` | Unload every other resident checkpoint before loading this one. |

Everything under [`kind: "openai"`](#kind-openai) applies too — `extraBody`,
the sampling knobs, `headers` — and unlike a hosted endpoint, `topK` and `minP`
genuinely reach the model.

**Keys `forge models` writes into the preset.** These configure the *server*
rather than a request, so they cannot be sent per call — they decide how the
child server is spawned. Setting them here means `forge models` can generate the
preset instead of you maintaining it beside `config.json` and keeping the
numbers in step by hand.

| Key | Preset flag | Notes |
|---|---|---|
| `modelPath` | `model` | The `.gguf` on disk. Without it the model is left out of the generated preset — the file is not derivable from an id, and a section pointing at the wrong one fails at load with a message about the file rather than about the config that named it. |
| `contextWindow` | `ctx-size` | The same number the budget gate plans against. Writing both from one source is the point. |
| `reasoningBudget` | `reasoning-budget` | Tokens a thinking model may spend before it must begin answering. Unset, a 30B MoE measured here burned all 32,768 of its output budget reasoning and never started, on every call. |
| `reasoningEffort` | `reasoning-effort` | The gear, where the checkpoint has them. |
| `gpuLayers` | `n-gpu-layers` | |
| `flashAttention` | `flash-attn` | |
| `cacheTypeK`, `cacheTypeV` | `cache-type-k`, `cache-type-v` | KV quantization. `q8_0` on both roughly halves the cache, which is most of the VRAM at a large `ctx-size`. |
| `parallel` | `parallel` | |
| `mainGpu`, `splitMode` | `main-gpu`, `split-mode` | |
| `multimodal` | `mmproj-auto` | Default false. A projector beside the `.gguf` is loaded automatically and costs VRAM no text-only role will use. It is also what tells the loop this checkpoint can be shown an image: seeing is a property of the checkpoint, not of the adapter, and a prompt carrying one to a model without it is refused before the request. `forge doctor` reports both halves of the contradiction — a projector loaded for a role that does not want one, and a role that says `multimodal: true` whose child server was started without one. |
| `presetFlags` | — | An object merged in last, for any flag not listed above. |

Two roles naming the same `model` are one child server and collapse into one
section, which is the ordinary case rather than a mistake.

**The id is the directory, not the file.** A `--models-dir` entry is named
after the directory holding the `.gguf`, so a checkpoint at
`C:\AIModels\nemotron-3-nano-omni-30b-a3b-reasoning-gguf\…Q4_K_M.gguf` is
called `nemotron-3-nano-omni-30b-a3b-reasoning-gguf` and nothing else. The
router answers `400 model 'x' not found` for anything else, which is a good
failure — `forge doctor` turns it into a better one by listing what the router
actually serves:

```
  plan: FAIL name=plan kind=llamacpp model=qwen3.8 error=ProviderError: the
    llama.cpp router at http://127.0.0.1:8080 has no model 'qwen3.8'; it serves
    nemotron-3-nano-omni-30b-a3b-reasoning-gguf.
```

That refusal is also why this adapter is small, and part of why it is the only
local one. The backend it replaced answered to *any* model name and echoed it
back, so a config naming three checkpoints got one checkpoint and three labels —
and every artifact, every usage row and every cost figure attributed work to a
model that had not written it. The router routes by id and 400s an id it does
not have, so forge's record of which model wrote what is true for free.

**A load is not instant and is not the call's fault.** With
`--models-autoload` the first request after a swap simply blocks while a 30B
checkpoint loads, so a load that never finishes is reported as the *completion*
timing out — naming an endpoint that was healthy throughout. So the load is
asked for explicitly, waited for against `loadSeconds`, and a checkpoint still
not `loaded` at the deadline says which one and for how long. A child that dies
instead of binding reverts to `unloaded` with no reason published, and is
reported as a dead child rather than polled until the deadline.

**`exclusive` — one checkpoint at a time.** `--models-max` defaults to 4, which
is right on a box with the VRAM for it and fatal on one without: the router
keeps the previous role's checkpoint resident and the next role's load fails
with

```
ggml_vulkan: vk::Device::allocateMemory: ErrorOutOfDeviceMemory
```

from a child process, in the router's log, while `/v1/models` shows only that
the model went back to `unloaded`. `exclusive` unloads everything else first,
trading a reload per role alternation — measured at 10-20s for a 30B A3B MoE at
Q4_K_M — for a ceiling of one checkpoint. Starting the router with
`--models-max 1` does the same thing globally; `forge doctor` reports the
residency either way.

The eviction is waited out before the next load is asked for, because
`/models/unload` answers when the child server has been *asked* to exit rather
than when it has, and the slot is only free once it has. A load that lands in
that window is refused with

```
500 {"error":{"code":500,"message":"model limit reached, try again later"}}
```

which reaches the loop as a model that cannot be talked to: roles drop out of
sign-off and delegation attempts are spent without a model ever being asked
anything. Forge waits for the catalogue to show the evicted checkpoint gone,
then retries a refusal that arrives anyway for up to 60s. Nothing to configure;
the only case that survives it is a slot genuinely held by another client, and
that is named in the error.

**The context window comes from the preset, and it has to.** The router's own
`/props` answers `n_ctx: 0` — it holds no model — and a child's port is
ephemeral, so the argv the catalogue publishes is the only place a per-model
window is visible without loading it. `contextWindow` in config wins; absent
one, the preset's `-c` is read; absent both it falls back to 8192 rather than
reading the trained maximum out of the GGUF, because that number describes the
model and not the window the server allocated. Believing it is how the budget
gate approves a prompt the server then truncates *from the front*, dropping the
system prompt and the spec — and what comes back reads as a weak model rather
than a truncated request. `forge doctor` reports the mismatch:

```
  plan: ok name=plan kind=llamacpp model=nemo-a reply='OK'
      contextWindow is 131,072 but the router starts 'nemo-a' with -c 32,768.
```

**Set `tokensPerSecond`.** A 30B A3B MoE at Q4_K_M measured 16 tok/s on a
consumer GPU. At the 30 tok/s default the derived timeout is `120 + 32768/30 =
1,212s` for a budget that actually needs about 2,150s, so a large
`maxOutputTokens` is never reachable and the failure arrives wearing the name
of a network fault.

A preset file is worth writing rather than relying on `--models-dir`, because a
generated entry pins no `-c` and does load a multimodal projector beside a
text-only checkpoint — VRAM no role here uses, on a card that may be sized for
exactly one model. There are two ways to acquire one and only the first is
visible: a models-dir entry beside an `mmproj-*.gguf` is spawned with an
explicit `--mmproj`, while an `hf-repo` entry resolves one *inside* the child
whenever the repo publishes it, so the argv says nothing and the only trace is
a line in the router's log. `no-mmproj = true` settles both, and `forge doctor`
reports either.

The format is INI, one section per id, keys spelled like the long flags without
their dashes:

```ini
[nemotron-3-nano]
model = C:\AIModels\nemotron-3-nano-omni-30b-a3b-reasoning-gguf\Nemotron-3-Nano-Omni-30B-A3B-Reasoning-Q4_K_M.gguf
ctx-size = 131072
jinja = true
no-mmproj = true

[qwen3.8]
hf-repo = unsloth/Qwen3.8-27B-GGUF:Q4_K_M
ctx-size = 131072
jinja = true
no-mmproj = true
```

```
llama-server --models-preset models.ini --models-max 1 --host 127.0.0.1 --port 8080
```

```json
"plan": {
  "kind": "llamacpp",
  "model": "nemotron-3-nano",
  "contextWindow": 131072,
  "maxOutputTokens": 32768,
  "tokensPerSecond": 16,
  "exclusive": true
},
"code": {
  "kind": "llamacpp",
  "model": "qwen3.8",
  "contextWindow": 131072,
  "maxOutputTokens": 32768,
  "tokensPerSecond": 16,
  "exclusive": true
}
```

Two entries, one endpoint, one GPU: the router swaps between them as the loop
alternates roles.

### `kind: "anthropic"`

| Key | Default |
|---|---|
| `baseUrl` | `https://api.anthropic.com` |
| `apiKeyEnv` | `ANTHROPIC_API_KEY` |
| `apiKey` | `""` |
| `model` | `claude-opus-5` |
| `effort` | unset — sent as the reply's effort setting when present |
| `vision` | `true` — every Claude model here takes images; set false to refuse them instead |

### `kind: "gemini"`

| Key | Default |
|---|---|
| `baseUrl` | `https://generativelanguage.googleapis.com/v1beta` |
| `apiKeyEnv` | `GEMINI_API_KEY` |
| `apiKey` | `""` |
| `maxOutputTokens` | `8192` |
| `vision` | `true` — as on the Anthropic adapter |

### `kind: "claude-cli"`

Runs the `claude` binary you already have logged in, so it bills against that
subscription rather than an API key.

| Key | Default | Notes |
|---|---|---|
| `binary` | `claude` | Path to the executable. |
| `model` | `""` | e.g. `opus`, `sonnet`. Empty uses the CLI's own default. |
| `extraArgs` | `[]` | Appended to the invocation. |
| `tools` | `""` | **Empty means no tools**, which is what makes this adapter behave like the completion endpoint the loop assumes. Set `"default"` to get the agent back, or name the tools it may use. |
| `allowAllTools` | `false` | Skips permission prompts. Required for genuinely unattended runs, and a real grant of authority — hence opt-in. |
| `contextWindow` / `maxOutputTokens` | `200000` / `32000` | |

This adapter cannot be shown an image and has no key for it. The prompt reaches
the CLI as text on stdin, and naming a file path instead would not help: with
no tools it cannot open one, so the reviewer would be ruling on a filename.

`tools` deserves the warning it gets. `claude -p` with tools is an agent, not a
completion call: it reads files on its own and bills for every turn. One
measured reviewer call spent 208k cache-read tokens and $0.34 judging a diff the
loop had already pasted into its prompt. It also voids a guarantee — the loop
decides what each role may see and write, and a role with tools reads and writes
whatever it likes.

### `rateLimit`

Per model. The gate parks the run when a window is spent and resumes on its own
— a limited planner parks exactly like a limited executor, and the dashboard
says which window it is waiting on.

| Key | Default | Meaning |
|---|---|---|
| `requestsPerMinute` | `0` (off) | |
| `tokensPerMinute` | `0` (off) | |
| `tokensPerWindow` | `0` (off) | Needs `windowSeconds` to do anything. |
| `costPerWindow` | `0` (off) | In dollars. Needs `windowSeconds`. |
| `windowSeconds` | `0` | e.g. `18000` for a five-hour window. |

Token windows count cache reads. A cache-heavy call reports almost nothing as
`prompt_tokens`, so a gate summing only that would never fire on the traffic
actually consuming the allowance.

---

## `roles`

```json
"roles": {
  "planner": "local-plan",
  "executor": "local",
  "tester": "local",
  "reviewer": "claude"
}
```

All four are required and each must name a declared model; either mistake fails
at startup rather than mid-run. This map is the whole "hybrid" idea — the loop
asks for *the reviewer*, config decides who that is.

| Role | Does | Wants |
|---|---|---|
| `planner` | turns a freeform spec into tickets, and rewrites a failed ticket from its evidence (`respec`) | judgment and a long output budget — it emits a whole backlog in one reply |
| `executor` | implements one ticket, returning whole files as text | throughput; it is the call you make hundreds of times |
| `tester` | writes the assertions for a ticket's criteria | same model as the executor is fine — it never judges its own work |
| `reviewer` | accepts or rejects the diff against the spec | the strongest model you are willing to pay for; it is what keeps a cheap executor honest |

Review is roughly one call per ticket and close to all of the money on a hybrid
run. That is the trade being made deliberately.

---

## `commands`

```json
"commands": {
  "lint": "cargo clippy --all-targets -- -D warnings",
  "typecheck": "cargo check --all-targets",
  "test": "cargo test",
  "format": "rustfmt"
}
```

`lint`, `typecheck` and `test` are shell commands run from the project root
after each attempt. An empty string skips that check.

`format` is different in three ways, and each of them matters:

- **It runs before verification, not after** — between the tester writing its
  file and anything judging either file.
- **It rewrites files instead of reporting on them**, and the loop appends the
  paths to rewrite. Give the command *without* a target: `gdformat`,
  `prettier --write`, `ruff format`, `rustfmt`, `gofmt -w`, `black`. A command
  that ignores its arguments and reformats the whole tree is an out-of-scope
  edit on every attempt.
- **Its own failure is never the ticket's.** A missing binary is logged and
  skipped, and the attempt is judged exactly as it would have been with no
  formatter configured.

It runs only over the files that attempt landed — the executor's and the
tester's — never the ticket's whole glob, and never a bug ticket's
reproduction, which is the standard the fix is measured against.

This lowers no bar. The linter is the project's and its thresholds are
untouched; what changes is that the code is made to meet them before it is
judged. One run spent 117 of a ticket's 160 lint failures on trailing
whitespace in a file the tester had just written, each costing a full attempt.
See [CONVERGENCE](CONVERGENCE.md).

**`format` may be a list**, run in order over the same files. Most linters fix
a good deal of what they report, and what a fixer settles never reaches a model
as a failure at all — but a fixer and a formatter are not substitutes for each
other. One removes the unused import and leaves the blank line it was on; the
other tidies the blank line and cannot remove the import.

```json
"commands": {
  "format": ["ruff check --fix", "ruff format"]
}
```

They cannot be joined into one string with `&&`. The files the attempt wrote
are appended to the command, so only the last would receive them and the first
would run over the whole tree — reformatting files the ticket never touched,
which is the out-of-scope edit this step exists to avoid. A plain string still
means one command, and the list form works inside the per-language map too.

A later command runs whatever the one before it reported. `eslint --fix` and
`ruff check --fix` both exit non-zero when they found something, fixed or not,
and stopping there would leave the file worse than either tool alone.

**Keep fixers to their safe modes.** A formatter is semantics-preserving by
definition, which is what makes "the code is made to meet the bar before it is
judged" an honest claim. A fixer is not always: `ruff check --fix` applies only
safe fixes unless `--unsafe-fixes` is passed, and most of `eslint --fix` is
safe, while `cargo clippy --fix` will rewrite logic. Turning those on means the
harness edits behaviour the model wrote, and the acceptance criteria then judge
something nobody authored. That is a different act from tidying whitespace, and
this step does not distinguish them for you.

**Each one may also be a map from language to command**, because a repository is
rarely one language and a single command silently means "everything is Rust":

```json
"commands": {
  "test": { ".rs": "cargo test", ".js": "node --test web/" },
  "lint": { ".rs": "cargo clippy --all-targets -- -D warnings", ".js": "eslint web/" }
}
```

Keys are extensions (`.rs`) or language names (`rust`, `javascript` — which
expands to `.js`, `.mjs`, `.cjs`, `.jsx`), and `*` is a catch-all. A plain
string is read as `{"*": "..."}`, so every existing config keeps its meaning.

A language with nothing worth testing is declared rather than left blank —
`false` (or `"skip"`) says so, and the loop stops asking:

```json
"test": { ".rs": "cargo test", ".sh": false, ".ps1": false }
```

That is the difference between a decision and an oversight, which is the whole
point of the gate: a build script nobody unit-tests is fine, a language nobody
noticed is not. Work in a declared language is checked at review, and the run
says so at the end.

`forge toolchain` sets one up for a language that has none — reading the repo's
own CI and build files to propose a command, and writing it only when you
accept:

```bash
forge toolchain                                    # the coverage matrix
forge toolchain --language .js                     # propose one, write nothing
forge toolchain --language .js --accept            # write what it proposed
forge toolchain --language .js --set "node --test web/"
forge toolchain --language .sh --skip                 # nothing runs it, on purpose
```

A catch-all counts as coverage *until it names a runner that cannot run the
language*: `"test": "cargo test"` does not cover a project's JavaScript, and
`forge doctor` prints the matrix so the gap is visible before it becomes a
ticket checked by reading. A command keyed to a language it demonstrably cannot
run — `{".js": "cargo test"}` — is refused at startup, because a ticket failing
that way reports it as the ticket's fault. They are the loop's only source of ground truth about
whether the work is good — a run with all three empty is verified by review
alone.

A missing `typecheck` is reported, not gated — and only for the languages whose
test command does not already compile them. `cargo test` and `go test` do, so
nothing is said about Rust or Go. `npm test` and `pytest` load the modules their
tests reach and nothing else, so for TypeScript and Python a missing entry is a
hole the size of every file no test imports:

```
  no type check:
    .ts  —  try `tsc --noEmit`
```

`forge doctor` prints that, and the run log says it once at the start. Close it
with `forge toolchain --kind typecheck --language .ts`, or say it needs none
with `--skip`.

`test` does double duty: its text decides which language the tester writes in,
so `cargo test` gets `.rs` files and `pytest` gets `.py` ones. A ticket writing
files the test command cannot collect authors no tests and is checked at review
instead, and the run says so at the end.

---

## `workspaces`

Optional. A repository with one build never needs it — `commands` above is read
as a single workspace at the repository root, and every run behaves exactly as
it did before this key existed.

Declare it when the repository contains a **second build**: a directory with its
own manifest, its own dependency tree, and commands that only work when run from
inside it.

```json
"workspaces": [
  {
    "root": ".",
    "commands": { "test": "godot --headless --import && runtest.cmd -a tests/" },
    "excludes": ["tools/**"]
  },
  {
    "root": "tools/path-forge",
    "commands": { "lint": "npm run lint", "typecheck": "tsc --noEmit", "test": "npm test" }
  }
]
```

| Key | Default | Meaning |
|---|---|---|
| `root` | `"."` | Repo-relative directory this build owns. Must exist. |
| `commands` | `{}` | Exactly the `commands` block documented above — string, language map, or `false` exemptions — scoped to this build. |
| `excludes` | `[]` | Glob patterns this workspace does *not* own despite their sitting beneath it. A child workspace's root is excluded implicitly. |

Three rules follow from it:

1. **A file belongs to the workspace with the longest matching root.** `.`
   contains a subproject's files too; ownership is the deepest claim, not the
   first one.
2. **Verify runs each build's commands with `cwd` set to that build's root** —
   which is what `npm test` and `cargo test` need and what a repository-root
   command cannot give them. A ticket is verified by the build its writable
   files belong to; the sweeps between tickets and at the end of the run still
   check every build, so cross-build breakage is caught.
3. **A file no workspace owns is verified by nothing, and the loop says so
   rather than guessing.** This is the reason the key exists. Without it, a
   subproject's files are absorbed by whatever catch-all is configured at the
   root, and absorption reads as coverage everywhere downstream: one repository
   had its Godot test launcher report itself as the test command for 4,000 lines
   of TypeScript it could not see, and fifteen tickets finished green over code
   that had never been compiled.

`commands` and `workspaces` are two spellings of the same thing, so declaring
both is refused — the top-level block would be read by nothing while looking
configured. Move it into the workspace whose root is `.`.

A root that does not resolve is refused at load for the same reason. It owns no
files, so every file falls through to whichever workspace does match, and the
config looks entirely reasonable while a whole build goes unverified.

Step names carry the build only when there is more than one: a single-workspace
project logs `test`, and a two-workspace one logs `test` and
`test[path-forge]`.

`forge init` proposes the list. It walks the tree for the files that mark a
build — `package.json`, `Cargo.toml`, `pyproject.toml`, `go.mod`,
`project.godot`, and the rest — skipping generated directories, and asks only
when it finds more than one. A repository with a single manifest at its root is
the ordinary case and is never asked. Saying no keeps one set of commands for
the whole repository, which is a real answer: some repositories genuinely carry
a second manifest they do not verify separately.

`forge toolchain` sets one build's command up:

```bash
forge toolchain --workspace tools/path-forge --language .ts --set "npm test"
forge toolchain --workspace tools/path-forge --language .ts          # propose
forge toolchain --workspace tools/path-forge --language .ts --accept # write it
```

`--workspace` is required once a repository declares more than one build, and
refused if the root does not name one. Writing a command into the wrong build
is worse than writing none: it reports as coverage for files that command
cannot see, which is the failure this whole key exists to remove. Detection
reads the build's own CI, manifest and docs, not the repository root's.

---

## `neverDelegate`

```json
"neverDelegate": ["src/auth/**", "migrations/**"]
```

Glob patterns the executor may never write, enforced before anything reaches
disk — not by asking the model nicely. A ticket whose allowed files match one of
these is blocked outright, naming the pattern, rather than half-implemented.

Worth listing: auth, secrets, migrations, CI workflows, anything whose failure
mode is silent.

---

## `memory`

Optional. Without it the executor sees only what a ticket's own `context`
carries, which for an `forge ingest`-seeded run is usually nothing.

| Key | Default | Meaning |
|---|---|---|
| `url` | `""` | HTTP MCP endpoint. |
| `command` | `[]` | argv for an MCP server over stdio, e.g. `["mempalace-mcp"]`. Either this or `url`; `command` implies stdio. |
| `enabled` | `true` | Off without deleting the block. |
| `room` | falls back to top-level `room` | Scope for reads and writes. |
| `arguments` / `writeArguments` | `{}` | Extra tool arguments. Reads and writes want different scopes: a search spans the project, a recorded decision belongs to one part of it. |
| `searchTool` / `writeTool` | discovered | Override when the server's tool names are not guessable. |
| `token` / `tokenEnv` | `""` | Bearer token; prefer `tokenEnv`. |
| `headers` | `{}` | Extra HTTP headers. |
| `limit` | `6` | Entries retrieved per ticket. |
| `maxTokens` | `1200` | Ceiling on the retrieved block. It is the first thing the budget gate drops. |
| `timeout` | `30` | Seconds. |
| `write` | `false` | Whether the loop records decisions back. Off by default — a wrong entry is replayed into every later prompt. |
| `dryRun` | `false` | Log what would be written without sending it. The honest way to see what the recorder thinks is durable. |
| `maxWriteChars` | `2000` | Hard cap per entry. Memory is for decisions, not transcripts. |
| `recordRole` | `reviewer` | Which role decides what is worth keeping. The reviewer has just read the diff against the spec, so it can tell a decision from narration. |

Memory is best-effort everywhere: a server that is down, slow or wrong costs the
run some context, never the run.

---

## `loop`

| Key | Default | What it changes |
|---|---|---|
| `maxAttempts` | `5` | Rework attempts per ticket before it parks as blocked. Three absorbs a lint error and a shallow test failure; five also absorbs the one a local executor actually produces, which is a correct implementation arriving in a shape the parser cannot read — an attempt spent teaching the ticket nothing. Past five the failure is a spec problem no amount of retrying fixes, and `retryCycles` with a respec between cycles is the tool for that rather than more attempts against the same words. |
| `autoCommit` | `false` | Commit each verified ticket. Off so the first unattended runs leave their work in the tree for you to read. |
| `stopOnBlocked` | `false` | Stop the whole run when a ticket blocks, instead of moving on. On means a blocker gets attention; off means the backlog keeps making progress elsewhere. |
| `retryCycles` | `-1` | Whole-backlog retry cycles after a run ends anything but done. `-1` keeps going until the backlog is clean or you stop it; `0` hands back to a human after the first pass. Anything below `-1` is a typo and is rejected.<br><br>`-1` is the default only because a cycle can now be *measured* rather than counted: `flatCycles` ends the retries when a cycle fails in exactly the way the one before it did, so an unattended run converges or stops on its own. Both have been observed — one backlog stopped itself after a single repeated cycle, and the next landed a ticket on the cycle after the one that gave up on it. **If you turn `flatCycles` off, set this back to `0` or a small number in the same edit**: without the detector this is the 18-hour run in [CONVERGENCE](CONVERGENCE.md). |
| `respecOnRetry` | `true` | Have the planner rewrite each requeued ticket from why it failed before the next cycle. A cycle that re-runs the spec which already failed is a slower version of the same failure. |
| `respecCriteria` | `false` | Let a respec rewrite the acceptance criteria too, and let ratification *add* them. Off: the party being judged does not write the standard it is judged against. Left on, one ticket's criteria drifted until they asserted the opposite of what its author wrote — and a ratify pass grew a ten-criterion ticket to fourteen, inventing a hash value it had no way to compute. |
| `reopenStaleDependents` | `true` | Re-open a ticket that passed on top of a dependency a respec has since rewritten — its `done` was earned against a contract that no longer exists. Can re-open a lot of a backlog after one respec; turn it off to be warned instead. |
| `preflight` | `true` | Probe every model before the first ticket, so a dead endpoint fails in seconds rather than one ticket at a time. |
| `preflightCanary` | `true` | Prove, rather than infer, that each build's test command actually reads each language **this backlog is about to write**. Writes an unparseable file where that language's tests live, runs the command over it, and requires the command to go red **and** to name the file — then deletes it. Coverage used to be read off the *text* of a command against a table of known runners, and a runner the table has never heard of answers "covered" for everything: one gdUnit4 launcher reported itself as the test command for 4,000 lines of TypeScript and exited 0 fifteen times. A build that fails this blocks the run before anything is delegated. Scoped to the languages the tickets declare they will write, not to every language in the tree — a Godot repository with one Python helper script beside its `project.godot` has `.py` present, nothing that runs it, and no ticket that cares. Costs one command per language per build, once per run. Turn off for a suite slow enough that paying it at startup is worse than finding out later; `forge toolchain --language X --skip` is the narrower way to excuse one language, and says so on the record. |
| `requireGreenBaseline` | `true` | Run the verify commands once before the first ticket and refuse to start on a tree that is already red. A failure that pre-dates the run is excused for *every* ticket in it, so the backlog would finish reporting green having compiled nothing — and `_unverifiable` cannot catch it, because red in files no ticket owns has no exhausted owner to point at. Only a failure naming files is gated on: `pytest` exiting 5 on a project with no tests yet is a greenfield run, not a broken one, and is reported instead. Turn it off, or pass `--allow-red-baseline`, for a repository whose red is what the backlog is there to fix. |
| `quarantineFailed` | `true` | When a ticket gives up, restore the files it wrote to the state it inherited and keep a copy under `.hybridforge/abandoned/run-N/<ticket>/`. Verification is whole-project, so an abandoned file that does not compile is reported to every later ticket — and because it is outside their scope they are excused for it and pass having had nothing compiled. Quarantine keeps the salvage without the poison. Turn it off, or pass `--no-quarantine`, to have a failed ticket's work left in the tree. |
| `pollSeconds` | `2.0` | Control-channel poll interval while waiting. |
| `maxRuntimeSeconds` | `0` (off) | Cap on unattended wall-clock time. Covers the whole `forge go`, not each run in its queue — one `go` can drain several runs, and a fresh clock per run would quietly multiply the cap. |
| `baselineVerify` | `true` | Run the verify commands once before each ticket, so breakage that was already there is not blamed on whichever ticket ran next. Turn off only when a full suite is slow enough that paying it per ticket costs more than the attempts it saves. |
| `bugHypotheses` | `3` | How many explanations a `forge bug` ticket may go through before it parks. The first is the planner's reading of the report; each one after it is a re-diagnosis, asked for when the reproduction could not be written — a test that passes against the named code has *disproved* that reading, and disproof is evidence rather than a dead end. `1` parks on the first wrong guess. See [BUG-LOOP.md](BUG-LOOP.md). |
| `executorTurns` | `4` | Replay this many prior attempts to the executor as real conversation turns — its own reply as an `assistant` message, the failure that followed as the next `user` one. `0` restores the flat single-message prompt, in which the executor reads its own previous work as somebody else's. See [SETUP](SETUP.md#thinking-models-answer-last) for the trade and [CONVERGENCE](CONVERGENCE.md) for what the flat shape cost on a long backlog. |
| `innerTurns` | `3` | How many times a compile failure may go straight back to the executor without spending an attempt. Between apply and the tests step the loop runs the `lint` and `typecheck` commands it was going to run at verify anyway; a failure there can only be the executor's own, so the reply goes back on the same conversation thread against the same contract, and the tester is not asked to write assertions for something that does not compile. Never `test` — a red suite may be the tester's assertion rather than the executor's code, and the executor cannot edit that file to find out. A turn is spent only while the error count is falling; when it stops, the attempt is charged and the ordinary path resumes. It shipped off because it changes how attempts are counted, and every convergence rule in the loop is written against that number; the measurement is what moved it. On one ticket `typecheck` averaged 0.7s against the tester's 12.0s, and 58 of its 95 cycles wrote a test file for an implementation that then failed to compile. Set `0` to restore the older accounting. See [CONVERGENCE](CONVERGENCE.md). |
| `toolchainContext` | `true` | Show the executor and tester the linter, compiler and test-runner settings that grade what they write — the real files at their real paths, resolved per language from the ticket's own scope and clipped. They are measured by `commands.lint` and `commands.typecheck` and were otherwise never shown what those enforce, so they inferred it from failures. Costs a few hundred characters a call, and the budget gate drops it first. See [CONVERGENCE](CONVERGENCE.md). |
| `readTools` | `true` | Give every role whose provider can take them the read-only tools in `forge/tools.py` — `grep`, `read_file`, `list_dir`, `outline` — so it assembles its own context instead of being handed a scope guessed before the ticket was read. A provider that cannot encode a tool call is not refused: that role falls back to the pasted-sources prompt. On, because the alternative was measured — run 1 of `HANDBACK-DASHBOARD.md` pasted 156k characters of a test suite the ticket never mentioned, omitted the one file its spec named, and ended blocked after nine attempts having written nothing. See [CONTEXT-TOOLS](CONTEXT-TOOLS.md). |
| `toolTurns` | `8` | Tool calls one role may make before it has to answer. The last turn is taken with the tools withdrawn and an instruction to answer from what it has, rather than by cutting the conversation off: a model that has read for eight turns without answering will not answer on the ninth, and an attempt that ends with no reply at all is worse than one built on a partial read. See [CONTEXT-TOOLS](CONTEXT-TOOLS.md). |
| `repoMap` | `true` | Carry a map of the repository — every source file and what it declares, without the bodies — in the prompt's stable prefix. Roughly 9k tokens on this repository against the 470k a full paste would be. It is what makes a read tool cheap: a role that can see where a name is defined reads one file instead of grepping four times to find it. It is identical for every ticket in a run, so a provider that caches a prefix charges for it once. See [CONTEXT-TOOLS](CONTEXT-TOOLS.md). |
| `priorFailures` | `8` | Earlier failures carried into the executor's prompt alongside the newest one, deduplicated by failure *class* — `(step, error code, file)`, line numbers and quoted values masked — rather than by raw text. Keyed by text this window held one mistake repeated, not several distinct ones. See [CONVERGENCE](CONVERGENCE.md). |
| `learnedLimit` | `12` | How many of a ticket's accumulated learnings reach a prompt, commonest first. A learning is a fact about *this repository* that an earlier attempt established — a compiler flag, an import convention — recorded by respec so the loop stops rediscovering it. It is not a bar: the reviewer is not shown it and no criterion is made from it. `0` renders none. See [CONVERGENCE](CONVERGENCE.md). |
| `flatCycles` | `0` (off) | Consecutive cycles a ticket may fail on exactly the same set of failure *classes* before it is parked and the rest of the backlog carries on. Per ticket, unlike the backlog-wide brake beside it, which cannot see a ticket going nowhere while any other ticket still moves. Off because the threshold was measured and none is safe: on the run this comes from, a ticket that went on to pass sat still for four consecutive cycles while the genuinely unsatisfiable one managed three. The measurement runs either way and logs what it found — `descending`, `churning`, `flat` — which is what the escalation rungs will read. See [CONVERGENCE](CONVERGENCE.md). |
| `reviewWhenStuck` | `2` | Consecutive flat cycles before the loop escalates. Two rungs, one per cycle after it: at `n` the reviewer is asked against the red tree whether the ticket is winnable at all — normally unreachable for a ticket that never verifies — and at `n + 1` the planner is asked the inverted question and may reply `impossible`, which parks the ticket for a reason rather than for a count. `0` never escalates. See [CONVERGENCE](CONVERGENCE.md). |
| `freezeTests` | `true` | Keep a ticket's tests while the criteria they encode are unchanged, instead of re-deriving them on every attempt. The tests are a function of the criteria, so an unchanged fingerprint — criteria, spec, scope, test command — produces the same file at the price of the loop's most expensive role, and gives the executor a target that stops moving under it. Rewritten when any of those changes, when the file is not on disk, or when the last failure was in the test file itself. See [CONVERGENCE](CONVERGENCE.md). |
| `ratifyPasses` | `2` | Sign-off passes over a ticket before its first attempt. Every role is asked whether it can do its part as written, the planner rewrites the ticket from what they say, and the pass repeats. A ticket ships when everyone signs off, when a majority does, or when the planner and one other do; below that it parks with the objections recorded. Costs `roles × passes` calls per ticket before any code exists, one of them on the reviewer. See [RATIFY.md](RATIFY.md). |
| `ratifyOrder` | all four, in the order above | The order the roles vote in, within a pass. A permutation of the four — it sets the order they vote, not which of them vote, and an order omitting one is refused because sign-off is counted over all four. Two things ride on it. Votes accumulate as they are cast and every role sees the ones before it, so the first votes blind and the last answers three arguments. And on a backend serving one checkpoint at a time (`llamacpp` with `exclusive`, or a router started with `--models-max 1`), two roles sharing a model are free when adjacent and cost a reload when not — see below. |

---

## `ui`

| Key | Default | Notes |
|---|---|---|
| `host` | `127.0.0.1` | |
| `port` | `8799` | |
| `enabled` | `true` | |

**The dashboard has no authentication and its stop button ends a run.** Binding
it beyond loopback publishes run control to everything that can reach the
address. `forge ui --host` overrides for one invocation without writing the
change back, which is the right way to attach from another machine — or tunnel
in and leave the bind address alone.

---

## Two complete examples

### Minimal — everything on one local model

Close to what `forge init --defaults` writes, plus the commands for your
project. The default also leaves an empty `memory` block behind, so its shape is
discoverable without reading this page.

```json
{
  "models": {
    "local": {
      "kind": "llamacpp",
      "baseUrl": "http://127.0.0.1:8080/v1",
      "model": "qwen3.8",
      "modelPath": "/models/Qwen3.8-27B-UD-Q4_K_M.gguf",
      "contextWindow": 65536,
      "maxOutputTokens": 8192,
      "reasoningBudget": 2048
    }
  },
  "roles": {
    "planner": "local",
    "executor": "local",
    "tester": "local",
    "reviewer": "local"
  },
  "commands": {
    "lint": "ruff check .",
    "typecheck": "mypy .",
    "test": "pytest -q"
  }
}
```

One model reviewing its own executor's work is weaker than it looks — it is the
same weights judging the answer they just produced. It is a fine place to start
and the first thing to change.

`forge models` turns the block above into the preset that serves it:

```ini
[qwen3.8]
model = /models/Qwen3.8-27B-UD-Q4_K_M.gguf
jinja = true
ctx-size = 65536
reasoning-budget = 2048
mmproj-auto = false
```

### Populated — local build, Claude review

Shipped as [`templates/config.sample.json`](../templates/config.sample.json).
Two entries name the same router id with different output budgets, because the
planner emits a whole backlog in one reply and the executor emits one file at a
time — one child server, two roles. `api` is declared but unassigned: swapping
the reviewer to it is a one-line edit in `roles`.

```json
{
  "room": "image-marquee",
  "models": {
    "local": {
      "kind": "llamacpp",
      "baseUrl": "http://192.168.1.10:8080/v1",
      "model": "qwen3.8",
      "modelPath": "/models/Qwen3.8-27B-UD-Q4_K_M.gguf",
      "exclusive": true,
      "contextWindow": 65536,
      "maxOutputTokens": 8192,
      "reasoningBudget": 2048,
      "tokensPerSecond": 80,
      "temperature": { "default": 0.6, "deterministic": 0.0 },
      "topP": 0.95
    },
    "local-plan": {
      "kind": "llamacpp",
      "baseUrl": "http://192.168.1.10:8080/v1",
      "model": "qwen3.8",
      "exclusive": true,
      "contextWindow": 65536,
      "maxOutputTokens": 16384,
      "reasoningBudget": 2048,
      "tokensPerSecond": 80,
      "temperature": { "default": 0.6, "deterministic": 0.0 }
    },
    "claude": {
      "kind": "claude-cli",
      "model": "opus",
      "contextWindow": 200000,
      "maxOutputTokens": 32000,
      "rateLimit": {
        "tokensPerWindow": 0,
        "costPerWindow": 15.0,
        "windowSeconds": 18000
      }
    },
    "api": {
      "kind": "anthropic",
      "model": "claude-sonnet-5",
      "apiKeyEnv": "ANTHROPIC_API_KEY",
      "contextWindow": 200000,
      "maxOutputTokens": 16384,
      "rateLimit": {
        "requestsPerMinute": 50,
        "tokensPerMinute": 40000
      }
    }
  },
  "roles": {
    "planner": "local-plan",
    "executor": "local",
    "tester": "local",
    "reviewer": "claude"
  },
  "commands": {
    "lint": "cargo clippy --all-targets -- -D warnings",
    "typecheck": "cargo check --all-targets",
    "test": "cargo test",
    "format": "rustfmt"
  },
  "neverDelegate": [
    "src/auth/**",
    "migrations/**",
    ".github/workflows/**"
  ],
  "memory": {
    "command": [
      "mempalace-mcp"
    ],
    "room": "image-marquee",
    "limit": 6,
    "maxTokens": 1200,
    "write": true,
    "recordRole": "reviewer",
    "maxWriteChars": 2000,
    "dryRun": true
  },
  "loop": {
    "maxAttempts": 5,
    "autoCommit": false,
    "stopOnBlocked": false,
    "retryCycles": -1,
    "respecOnRetry": true,
    "respecCriteria": false,
    "reopenStaleDependents": true,
    "preflight": true,
    "preflightCanary": true,
    "requireGreenBaseline": true,
    "quarantineFailed": true,
    "pollSeconds": 2.0,
    "maxRuntimeSeconds": 0,
    "baselineVerify": true,
    "bugHypotheses": 3,
    "executorTurns": 4,
    "innerTurns": 3,
    "toolchainContext": true,
    "readTools": true,
    "toolTurns": 8,
    "repoMap": true,
    "priorFailures": 8,
    "learnedLimit": 12,
    "flatCycles": 0,
    "reviewWhenStuck": 2,
    "freezeTests": true,
    "ratifyPasses": 2
  },
  "ui": {
    "host": "127.0.0.1",
    "port": 8799,
    "enabled": true
  }
}
```

---

## When it will not load

Every one of these fails at startup, before a single token is spent.

| Message | Cause |
|---|---|
| `no .hybridforge/config.json in <path>` | Wrong directory, or never initialised. `forge init`. |
| `... is not valid JSON` | Usually a trailing comma, or a comment — JSON has none. |
| `config declares no models under 'models'` | Empty or missing `models`. |
| `config has no model assigned to role 'x'` | One of the four roles is missing. |
| `role 'x' points at model 'y', which is not declared` | Typo in `roles`, or a model renamed on one side only. |
| `model 'x' has unknown kind 'y'` | Check the kind table above; aliases are listed there. |
| `loop.retryCycles is -2; expected 0 ...` | Negative but not `-1`. Rejected rather than guessed at. |
| `loop.executorTurns is -1; expected 0 ...` | Same reasoning. |
| `loop.innerTurns is -1; expected 0 ...` | Same reasoning. |
| `memory.recordRole is 'x', which is not a role` | Must be one of the four. |

A config that loads is not a config that works. `forge doctor` asks every model
to answer and reports what came back — run it after any edit to `models`.
