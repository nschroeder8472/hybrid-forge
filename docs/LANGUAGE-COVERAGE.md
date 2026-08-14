# Per-language verify commands — design spec

**Status:** phases 1–3 built. 4 (`forge toolchain` and the setup loop) and 5
(lint and typecheck as reported gaps) are still open. Each phase landed on its
own with tests; the sections below describe the whole design, and what is not
yet built is marked.

`commands.lint`, `.typecheck` and `.test` are single strings, which encodes an
assumption no real project keeps: that a repository is one language. Everything
downstream inherits it — which language the tester writes in, where the test
file goes, what verification proves, what a green ticket means, and whether a
bug can be reproduced at all.

---

## The evidence

Three failures, all the same root.

**A ticket shipped green over code that never ran.** TT-005 wrote
`web/index.html` and `web/main.js`. The suite is `cargo test`, so the ticket
authored no tests — correctly, by the current rule — and its criteria were
checked by reading the diff. Every criterion was a token-presence check, and
every one was true of code that threw on the second line of its own entry
point. The page loaded to an empty board. (`LOOP-REPAIR.md` §3.6.)

**A bug report could not be reproduced, because the fault was in the language
the suite does not run.** A report said the game starts at level 0.
`Game::new` sets `level: 1`, so no Rust test could fail; the bug loop
re-diagnosed, and the answer was `web/main.js:13` — `instantiateStreaming`
resolves to `{module, instance}`, so `instance.exports` is `undefined` and the
page keeps its hardcoded `Level: 0` forever. `cargo test` runs no JavaScript.
The loop can now *name* that gap and still cannot cross it.

**The single-command model already leaks into unrelated tickets.** One `.js`
file in a Rust repo once made `_suite_suffix` report that the suite collects
`.js`, and every Rust ticket afterwards authored no tests and reported the skip
as routine. The fix was to read the language off the *command* instead — which
works only while there is exactly one.

The pattern: a language with no runner is invisible to verification, and
invisible is reported as fine.

---

## The change

`commands.test`, `.lint` and `.typecheck` each become a **map from language to
command**. A string still means what it means today.

```json
"commands": {
  "test": {
    ".rs": "cargo test",
    ".js": "node --test web/"
  },
  "lint": {
    ".rs": "cargo clippy --all-targets -- -D warnings",
    ".js": "eslint web/"
  },
  "typecheck": {
    ".rs": "cargo check --all-targets"
  }
}
```

Keys are extensions (`.rs`) or language names (`rust`, `javascript`), and `*`
is a catch-all for a command that covers everything. Names normalise to
extensions through one table, so `"python"`, `"py"` and `".py"` are the same
key. A string is read as `{"*": "..."}`, so every config that exists today
keeps working unchanged and means exactly what it meant.

`typecheck` legitimately has no entry for some languages. That is a covered
language with no type checker, not a gap — the gate below is about `test`.

---

## What "covered" means

Two questions, answered from the file census `evidence.repo_files` already
produces:

- **Which languages does this repository contain?** Extensions of tracked and
  untracked source files, minus generated and non-code ones.
- **Which languages does this ticket write?** Extensions of its `allowed_files`.

A language is **covered** when a `test` command exists for it, either by its own
key or by `*`. A ticket is covered when every language it writes is.

---

## Behaviour, subsystem by subsystem

### Verification

`_VERIFY_STEPS` becomes the cross product of step kind and configured language:
`lint[.rs]`, `test[.rs]`, `test[.js]`. Every command for a language **present in
the repository** runs, in the current order — verification stays whole-project,
because the baseline amnesty, the orphan-test sweep and the "who broke this"
attribution all depend on it. Identical commands under two keys run once.

Step names carry the language so the dashboard and the step log stay readable,
and `_baseline_failures` keys its signatures the same way.

### The tester

`_test_target` picks the language from **the ticket's own scope**, not from the
global command. A ticket writing `web/main.js` gets `web/main_test.js` and the
JavaScript runner's conventions; a ticket writing `src/game.rs` is unchanged.
`_RUNNER_SUFFIXES` stops being a guess about which language the project is and
becomes a check that the command for a language plausibly runs that language —
`"test": {".js": "cargo test"}` is a configuration error worth naming.

`_suite_suffix`'s whole reason for existing — inferring one language for the
project — goes away. That is the point.

### The gate: a ticket in an uncovered language blocks

This is the ask. When a ticket writes a language with no `test` command, it
does not run: it blocks before any model is called, with a note that names the
language, the files, and what to do.

```
TT-005: blocked — this ticket writes .js files and no test command covers .js,
so nothing here could check the work. Set one up:

  forge toolchain --language .js      (detects it from the repo)
  or add it by hand:
  "commands": { "test": { ".js": "node --test web/" } }

Then: forge retry --ticket TT-005
```

