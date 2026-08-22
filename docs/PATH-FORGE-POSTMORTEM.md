# Post-mortem: a backlog reported `done` over a tree that never compiled

Derived from the `Puzzle-Path` run of 2026-08-20/21
(`C:\Users\nschr\Documents\GodotProjects\Puzzle-Path\.hybridforge\run.db`, run 1):
15 tickets, 32 executor attempts, **15/15 `done`**, run status `done`, run note
`all tickets complete`. Every file anchor below is against `hybrid-forge` at
`35dc402`.

The delivered artifact is 17 TypeScript files, 4,068 lines including tests. It
has never been compiled, and it cannot be: 16 of the 17 source files import
modules that do not exist anywhere in the tree, and the repository has no
`package.json`, no `tsconfig.json`, and no JavaScript test runner of any kind.
Nothing the loop ran executed a single line of the code it accepted.

This is the failure `docs/LANGUAGE-COVERAGE.md` was written to prevent —
"a language with no runner is invisible to verification, and invisible is
reported as fine" — occurring after that spec was built, because the mechanism
that decides whether a language is covered fails **open**.

The run failed for four independent reasons. Ordered by what unblocks what.

---

## 1. The catch-all coverage rule is fail-open

### What happened

The target is a Godot/GDScript game. `forge init` wrote a flat-string config:

```json
"commands": {
  "lint": "python -m gdtoolkit.linter scripts scenes tests",
  "typecheck": "",
  "test": "call \"...Godot_v4.7-stable_win64_console.exe\" --headless --import && call addons\\gdUnit4\\runtest.cmd --godot_binary \"...\" -a tests/"
}
```

The backlog it was then handed writes only TypeScript, into a new `src/` tree —
a standalone browser-based level editor that has no Godot dependency at all.

Reproduced against the real config at `35dc402`:

```
>>> c = Config.load(Path("…/Puzzle-Path"))
>>> c.covering("test", ".ts")
('call "…Godot…" --headless --import && call addons\\gdUnit4\\runtest.cmd … -a tests/', 'catch-all')
>>> c.covering("test", ".rs")
(… same command …, 'catch-all')
>>> c.covering("lint", ".py")
('python -m gdtoolkit.linter scripts scenes tests', 'catch-all')
```

The Godot command claims to cover TypeScript. It also claims to cover Rust,
Python, and every other language, because the rule that would reject it never
fires.

### Why

`config.covering()` (`forge/config.py:539`) accepts a catch-all unless
`_wrong_language()` (`forge/config.py:160`) can prove the command belongs to a
different language. That proof requires the command to name a runner in
`_RUNNER_LANGUAGES` (`forge/config.py:103`). `call`,
`Godot_v4.7-stable_win64_console.exe`, `runtest.cmd` and `gdUnit4` name nothing
in that table, so `_named_runners()` returns empty, `_wrong_language()` returns
`""`, and the catch-all is taken as covering.

The same is true of `python -m gdtoolkit.linter`, which lints `.gd` files and is
reported as covering `.py`.

### What it took down

Every gate downstream asks `covers()`, so every one of them passed:

| Gate | Anchor | Should have said | Said |
|---|---|---|---|
| `_uncovered_languages` | `loop.py:4135` | `.ts` has no runner — block the ticket | `[]` |
| `_warn_uncovered` at ingest | `cli.py:1149` | warn before a token is spent | silent |
| `_report_coverage` in doctor | `cli.py:265` | `.ts (no test command)` | prints the Godot command as `.ts` coverage |
| `_suite_suffix` | `loop.py:3965` | no suite collects `.ts` | `.ts` — so the tester authored `.ts` tests |
| `_no_runner_note` / bug loop | `loop.py:4163` | name the gap | never reached |

The last row is the one that produced files. Because `covers("test", ".ts")` was
true, `_suite_suffix` returned `.ts` from the ticket's own written files, and the
tester wrote `node:test` suites into `tests/decor/*.ts` — the same `tests/`
directory gdUnit4 globs. gdUnit4 ignores `.ts`, exits 0, and
`05-verify-test.json` records `"status": "ok"` on every ticket. Fifteen
consecutive green verdicts, none of which ran anything.

### Fix 1a — make the catch-all fail closed (small)

An unrecognised runner is not evidence of coverage. `covering()` should
distinguish three cases, not two:

