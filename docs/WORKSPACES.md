# Workspaces — design spec

**Status:** built, all five phases. Written from the `Puzzle-Path` run of
2026-08-20/21 (`docs/PATH-FORGE-POSTMORTEM.md`); every file anchor is against
`35dc402`, before the change.

What is in: the `workspaces` key and its validation, longest-prefix file
resolution, `cwd` per build in `_shell`, one verify plan per build with a
ticket-scoped variant, path re-rooting so a subproject's failures still
attribute, and the preflight canary that measures coverage instead of inferring
it. 51 tests, 930 in the suite, green.

The original failure is now caught with **no config change at all**: the
Puzzle Path `commands` block, unmodified, blocks its own run because a gdUnit4
launcher stays green over a `.ts` file that cannot parse. Workspaces make the
truth expressible; the canary is what notices when nobody has expressed it.

The gates are in as well: a file no workspace owns blocks at ingest and at the
ticket, a ticket straddling two builds is refused as a scoping error, `doctor`
prints one matrix per build plus the files nothing owns, and a ticket's test
file is written where its own build looks for it. 68 tests, 948 in the suite,
green.

Setup proposes the list rather than waiting to be told: `forge init` walks the
tree for build manifests and asks only when it finds more than one, and `forge
toolchain --workspace` sets one build's command up, reading that build's own
files. 82 tests, 970 in the suite, green.

`docs/LANGUAGE-COVERAGE.md` replaced one command per step with one command per
*language*, which fixed the case where two languages share a root. It left the
case where a repository contains a second **build** — its own manifest, its own
dependency tree, its own commands that only work from inside its own directory.
That case cannot be expressed at all today, and the thing that happens instead
is that the root's command claims the subproject's files and reports green over
code it never ran.

A workspace is a build root. Not a language, not a module, not a folder: a
directory that owns a manifest and a set of commands that need `cwd` set to it.

---

## The evidence

**Fifteen tickets shipped `done` over a tree that never compiled.** The target
was a Godot game whose `commands.test` launches gdUnit4 against `tests/`. The
backlog wrote 4,068 lines of TypeScript into a new `src/` tree. gdUnit4 globs
`tests/`, ignores `.ts`, exits 0. Every verify step recorded `"status": "ok"`,
fifteen times, having executed none of the code it was reporting on. The run
finished with `all tickets complete`.

**The language map would not have saved it, because nobody could have written
the entry.** The correct command for that subproject is `npm test` with
`cwd=tools/path-forge`. `commands` has no `cwd`, `_shell` hardcodes
`cwd=self.config.root` (`loop.py:644`), and the only workaround — `cd
tools/path-forge && npm test` — is rejected by `toolchain.clean_command`
(`config.py:_REJECT`) precisely because a command starting with `cd` is not a
verify command. There was no config that expressed the truth.

**The catch-all filled the vacuum and answered for everything.** Reproduced
against the real config:

```
>>> c.covering("test", ".ts")
('call "…Godot…" --headless --import && … runtest.cmd -a tests/', 'catch-all')
>>> c.covering("test", ".rs")
(… the same command …, 'catch-all')
```

`covering` accepts a catch-all unless `_wrong_language` can prove the command
belongs elsewhere, and that proof needs the command to name a runner in
`_RUNNER_LANGUAGES` (`config.py:103`). `runtest.cmd` and `gdUnit4` name nothing
in that table, so the Godot launcher claims TypeScript, Rust, Python and every
other language at once.

The pattern: **a build with no way to declare itself is absorbed by the build
above it, and absorption reads as coverage.**

---

## The change

A new top-level `workspaces` array. Each entry is a root, its commands, and
optionally what it does not own.

```json
{
  "workspaces": [
    {
      "root": ".",
      "commands": {
        "lint": "python -m gdtoolkit.linter scripts scenes",
        "test": "godot --headless --import && addons/gdUnit4/runtest.cmd -a tests/"
      },
      "excludes": ["tools/**"]
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
}
```

