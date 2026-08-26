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
| `openai` | anything speaking the OpenAI chat-completions shape | `openai-compatible`, `ollama`, `vllm`, `lmstudio`, `openrouter`, `litellm` |
| `anthropic` | the Anthropic Messages API | `claude` |
| `gemini` | Google's Generative Language API | `google` |
| `claude-cli` | a local `claude` binary in `-p` mode | `claude-code` |
| `command` | any CLI that takes a prompt and prints an answer | `cli`, `subprocess` |

Default `openai`. An unknown kind fails at startup naming the ones that exist.

### Keys every kind understands

| Key | Default | What it does |
|---|---|---|
| `model` | `""` | The model id sent to the backend. Required by `openai`, `anthropic` and `gemini`; optional for `claude-cli` (empty = whatever the CLI defaults to) and `command`. |
| `contextWindow` | see below | Total prompt+output budget in tokens. The budget gate reserves output from it and refuses — or trims — a prompt that will not fit. |
| `maxOutputTokens` | see below | Ceiling on one reply. Too low is not a silent failure: a truncated planner reply is reported as running out of output room, not as bad JSON. |
| `temperature` | unset | Overrides the temperature the loop asks for. Set it when a model ships a sampling recipe you are meant to follow — several reasoning models degenerate into repetition above their recommended value. |
| `rateLimit` | none | See [`rateLimit`](#ratelimit) below. |
| `cwd` | the project root | Working directory for adapters that shell out. Filled in automatically; override only to point a role at a different checkout. |

**Context-window defaults differ by kind, and it matters.** `openai` asks the
endpoint (Ollama's native API answers) and falls back to 8192 when nothing does;
`claude-cli` assumes 200000/32000; `command` assumes 8192/4096; `gemini`
defaults output to 8192. A window that collapsed to a default is the failure
that reports six 1-3k-token tickets as "too large for this model" — if you see
that, set `contextWindow` explicitly and run `forge doctor`.

### `kind: "openai"`

| Key | Default | Notes |
|---|---|---|
| `baseUrl` | `http://localhost:11434/v1` | Include the `/v1`. Ollama's native API is discovered one level up from it. |
| `apiKeyEnv` | unset | Name of the env var holding the key. Preferred. |
| `apiKey` | `""` | Inline key. Works; makes the file unsafe to commit. |
| `headers` | `{}` | Extra request headers, merged over the `Authorization` header. |
| `extraBody` | `{}` | Fields merged into the request body, for backend-specific knobs (vLLM routing, Ollama `options`, OpenRouter preferences). |
| `supportsTemperature` | `true` | Set false for endpoints that reject the field outright. |
| `topP`, `topK`, `minP`, `presencePenalty`, `frequencyPenalty` | unset | Sent only when set, so a model's own shipped recipe still applies. **`topK` and `minP` are accepted and silently discarded by Ollama's `/v1` shim** — put them in a Modelfile instead (`forge models` writes one). |

### `kind: "anthropic"`

| Key | Default |
|---|---|
| `baseUrl` | `https://api.anthropic.com` |
| `apiKeyEnv` | `ANTHROPIC_API_KEY` |
| `apiKey` | `""` |
| `model` | `claude-opus-5` |
| `effort` | unset — sent as the reply's effort setting when present |

### `kind: "gemini"`

| Key | Default |
|---|---|
| `baseUrl` | `https://generativelanguage.googleapis.com/v1beta` |
| `apiKeyEnv` | `GEMINI_API_KEY` |
| `apiKey` | `""` |
| `maxOutputTokens` | `8192` |

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

`tools` deserves the warning it gets. `claude -p` with tools is an agent, not a
completion call: it reads files on its own and bills for every turn. One
measured reviewer call spent 208k cache-read tokens and $0.34 judging a diff the
loop had already pasted into its prompt. It also voids a guarantee — the loop
decides what each role may see and write, and a role with tools reads and writes
whatever it likes.

### `kind: "command"`

| Key | Default | Notes |
|---|---|---|
| `command` | — | Required, non-empty argv array. |
| `promptOn` | `stdin` | `stdin`, or `argv` to substitute `{prompt}` into the arguments. |
| `model` | first argv element | Label used in the ledger. |
| `contextWindow` / `maxOutputTokens` | `8192` / `4096` | Nothing to discover, so set these. |

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
| `maxAttempts` | `3` | Rework attempts per ticket before it parks as blocked. Past three the failure is usually a spec problem no amount of retrying fixes. |
| `autoCommit` | `false` | Commit each verified ticket. Off so the first unattended runs leave their work in the tree for you to read. |
| `stopOnBlocked` | `false` | Stop the whole run when a ticket blocks, instead of moving on. On means a blocker gets attention; off means the backlog keeps making progress elsewhere. |
| `retryCycles` | `0` | Whole-backlog retry cycles after a run ends anything but done. `0` hands back to a human, `-1` keeps going until the backlog is clean or you stop it. Anything below `-1` is a typo and is rejected. |
| `respecOnRetry` | `true` | Have the planner rewrite each requeued ticket from why it failed before the next cycle. A cycle that re-runs the spec which already failed is a slower version of the same failure. |
| `respecCriteria` | `false` | Let a respec rewrite the acceptance criteria too. Off: the party being judged does not write the standard it is judged against. Left on, one ticket's criteria drifted until they asserted the opposite of what its author wrote. |
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
| `innerTurns` | `0` (off) | How many times a compile failure may go straight back to the executor without spending an attempt. Between apply and the tests step the loop runs the `lint` and `typecheck` commands it was going to run at verify anyway; a failure there can only be the executor's own, so the reply goes back on the same conversation thread against the same contract, and the tester is not asked to write assertions for something that does not compile. Never `test` — a red suite may be the tester's assertion rather than the executor's code, and the executor cannot edit that file to find out. A turn is spent only while the error count is falling; when it stops, the attempt is charged and the ordinary path resumes. Off because it changes how attempts are counted, and every convergence rule in the loop is written against that number. Measured on one ticket: `typecheck` averaged 0.7s against the tester's 12.0s, and 58 of its 95 cycles wrote a test file for an implementation that then failed to compile. See [CONVERGENCE](CONVERGENCE.md). |
| `toolchainContext` | `true` | Show the executor and tester the linter, compiler and test-runner settings that grade what they write — the real files at their real paths, resolved per language from the ticket's own scope and clipped. They are measured by `commands.lint` and `commands.typecheck` and were otherwise never shown what those enforce, so they inferred it from failures. Costs a few hundred characters a call, and the budget gate drops it first. See [CONVERGENCE](CONVERGENCE.md). |
| `priorFailures` | `8` | Earlier failures carried into the executor's prompt alongside the newest one, deduplicated by failure *class* — `(step, error code, file)`, line numbers and quoted values masked — rather than by raw text. Keyed by text this window held one mistake repeated, not several distinct ones. See [CONVERGENCE](CONVERGENCE.md). |
| `learnedLimit` | `12` | How many of a ticket's accumulated learnings reach a prompt, commonest first. A learning is a fact about *this repository* that an earlier attempt established — a compiler flag, an import convention — recorded by respec so the loop stops rediscovering it. It is not a bar: the reviewer is not shown it and no criterion is made from it. `0` renders none. See [CONVERGENCE](CONVERGENCE.md). |
| `flatCycles` | `0` (off) | Consecutive cycles a ticket may fail on exactly the same set of failure *classes* before it is parked and the rest of the backlog carries on. Per ticket, unlike the backlog-wide brake beside it, which cannot see a ticket going nowhere while any other ticket still moves. Off because the threshold was measured and none is safe: on the run this comes from, a ticket that went on to pass sat still for four consecutive cycles while the genuinely unsatisfiable one managed three. The measurement runs either way and logs what it found — `descending`, `churning`, `flat` — which is what the escalation rungs will read. See [CONVERGENCE](CONVERGENCE.md). |
| `reviewWhenStuck` | `2` | Consecutive flat cycles before the loop escalates. Two rungs, one per cycle after it: at `n` the reviewer is asked against the red tree whether the ticket is winnable at all — normally unreachable for a ticket that never verifies — and at `n + 1` the planner is asked the inverted question and may reply `impossible`, which parks the ticket for a reason rather than for a count. `0` never escalates. See [CONVERGENCE](CONVERGENCE.md). |
| `freezeTests` | `true` | Keep a ticket's tests while the criteria they encode are unchanged, instead of re-deriving them on every attempt. The tests are a function of the criteria, so an unchanged fingerprint — criteria, spec, scope, test command — produces the same file at the price of the loop's most expensive role, and gives the executor a target that stops moving under it. Rewritten when any of those changes, when the file is not on disk, or when the last failure was in the test file itself. See [CONVERGENCE](CONVERGENCE.md). |
| `ratifyPasses` | `2` | Sign-off passes over a ticket before its first attempt. Every role is asked whether it can do its part as written, the planner rewrites the ticket from what they say, and the pass repeats. A ticket ships when everyone signs off, when a majority does, or when the planner and one other do; below that it parks with the objections recorded. Costs `roles × passes` calls per ticket before any code exists, one of them on the reviewer. See [RATIFY.md](RATIFY.md). |

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
      "kind": "openai",
      "baseUrl": "http://localhost:11434/v1",
      "model": "qwen3.6:35b-a3b",
      "maxOutputTokens": 8192
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

### Populated — local build, Claude review

Shipped as [`templates/config.sample.json`](../templates/config.sample.json).
Two entries point at the same Ollama endpoint with different output budgets,
because the planner emits a whole backlog in one reply and the executor emits
one file at a time. `api` is declared but unassigned: swapping the reviewer to
it is a one-line edit in `roles`.

```json
{
  "room": "image-marquee",
  "models": {
    "local": {
      "kind": "openai",
      "baseUrl": "http://192.168.1.10:11434/v1",
      "model": "qwen3.6:35b-a3b",
      "contextWindow": 32768,
      "maxOutputTokens": 8192,
      "temperature": 0.6,
      "topP": 0.95
    },
    "local-plan": {
      "kind": "openai",
      "baseUrl": "http://192.168.1.10:11434/v1",
      "model": "qwen3.6:35b-a3b",
      "contextWindow": 32768,
      "maxOutputTokens": 16384,
      "temperature": 0.6
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
    "test": "cargo test"
  },
  "neverDelegate": [
    "src/auth/**",
    "migrations/**",
    ".github/workflows/**"
  ],
  "memory": {
    "command": ["mempalace-mcp"],
    "room": "image-marquee",
    "limit": 6,
    "maxTokens": 1200,
    "write": true,
    "recordRole": "reviewer",
    "maxWriteChars": 2000
  },
  "loop": {
    "maxAttempts": 3,
    "autoCommit": false,
    "stopOnBlocked": false,
    "retryCycles": 2,
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
    "executorTurns": 4,
    "innerTurns": 0,
    "toolchainContext": true,
    "priorFailures": 8,
    "learnedLimit": 12,
    "flatCycles": 3,
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