- command names a runner that covers the suffix → `catch-all`, as today;
- command names a runner that does not → `""` with `runs {other}`, as today;
- **command names no runner this table knows → `""` with `unrecognised runner`**,
  whenever the repository contains more than one code suffix.

The single-language guard keeps every existing config working: a Rust-only repo
with `"test": "make test"` still passes, because there is no second language for
the claim to be wrong about. A repo containing both `.gd` and `.ts` has to say
which command runs which.

Cost: one branch in `covering()`, plus the census `_report_coverage` already
computes. It would have blocked PF-001 at ingest.

### Fix 1b — prove coverage instead of inferring it (the real fix)

`_RUNNER_LANGUAGES` (config) and `_RUNNER_SUFFIXES` (loop, `loop.py:3915`) are
two hardcoded tables that must know every test runner that will ever exist, and
they do not agree with each other. Godot is in neither. Neither is Bazel, Meson,
`ctest`, `dune`, `mix`, `sbt`, `zig build test`, or whatever the next target
repository uses. Each miss is silent, and each silent miss reports green.

A measurement needs to know none of them. At preflight — which already exists and
already runs the commands (`LoopSettings.preflight`, `config.py:243`) — for each
code language present in the tree:

1. write a deliberately failing canary test in that language, at the path
   `_test_target` would choose for it;
2. run the step's command for that language;
3. assert it goes **red**;
4. delete the canary.

A command that stays green over a test that cannot pass does not run that
language. That is a direct observation, it costs one command invocation per
language per run, it is immune to runner naming, and it separates the three
outcomes that matter: red (covered), green (not covered — refuse to start), and
an error naming the canary (covered, but the toolchain is broken — also worth
refusing).

This is the change I would prioritise above everything else in this document.

### Fix 1c — check that the ticket's own test file was executed

Nothing in the loop verifies that the file the tester just wrote was collected by
the test command. `_UNBUILDABLE` (`loop.py:139`) exists only on the
bug-reproduction path. A green from a command whose output never mentions
`tests/decor/pf_001_test.ts` is not evidence about PF-001, and here it was
recorded as if it were, fifteen times.

Cheapest usable form: after the tests step, require the runner's stdout to mention
either the test path, its stem, or a test count that increased against the
baseline. Failing all three, the attempt is unverified — the same class of outcome
`_unverifiable` already models, and it should reuse that path rather than
inventing a new one.

---

## 2. `typecheck` was empty, for a language whose type checker is the whole check

`"typecheck": ""` is skipped silently by the verify plan. For a TypeScript
backlog, `tsc --noEmit` is not an optional nicety — it is the single command that
would have caught every phantom import in section 3, in about two seconds, with
no model involved.

`toolchain.py` already knows `tsc` (`config.py:117`). An empty `typecheck` for a
language whose ecosystem has a standard, near-universal checker should be a
preflight warning at minimum, and arguably the same block as a missing test
command. The set is small and stable: `.ts`/`.tsx` → `tsc`, `.py` → `mypy` or
`pyright`, `.rs` → `cargo check`, `.go` → `go vet`, `.java`/`.kt` → the build's
compile task.

---

## 3. A backlog of fifteen leaf modules with no scaffold and no dependency edges

### What happened

Every ticket in the run:

```
PF-001 needs=[] allowed_files=["src/parser/level.ts"]
PF-002 needs=[] allowed_files=["src/parser/validation.ts"]
PF-003 needs=[] allowed_files=["src/renderer/logical.ts"]
…
PF-015 needs=[] allowed_files=["src/io/filesystem.ts"]
```

Fifteen tickets, fifteen disjoint leaf modules, zero dependency edges, and no
ticket owning:

- `package.json`, `tsconfig.json`, or any build manifest;
- an entry point or `index.html`;
- `src/types.ts` — the shared model every one of them was told to use.

PF-001's context field says, verbatim: *"All downstream components depend on the
internal model `{ tiles: Tile[], player: Vec2, blocks: Vec2[] }`."* `Vec2` and
`Tile` had no home and no owner. The executor did the only thing it could and
wrote `import { Vec2 } from '../types'` — a file outside its allowed scope, which
it was not permitted to create and no later ticket created either.

Every subsequent ticket hit the same wall independently and invented its own name
for the missing module. The result across `src/`:

```
../types (4)  ../geometry  ../theme  ./theme  ../atlas  ../sprite
../wall-utils  ./swatch  ../model/level  ../model/cell  ../model/rect
../model/grid  ../models/level_model (2)  ../../../types
```