`commands` inside a workspace keeps the exact shape `LANGUAGE-COVERAGE.md`
defines — string, language map, `false`/`"skip"` exemptions, alias
normalisation. Workspaces sit **above** that layer, not instead of it: a
genuinely polyglot single root still uses a language map, and now a repository
with two builds can give each of them one.

**Absent `workspaces` means one implicit workspace at `.` holding today's
top-level `commands`.** Every config that exists keeps its exact current
meaning, the same way a plain string still means `{"*": …}`. This is the
property that makes the change safe to land in one commit.

`excludes` is load-bearing rather than decorative. Without it the root
workspace's `tests/` glob swallows `tools/path-forge/tests/`, which is precisely
how the gdUnit4 command came to "collect" the TypeScript suites. A child
workspace's root is implicitly excluded from every ancestor; `excludes` is for
the rest.

---

## Resolution

**File to workspace: longest matching root wins.** `tools/path-forge/src/parser/level.ts`
resolves to `tools/path-forge`, not `.`. Ties cannot occur — two workspaces with
the same root is a config error caught at load.

Three rules on top of that:

1. **A ticket belongs to one workspace, determined by its `allowed_files`.** A
   ticket whose writable files resolve to two workspaces is a scoping error,
   refused at ingest rather than discovered on attempt three.
2. **Verify runs the owning workspace's commands with `cwd` set to its root.**
   This is the rule the whole spec exists for.
3. **A file in no workspace blocks.** No catch-all, no inference, no runner
   table. Either a workspace claims the file or nothing does, and
   nothing-claims-it is a hard stop with a one-command fix.

Rule 3 carries the value. Rules 1 and 2 are the plumbing that makes it
expressible. It is the fail-closed default of `LANGUAGE-COVERAGE.md`'s gate,
restated structurally: coverage stops being something inferred from the text of
a command and becomes something the config either states or does not.

---

## Behaviour, subsystem by subsystem

### Verification

`_verify_plan` (`loop.py:733`) becomes one plan per workspace, each run with its
own `cwd`. Within a workspace it stays **whole-workspace**, not per-ticket — the
existing docstring's reasoning is right and should not be touched. The baseline
amnesty, the orphan sweep and "you broke this, not the ticket before you" all
rest on verification being wider than the ticket being verified.

What changes is the blast radius, not the sweep. A red Godot tree stops
excusing a TypeScript ticket, and a broken TypeScript build stops failing a
GDScript one. That is a tightening of the amnesty that falls out for free.

Step labels follow the existing convention: a single workspace keeps the plain
name, so a one-build project's step log and dashboard read exactly as they do
now. Two workspaces produce `test[path-forge]`, composing with the language
suffix where both apply.

`_languages_present` (`loop.py:768`) scopes to the workspace's files minus its
excludes, so a JavaScript runner in a workspace with no JavaScript is still
skipped.

### Failure attribution — the hard part

`errors_naming` (`failures.py:412`) and `signatures` (`failures.py:157`)
attribute blame by matching **repo-relative** paths against runner output. Run
`tsc` with `cwd=tools/path-forge` and it prints `src/parser/level.ts`. The
ticket owns `tools/path-forge/src/parser/level.ts`. No match, so the error names
nothing, so `_baseline_failures` (`loop.py:1131`) excuses it, so the attempt goes
green.

That is the same failure class this spec exists to remove, reintroduced by the
fix. It must land in the same commit, not the next one:

- every location parsed out of a workspace's output has the workspace root
  prepended before matching;
- `_LOCATION` (`failures.py:293`) already handles absolute paths and drive
  letters, so absolute output needs nothing — it is relative output that needs
  the prefix;
- a workspace whose blame cannot find its own canary (below) is a workspace
  whose green means nothing, and the run should refuse to start on it.

### The preflight canary

