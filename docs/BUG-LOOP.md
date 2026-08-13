# The bug loop

`forge bug "<report>"` takes a plain-language report and lands a fix. It is the
same daemon, the same roles and the same state machine as the build loop — with
one inversion that changes what a green ticket means.

```
forge bug "pieces sometimes drop three at once after I switch tabs"
forge go
```

---

## Why it is not just a ticket

The build loop verifies what the criteria say. That has a hard edge: a defect
nobody wrote a criterion for survives the whole pipeline — verification, review
and all. Two did. One run finished with six tickets `done`, lint and typecheck
clean and 36 tests passing, over a `Game::tick` that locks three pieces on a
long frame and a rotation with no wall kicks. Neither is a spec violation.
Both are bugs, and every check the loop had agreed the work was finished.

A bug ticket cannot be judged that way, because the contract does not exist yet
when the ticket is written. So the loop writes it first, and refuses to accept a
fix for a fault it never saw.

```
REPRODUCE  tester writes a test asserting the CORRECT behavior
           the test command runs it — it must FAIL
BUILD      executor fixes the cause; it cannot edit the test
VERIFY     lint / typecheck / test, with that test now passing
REVIEW     reviewer reads the diff against the failure it produced
```

The reproduction is the deliverable as much as the fix is. It stays in the repo
afterwards, which is what stops the bug coming back.

---

## Scope comes from evidence, not from a plan

A plan states which files a ticket may write. A report does not — the file that
needs changing is exactly what is being looked for, and "pieces drop three at
once" names none of it.

So the harness gathers evidence first, and the report decides what kind.

**A report that names something.** `Game::tick`, `src/game.rs`, a backticked
span — those are grepped directly, because a reporter who named a symbol has
already said more than any heuristic will work out.

**A report that names nothing.** "The score sometimes stops updating after I
clear a line" contains no identifier and no path, and this is the ordinary case.
Two things fill the gap. Its *content words* are grepped instead — stopwords
dropped, ordered by how often the report repeats them, because a score is
usually near something called `score`. And the definitions in the project are
listed, which is the bridge from the report's words to the code's names: "score
stops updating" matches no line in a codebase that calls it `commit_lines`, but
a reader can see where lines and score meet.

Either way the project's file list comes too — **tracked and untracked**, which
matters more here than anywhere else: `autoCommit` is off by default, so a
project the loop has just built is entirely uncommitted, and a search that only
knew about tracked files found nothing at all in it. Ignored paths are still
skipped, and a project with no git at all is walked directly, minus the
directories a `.gitignore` would have named.

**Then it reads.** A first pass hands the planner that evidence and asks one
question: which files are worth opening? The harness reads the ones it names —
filtered to paths that actually exist, so an invented one cannot be silently
read as nothing — and the *second* pass writes the ticket with those files in
front of it. That is the difference between a ticket scoped from filenames and
one scoped from the code, and it is why the spec can describe the defect rather
than restating the symptom.

The survey is best effort: a planner that answers with nothing usable costs a
call, and the ticket is still written from the file list and the grep hits.

The planner names `allowed_files` and `reference_files` and states what the code
should do instead. All of it is gathered by the harness rather than by a model
with tools, for the same reason `toolchain.py` does it: it works identically
behind every adapter, sends exactly what we can name, and needs no tool grant.

A report the planner cannot place in the repository at all stops there, rather
than becoming a plausible ticket scoped to files that do not exist.

---

## What it refuses to do

**Fix a bug it could not reproduce.** If the test passes against the code as it
stands, it is asserting something the fault does not touch — or asserting the
fault itself. The tester gets one more attempt with the passing output quoted
back, and then the ticket parks with the test on disk to start from. A bug
nobody can demonstrate is a bug the loop would be fixing on faith, and the green
afterwards would mean exactly what the green that shipped those two defects
meant.

**Guess at a report it cannot turn into an assertion.** The tester may reply
`BLOCKED:` naming what it would need to know. That is an answer, not a failure —
a test written from a guess proves nothing and is then trusted by everything
downstream.

**Let the fix touch its own proof.** The reproduction is outside the ticket's
scope, enforced before anything reaches disk, and the executor is told so.

**Excuse the reproduction at verification.** Every ticket takes a baseline first
and is not blamed for breakage that pre-dates it. On a second cycle the
reproduction is already on disk and already failing, which fits that description
exactly — so it is excluded by name. Otherwise a bug ticket could pass with the
bug still in place, which is the failure this whole path exists to prevent.

**Delete the evidence.** An unverified feature test is removed when its ticket
fails, because verification is whole-project and an abandoned assertion fails
every later ticket. A reproduction is kept: it is the one assertion here that
was demonstrated against real behavior. It is safe to leave failing — every
other ticket's baseline treats it as breakage outside its own scope.

---

## What it needs

A **test command**. A project with none cannot reproduce anything, and the
ticket says so rather than proceeding: there would be nothing to run the proof,
and the fix would be checked by reading. See
[CONFIG.md](CONFIG.md#commands).

A report with **something checkable in it** — though not a location. Naming the
file is welcome and never required; that is what the survey pass is for. What
cannot be worked around is a symptom no test could assert: the reproduction has
to compare a value, a count, an ordering. "It feels slow" defeats it. "The score
stays at zero when I clear a line" does not.

---

## The command

```bash
forge bug "pieces sometimes drop three at once after I switch tabs"
forge bug --file report.md
git log -1 --format=%B | forge bug -          # or from stdin
forge bug "..." --go                          # file it and start the loop
```

It files one ticket as `BUG-nnn`, counting across every run in the repository —
the id names the reproduction's filename, and reusing it would overwrite the
evidence for a bug nobody said was fixed. The ticket is written to
`.hybridforge/tickets/` like any other, and the scope it chose is printed for
you to read before `forge go` spends anything.

A bug whose scope touches a `neverDelegate` path is routed `claude-only` and
left for a person, on the same reasoning that governs the build loop: a defect
in code the project marked off-limits is exactly the kind that wants a human.

---

## Trying it

The two defects above are still in the test project, deliberately unfixed. They
are the honest first test of this feature: a plain-language report, a fix, and
the existing suite still passing afterwards.

```bash
forge bug "after the tab is in the background for a few seconds, switching \
back locks three pieces in a row instead of one"
```