Sixteen imports, eight invented locations, one existing target (`./prng`). PF-002
went further and invented a *different* `Level` shape than PF-001 returns —
`level.grid` and `level.view` against PF-001's `{tiles, player, blocks, width,
height}` — so even if the module existed, the two halves of the parser could not
typecheck against each other.

### Fix 3a — an unresolved-relative-import gate at APPLY

The highest value-per-line change available. After an edit lands, extract every
**relative** import target from the written files and check each one resolves to a
file on disk, or to a path some ticket in the backlog owns. If not, fail the
attempt with the exact list.

It is a regex and a `stat`. No model, no toolchain, no ecosystem knowledge beyond
one pattern per language family:

```
ts/js   import … from '…'  |  require('…')  |  import('…')     — leading . or ..
python  from .x import  |  from ..x import
rust    mod x;
go      import "./x"
c/c++   #include "x.h"
```

This alone fails PF-001 on attempt 1 with `src/parser/level.ts imports '../types',
which does not exist and no ticket creates` — exactly the sentence a human needed
to see fifteen tickets ago. It also generalises to the case where the target *is*
owned but not yet built, which is a dependency the planner failed to declare (3b).

### Fix 3b — closure check at ingest

`forge ingest` can see the shape of what it just parsed. Two checks worth refusing
on, or at least warning hard about:

- **No build manifest.** The backlog writes `.ts` into a tree with no
  `package.json`/`tsconfig.json`, and no ticket creates one. For a greenfield
  language in an existing repo this is always wrong. The per-language manifest set
  is the same small table `toolchain.py:EVIDENCE_GLOBS` already carries.
- **N tickets, N files, zero `needs`.** A backlog where nothing depends on
  anything is either genuinely parallel — rare, and usually small — or a planner
  that emitted a file list instead of a plan. It is a diagnosable shape and worth
  naming at ingest, when nothing has been spent.

A third, softer one: any type or symbol a ticket's context names as shared ("all
downstream components depend on…") should have an owning ticket. That one needs a
model; the first two do not.

---

## 4. The normative source document never reached the executor

Every ticket in the run has `reference_files=[]`.

The source was `Docs/05_Level_Editor_Spec.md` — a 700-line specification whose
section 2 is explicitly labelled normative and contains, as tables:

- the complete legal alphabet: **18 characters**, with the trap that `B` is a
  block, so the blue door is `U`;
- the exact error strings, seven of them, to be emitted verbatim;
- the exact order the checks run in, and why the order is observable.

What PF-001's spec field says instead:

> *Reject internal blanks, leading blanks, spaces/tabs, and non-ASCII with exact
> error strings.*

The planner read the normative tables and paraphrased them away. The executor
never saw them. What it produced implements **4 of the 18 characters**
(`. # @ X`), treats `X` — the exit — as a pushable block, invents error strings
(`'no player'` where the spec mandates `no player start ('@')`), omits the
`no exit ('X')` check entirely, and validates width in a separate pass where the
spec requires it interleaved with the symbol scan. The acceptance criteria the
planner *did* write are mostly correct and would have caught several of these.
They were never run.

Two things worth changing:

- **Carry normative structure through, or attach the source.** When a spec
  document contains tables under a heading marked normative/contract/legend, the
  ticket should either quote them verbatim or list the source document in
  `reference_files` with the relevant section named. A paraphrase of a lookup
  table is a lossy encoding of the one part of a spec that cannot tolerate loss.
- **`reference_files` empty on a backlog derived from a document is a smell.** The
  document that generated the backlog is, by construction, the most relevant
  reference for every ticket in it. Defaulting it in costs nothing.

---

## 5. On moving hybrid-forge out of the target project

The instinct is right, and the reasoning splits into three parts — only one of
which is about location.

**Move out: run state.** `.hybridforge/` currently holds `run.db`, `run.db-shm`,
`run.db-wal`, `abandoned/`, and `artifacts/`. This run alone left roughly a
thousand artifact files in the target working tree and forced a `.gitignore` edit
to keep the SQLite files out of git. A per-machine forge home
(`~/.hybridforge/runs/<repo-id>/`) holding state and artifacts, with only
`config.json` and `tickets/` remaining in the repo, is strictly better: the target
repository's diff stays about the target repository.

**Do not move out: the verify commands.** They are properties of the target
repository and its installed toolchain. Centralising them makes section 1 worse,
not better, because the config drifts further from the tree it describes.