`LoopSettings.preflight` (`config.py:243`) already probes models and already
runs the commands. Extend it, per workspace, per language present:

1. write a deliberately failing test at the path `_test_target` would choose;
2. run that workspace's command for that language, from that workspace's root;
3. require it to go **red**, and require the failure to attribute to the
   canary's repo-relative path;
4. delete the canary.

A command that stays green over a test that cannot pass does not run that
language. A command that goes red without naming the file that failed cannot
support blame. Both are direct observations, both cost one invocation, and
neither needs `_RUNNER_LANGUAGES` (`config.py:103`) or `_RUNNER_SUFFIXES`
(`loop.py:3915`) to know anything — which matters, because those two tables must
otherwise enumerate every runner that will ever exist, they currently disagree
with each other, and Godot is in neither.

The canary subsumes the inference. Once it runs, `covering`'s catch-all
heuristic is a hint for error messages rather than a gate.

### The tester

`_suite_suffix` (`loop.py:3965`) and `_test_target` (`loop.py:4062`) resolve
within the ticket's workspace. `_TEST_ROOTS` (`loop.py:3794`) becomes
workspace-relative — `src/test/java` under the Gradle module that owns it, not
under the repository root, which is the JVM case that motivated the table in the
first place and is exactly where a second build is most likely to exist.

`_example_test` (`loop.py:3847`) reads examples from the ticket's own workspace.
A TypeScript ticket handed a GDScript suite as "the convention this repo
follows" is how conventions launder across builds that share nothing.

### The gate

`_uncovered_languages` (`loop.py:4135`) asks a cheaper question first: does a
workspace own these files at all? If not, the ticket blocks with a note naming
the unowned paths and the command that fixes it. If a workspace owns them, the
existing per-language logic runs inside that workspace unchanged.

`_warn_uncovered` (`cli.py:1149`) reports the same thing at ingest, before a
token is spent, which is where it should have fired on the Puzzle Path backlog.

### Coverage reporting

`_report_coverage` (`cli.py:265`) prints one matrix per workspace, plus a final
section listing source files owned by no workspace. That last list is the report
that would have made this run's misconfiguration obvious in two seconds, and it
is empty in every correctly configured repository.

---

## Detection at init

`toolchain.py:EVIDENCE_GLOBS` already enumerates every manifest that marks a
build root. Today it globs them at `config.root` only. Walking the tree for the
same globs, skipping `_IGNORED_DIRS` (`loop.py:3809`), produces the candidate
workspace list directly: `project.godot` at `.`, `package.json` at
`tools/path-forge`, and so on.

`forge init` proposes the list, the user confirms or edits it, and
`toolchain.detect` runs once per workspace with `cwd` set. Same model call, N
times, each with narrower and more honest evidence than one repository-wide
sweep — a subproject's own `package.json` and README state its commands far
better than the root README does.

Nothing is written without a human, which is decision 4 of
`LANGUAGE-COVERAGE.md` and stays true here.

---

## Migration

No config changes meaning. A config without `workspaces` is read as one
workspace at `.` carrying its existing top-level `commands`, and every code path
above resolves to exactly what it does today.

Adding a workspace is additive and reversible. `forge doctor` should report the
implicit workspace explicitly, so a user who has never heard of the feature can
still see the model the loop is using.

Top-level `commands` stays supported permanently rather than being deprecated
into a workspace entry. Most repositories are one build, and making them write
an array to say so is a worse default than the string they write now.

---

## Decisions to confirm

1. **Longest-prefix resolution, not glob patterns.** A workspace is a directory.
   Glob-based ownership invites overlapping claims and a precedence rule nobody
   can predict.
2. **A ticket spans exactly one workspace.** The alternative — running both
   workspaces' verify for a straddling ticket — is expressible but hides a
   scoping mistake instead of surfacing it. Refuse at ingest; the fix is to
   split the ticket, which is the right shape anyway.
