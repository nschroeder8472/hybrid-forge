# The sample project

A repository small enough to run a whole forge loop against in minutes, and
real enough that a change to the loop meets the things a real run meets: two
builds, a spec on the parsed path, a dependency between tickets, a green
baseline, and a bug the suite does not catch.

It exists so a change to the loop can be *run*, not only unit-tested. Most of
what this project has learned came from watching a real backlog fail; this is
the cheapest available imitation of that.

## Run it against a copy, never in place

A run writes code, a database, and artifacts. Copy the fixture out of the
repository first — the committed tree is a fixture, and a run that edits it in
place turns the next test into a report about the last run:

```
python scripts/sample_workspace.py            # copies to a temp directory
python scripts/sample_workspace.py /tmp/try   # or somewhere you name
```

It prints the path it wrote and the first command to run there.

## What is in it

```
wordcount/counter.py        count_words — the root build's code
tests/counter_test.py       its suite, green, punctuation-free on purpose
.flake8                     the root build's grading, max-line-length 88
plugin/histogram/bars.py    a second build, its own manifest, its own suite
plugin/tests/bars_test.py
plugin/.flake8              its own copy, resolvable from its own root
SPEC.md                     three tickets for `forge ingest`
HARD.md                     one exacting ticket that lands first try
STALL.md                    one ticket that cannot succeed, for the brakes
GRIND.md                    two tickets aimed at the middle; both land, and
                            one is refused by ratify before it is built
OPAQUE.md                   one ticket that withholds the rule deciding its
                            answer, and the two ways the loop restores it
BUG.md                      one report for `forge bug`
.hybridforge/config.json    two workspaces, per-language commands
```

The two builds are the point of the layout. `plugin/` has its own manifest, its
own tests directory and its own linter config, so the loop has to resolve each
ticket to the build that owns it and run that build's commands from that
build's directory. A change that breaks workspace resolution shows up here as a
command run from the wrong place, not as a subtle test failure.

The linter is why each build carries a `.flake8` of its own rather than sharing
one above them. `toolchain_context` resolves a config by walking from a
writable file up to its *workspace* root, so a shared file outside that root is
one the roles working in the build are graded against and never shown — which
is the exact failure Feature 1 of `docs/CONVERGENCE.md` exists to stop.

## The four things to run

```
forge --root . doctor      what runs against each language of each build
forge ingest SPEC.md       three tickets, parsed — no planner model runs
forge go                   the loop
forge bug --file BUG.md    the reproduce-before-fix path
```

`forge doctor` is the one to run first after any change to coverage,
workspaces, or the canary: it prints the matrix without spending a token.

`STALL.md`, `HARD.md`, `GRIND.md` and `OPAQUE.md` are each ingested *instead
of* `SPEC.md`, in their own copy, when a change touches retries, respec,
convergence or how a ticket is parked.

- `STALL.md` must end **blocked**, and the note it leaves has to name the real
  problem — its first two runs ended `done` over a criterion nobody had met,
  which is what the three guards in `CONVERGENCE.md` were written from.
- `HARD.md` must end **done**, and on the first attempt. It is the control
  case: a well-specified ticket needs none of the convergence machinery, and a
  change that makes this one take two attempts is the change to look at.
- `GRIND.md` must end **done**, both tickets, on the first attempt each — and
  the sign-off pass must refuse `GR-002` on its first pass, over a rounding
  rule that cannot produce one of its own criteria. Two runs, and the second
  reproduced the first down to three of four delivered files byte-identical. It
  was written to reach the middle of `CONVERGENCE.md` and did not: the defect it
  carries is one ratification repairs before a build call is spent. Read the
  ratify notes rather than the verdict.
- `OPAQUE.md` must end **done**, and the sign-off pass must refuse it on its
  first pass, naming the rule the ticket withholds. It was written to fail
  *slowly*, by keeping the rule that decides its answer out of the prompt — and
  three runs say the loop will not have it: `reading_scope` hands over the
  neighbouring file, and when it cannot, ratification blocks and asks for it in
  writing. Read `ratify_notes`, not the verdict.

`forge ingest` should report **parsed**, not planned. If it says planned, the
spec grammar changed and `SPEC.md` no longer matches it — which is itself the
finding.

## The invariants this fixture keeps

The forge suite pins all of these in `tests/test_sample_project.py`, so the
fixture cannot rot quietly:

- **Both suites pass on the committed tree.** `requireGreenBaseline` stops a
  run over a red tree, so a fixture that ships red cannot be run at all.
- **The spec takes the parsed path** and yields exactly three tickets, one of
  which depends on another, one of which lives in the second build.
- **Every ticket names its own test file** in `Allowed files`. A ticket without
  one gets a test written outside its scope, and then the executor is refused
  every time it tries to repair it.
- **Every path the spec names belongs to a build.** An unowned file is refused
  at ingest, which is correct and would make this fixture useless.
- **`count_words` still mishandles punctuation.** That is the seeded defect
  `BUG.md` reports, and it is deliberately not fixed. Fix it in your copy, not
  here.
- **Both builds lint clean, by their own configured command.** `lint` was
  `skip` for `.py` in both workspaces until 2026-09-01, which meant nothing the
  loop generated here was ever graded mechanically — and the run every
  convergence feature was derived from failed overwhelmingly on lint output.
  A fixture that cannot fail that way cannot exercise the brakes.

## Keeping it clean

The fixture is 20 files and nothing else. A run writes several more —
`wordcount/report.py`, two test files, a database, a tickets directory, an
artifact tree — and every one of them is ignored rather than named, by an
allow-list in the repository's `.gitignore`:

```
examples/sample-project/**
!examples/sample-project/**/
!examples/sample-project/*.md
… one line per committed file
```

So a new step that writes a new file is ignored by default instead of being
committed the next time somebody stages everything, and adding a file to the
fixture means adding a line there. `git add -f` is the escape hatch.

Two things that ignore rules cannot do, and the guard suite does instead:
`TestTheFixtureIsOnlyTheFixture` fails when the working tree holds a file that
is not the fixture's, and pins the four paths `SPEC.md` names that must *not*
exist yet — a ticket whose files all exist is one the loop can satisfy by
changing nothing. A modification to a tracked file is not ignorable at all, so
check `git diff -- examples/` before committing if a run has been near it.

## Models

The config points every role at a local `llama.cpp` server on
`http://127.0.0.1:8080/v1`. Change the endpoint, the model name, or the roles
in your copy — nothing in the fixture depends on which models you bring. The
commands, the spec, and the layout are what it is for.