**The actual fix is not location — it is scope.** What went wrong was not that
config lived in the repo. It was that *one* command set claimed authority over a
subproject with a completely different toolchain. Per-language maps
(`LANGUAGE-COVERAGE.md`) are the right shape for a repository where two languages
share one root. They are the wrong shape for what this run actually was: a
**separate project inside the repository**, with its own package manager, its own
root, and commands that must run with a different `cwd`.

That suggests a workspace concept sitting above the language map:

```json
"workspaces": [
  {
    "root": ".",
    "commands": {
      "lint": "python -m gdtoolkit.linter scripts scenes",
      "test": "godot --headless … runtest.cmd -a tests/"
    }
  },
  {
    "root": "tools/path-forge",
    "commands": {
      "lint": "npm run lint",
      "typecheck": "tsc --noEmit",
      "test": "npm test"
    }
  }
]
```

With three rules:

1. A ticket's `allowed_files` determine its workspace. Files spanning two
   workspaces is a ticket-scoping error, refused at ingest.
2. Verify runs the owning workspace's commands with `cwd` set to the workspace
   root — which is what `npm test` and `cargo test` need, and what a repo-root
   command cannot give them.
3. **A file in no workspace is a block, not a catch-all.** This is section 1's
   fail-closed rule expressed structurally, and it is the property that makes the
   whole thing worth building.

`_verify_plan` (`loop.py:733`) stays whole-project *within* a workspace, so the
baseline amnesty, the orphan sweep and "you broke this, not the ticket before
you" all keep working unchanged — they scope to a workspace instead of a
repository.

The `commands` map remains for the genuinely-shared-root polyglot case.
Workspaces handle the subproject case, which is the more common one in practice
and the one that produced this run.

---

## Suggested landing order

| | Change | Cost | Would it have stopped this run | Status |
|---|---|---|---|---|
| 1 | **3a** — unresolved relative imports at APPLY | one afternoon | Yes, at PF-001 attempt 1 | **done** — `forge/imports.py`, `LOOP-INVARIANTS.md` §15 |
| 2 | **1b** — canary proof of language coverage at preflight | small, self-contained | Yes, before the first ticket | **done** — `WORKSPACES.md` phase 3 |
| 3 | **1a** — unrecognised runner fails closed | one branch | Yes, at ingest | superseded by the canary, which measures instead of inferring |
| 4 | **2** — empty `typecheck` warns for languages that have one | trivial | Yes, at preflight | **done** — `TYPECHECKERS`, `Workspace.unchecked`; reported by `doctor` and at run start, never gated |
| 5 | **3b** — ingest closure checks | moderate | Yes, at ingest | **done** — phase 4 refuses a backlog no build owns; `toolchain.manifest_gaps` reports a language nothing can build (`LOOP-INVARIANTS.md` §17). `ingest.undeclared_order` reports the zero-`needs` shape |
| 6 | **4** — normative structure / `reference_files` | moderate | No — but it is why the code was wrong even where it parsed | **done** — planned ingests attach their source document; the planner is told not to paraphrase tables; prose references keep their tables when trimmed |
| 7 | **1c** — assert the authored test actually ran | moderate | Yes, at the first tests step | **done** — `failures.test_count`, `_test_was_collected`; `LOOP-INVARIANTS.md` §16 |
| 8 | **workspaces** | largest | Yes, and it is the durable shape | **done** — `WORKSPACES.md`, all five phases |

Items 1–4 are each independently sufficient to have turned this run red, and
together they cost less than one of the fifteen tickets did.

---

## Postscript: what to do with the run's output

The 4,068 lines in `src/` and `tests/` are not salvageable as delivered. The
parser implements the wrong alphabet; two modules disagree about the core type;
`src/editor/resize.ts` calls a helper that returns `[]` and then indexes `[0]`;
`src/io/filesystem.ts` calls `FileSystemDirectoryHandle.getFile()`, which does
not exist, and interpolates an untrusted filename into `innerHTML`; and the PRNG
that must byte-match Godot's `String.hash()` returns signed 32-bit values where
Godot returns unsigned — with the test's own "reference implementation of Godot's
hash" reproducing the same bug, so it passes.

They are, however, an excellent replay corpus: a fifteen-ticket backlog where the
correct verdict for every ticket is known and is `blocked`. Any of the fixes above
can be checked against it directly.