3. **A file in no workspace blocks rather than warns.** This is the whole point.
   A warning here is what the catch-all already was.
4. **Verify stays whole-workspace, not per-ticket.** Same trade as decision 2 of
   `LANGUAGE-COVERAGE.md`, one level down: slower than running only the ticket's
   own files, and it is what keeps cross-ticket blame honest.
5. **The canary runs at preflight, not per attempt.** Once per run per workspace
   per language. Per attempt would be more correct and far too slow.

---

## Phases

**1 — Config and resolution. Done.** Parse `workspaces`, validate roots exist
and are unique, implicit-single-workspace fallback, longest-prefix file
resolution, `cwd` threaded through `_shell`. No behaviour change for any
existing config. *Tests: every current config shape loads and means the same; a
two-workspace config resolves each file to the right root; overlapping roots are
a config error; a ticket's workspace is derived from `allowed_files`.*

One thing the implementation settled that the spec did not say: the implicit
root workspace is **derived on each access, not stored**. Storing it aliased a
dict, so `config.commands = {...}` — which the CLI and several tests do — left
the workspace holding the block that had been replaced, and the loop verified
against commands nobody had configured any more. There is one copy of the truth
and no way to update half of it.

**2 — Attribution. Done.** Re-root paths parsed from a workspace's output
before matching, in `_shell` rather than at each reader, so `signatures`,
`files_blamed`, `errors_naming`, the stored step detail and the executor's
prompt cannot disagree about which file a failure is about. **Shipped with
phase 1, not after it** — phase 1 alone moves `cwd` and silently breaks blame.
*Tests: a failure reported relative to a workspace root attributes to the right
ticket; the same failure un-rerooted attributes to nothing, which is the
regression this prevents; an absolute path, a path already repo-relative, and a
path that is not a file here are all left alone; the toolchain's own separator
survives.*

**3 — The preflight canary. Done.** Per workspace, per language:
red-on-failing-test and the-failure-names-the-file. A workspace failing either
blocks the run with a note naming which check failed and the one command that
resolves it. *Tests: a command that ignores the language is caught; a command
that runs it passes; a tree that is red for its own reasons is told apart from
a gap by re-running without the canary; the canary is removed on every path
including an exception; a stale one from a killed run is cleared and reused;
somebody else's file at that path is never overwritten.*

Three things the implementation settled that the spec did not say, all three
found by running it rather than by stubbing it:

**The canary is scoped to the backlog, not the tree.** The first version asked
about every language present in the repository, and blocked a run over a single
Python helper script sitting beside a `project.godot` — `.py` present, nothing
running it, no ticket that cares. That is `LANGUAGE-COVERAGE.md`'s "stalling a
backlog over build.sh" with a louder stop. What the tickets declare they will
write is the set whose verification has to mean something.

**The canary body is pure ASCII with no quote characters.** It was prose first,
and CPython reported `unterminated string literal` at the apostrophe in
"project's" — an error about line 3 instead of the deliberate garbage on line
1, saying nothing about what the check is. An em-dash in it came back as U+FFFD
through the subprocess decode on top of that. Every line now begins `@@@`.

**Attribution is a textual mention, not `errors_naming`.** That function reads
locations out of diagnostic *blocks*, which is right for a failing assertion
and wrong for a file that will not parse: `compileall` names the file twice, in
`*** Error compiling 'tests\x.py'...` and in `File "tests\x.py", line 3`, and
`errors_naming` found neither — reporting a runner that had plainly said which
file as unable to attribute. The claim being checked is only ever "the command
told us which file".

`preflightCanary` is also deliberately **not** coupled to `preflight`. That one
probes the models; this measures the tree, and somebody who skips the model
probe because they just ran `forge doctor` has said nothing about whether their
test command reads their code.