Blocking is right where warning is not: the alternative is what already
happened — the ticket passes on review alone and reports itself finished. It
blocks the **ticket**, not the run; the rest of the backlog proceeds and the
run ends `blocked` with the list.

For a bug ticket the same gate fires one step earlier, at `_repro_target`: a
reproduction that cannot be run proves nothing, and this is exactly the level-0
case. The block replaces "sharpen the report" with "this fault is in .js and
nothing here runs .js".

### Coverage reporting

`_report_unexecuted` already names tickets that passed on review alone. It
gains the reason — no runner for their language — which turns a warning about a
ticket into a warning about the project.

---

## The setup feedback loop

**`forge toolchain [--language X]`** — a new command wrapping the detector that
already exists. `toolchain.detect` reads CI workflows, Makefiles and
contributing guides and asks a model what the verify commands are; today it
answers once for the project. Scoped to a language it answers per language, and
writes the result into `commands` on confirmation. With no `--language` it
reports the whole coverage matrix:

```
language  files  test                     lint
.rs         34   cargo test               cargo clippy --all-targets
.js          3   (none)                   (none)
```

**At `forge init`**, the wizard runs the census first and asks per language,
instead of asking three questions about "the project".

**At ingest and `forge bug`**, the planner is told which languages have runners.
A ticket landing in an uncovered language is reported *before the run starts* —
the cheapest moment — and the planner is asked to name the framework it would
expect for that language, as a suggestion for the human to accept:

```
This plan writes .js, which has no test command. The planner suggests:
  "test": { ".js": "node --test web/" }
Accept with: forge toolchain --language .js --accept
```

Suggested, never written silently. Changing what verification means is not a
decision the loop gets to make on its own.

---

## Lint and typecheck

Same map, same normalisation, one difference: a missing `lint` or `typecheck`
entry is not a gate. Lint is quality; tests are proof. An uncovered language
blocks the ticket; an unlinted one is reported at run end and no more.

---

## Migration

- A string becomes `{"*": cmd}` at load. No existing config changes meaning.
- `config.write()` emits maps; a config that came in as strings goes out as
  strings until something adds a second language, so `forge init` on an old repo
  does not rewrite what it did not change.
- `Config.commands` keeps its type as `dict[str, Any]`; a new
  `Config.commands_for(kind) -> dict[str, str]` does the normalising, and every
  call site moves to it. There are nine.

---

## Decisions to confirm

1. **Keys are extensions, names are aliases.** `.rs` canonical, `rust` accepted.
   The alternative — language names canonical — reads better and needs a bigger
   table to map files to it.
2. **Every present language's commands run on every ticket.** Slower on a
   polyglot repo than running only the ticket's own languages, but it is what
   keeps "this ticket broke that" honest. A `commands.scope: "ticket"` escape
   hatch is possible later; not in v1.
3. **An uncovered language blocks the ticket, not the run.** The backlog keeps
   making progress and the gap is reported once at the end.
4. **Nothing is written to config without a human.** The loop detects, suggests,
   and blocks; `forge toolchain` writes.

---

## Phases

**1 — Config and normalisation. Done.** Map or string, alias table, validation,
`commands_for`, migration, `forge doctor` shows the matrix. No behaviour change:
a one-language project runs exactly as now. *Tests: every existing config shape
still loads and means the same; aliases normalise; a command that cannot run its
language is a config error.*

**2 — Verification over the map. Done.** Per-language steps, step naming, baseline
keyed to match, identical commands deduped. *Tests: two languages both run;
failures attribute to the right step; a language with no files present is
skipped.*

**3 — The tester and the gate. Done.** Test language from the ticket's scope, the
block for an uncovered language, `_repro_target` the same. *Tests: a `.js`
ticket gets a `.js` test file; an uncovered ticket blocks before any model call;
the bug loop's block names the language.*

**4 — `forge toolchain` and the setup loop. Open.** Per-language detection, the
coverage matrix, the accept flow, the wizard asking per language, ingest and
`forge bug` reporting uncovered scope up front. *Tests: detection scoped to one
language; nothing is written without the accept flag.*

**5 — Lint and typecheck. Open.** The same map, reported not gated.

---

## Risks

**A polyglot repo gets slower.** Every present language's suite runs per
attempt. Mitigated by decision 2's escape hatch if it bites.

**The extension census misreads a project.** A `.js` file in a Rust repo is
usually a build script, not a language the project owns — and under this design
it would block a ticket that touches it. The census must count *source* files
and respect ignore rules, and the block must be one command away from resolved.

**A wrong per-language command is worse than none.** `"test": {".js": "npm
test"}` in a repo with no `package.json` fails every JS ticket forever. Phase 1
validates that the command's runner matches the language; `forge doctor` should
run each command once and report what it did.