**4 — Gates and reporting. Done.** Unowned files block at ingest and at the
ticket; a ticket straddling two builds is refused the same way; `doctor` prints
per-workspace matrices plus the unowned-files list; `_TEST_ROOTS` and
`_example_test` scope to the workspace. *Tests: the Puzzle Path backlog is
refused at ingest; a correctly configured two-build repo is not; a repository
declaring no workspaces is never refused; the uncovered-language gate asks the
file's own build rather than the repository; another build's suite is not
offered to a ticket as the convention to follow.*

Two notes on what the gates ask, both of which tightened an existing check
rather than adding one:

**`_uncovered_languages` now resolves through the file's own workspace.** It
asked `config.covers(...)`, which answers across every build — so one
workspace's runner answered for another's files, which is the absorption this
whole feature exists to stop, surviving inside the gate meant to catch it. The
`Config`-level methods keep their repository-wide meaning for the callers that
genuinely have no path to ask about; every caller that holds one now asks the
build.

**`.gd` joined `_SOURCE_SUFFIXES` and `_CODE_SUFFIXES`.** `forge doctor`
reported "(no source files)" over a repository full of GDScript and no gate
asked anything about it, because the tables had never heard of the extension.
That is the same shape as the Godot launcher answering for TypeScript: a
language nothing in the loop can see is a language nothing in the loop can
check. It is also the language of the repository this whole document is about,
which is how it went unnoticed for four phases.

**5 — Detection at init. Done.** Tree walk for manifests
(`toolchain.discover_workspaces`), per-workspace `toolchain.detect`, wizard
confirmation, and `forge toolchain --workspace`. *Tests: a single-build
repository is never asked; two builds are proposed with the root first;
generated directories are never proposed; detection reads each build's own
directory; nothing is written without `--accept`; declining keeps one set of
commands.*

One defect this closed rather than added. `forge toolchain` wrote into the
top-level `commands`, which under a config that declares `workspaces` is read
by nothing — so the write succeeded, printed its confirmation, and changed no
behaviour at all. It writes into the named build now, and refuses to guess
which one when a repository declares several.

`BUILD_MANIFESTS` is deliberately narrower than `EVIDENCE_GLOBS`. That list
answers "where does this project write its commands down", which a README does;
this one answers "where is there a build", which a README does not. `Makefile`
is absent for the same reason from the other direction — it marks a build often
enough to tempt, and sits at the root of repositories with no subprojects at
all, so it proposes the workspace that already exists and nothing else.

Phases 1–4 are what turn the Puzzle Path run red. Phase 5 is what stops the next
repository being misconfigured in the first place.

Worth noting what phase 5 cannot do for the run this document is about:
discovery finds `project.godot` and nothing else, because the level editor
never had a `package.json` — no ticket owned one, which was the third root
cause in the postmortem. Discovery proposes builds that exist. A backlog
writing into a build nobody created is caught by the unowned-file gate and by
the canary, not here.

---

## Risks

**Phase 1 without phase 2 is worse than neither.** Moving `cwd` breaks path
attribution, and broken attribution excuses every failure. This is stated twice
in this document on purpose.

**Workspace sprawl in a monorepo.** A repository with forty `package.json` files
should not produce forty workspaces, each running its own suite every attempt.
Detection should propose only roots that a ticket could plausibly own, and the
verify plan should skip a workspace no file in the backlog touches — the same
"no files present, nothing to say" rule `_verify_plan` already applies per
language.

**A wrong workspace root is invisible.** `root: "tools/path-forg"` (typo)
resolves nothing, so every file falls to `.`, and the config looks fine. Validate
at load that each root exists and contains at least one file, and report the
resolution in `doctor` rather than only the commands.

**The unowned-files list is noisy on first adoption.** A real repository has
stray `.sh`, `.ps1` and config-adjacent scripts that no build owns. The
`false`/`"skip"` exemption from `LANGUAGE-COVERAGE.md` has to work at the
workspace level too, or rule 3 stalls a backlog over `setup.sh`.
