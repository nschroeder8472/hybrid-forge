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

## A wrong guess is a measurement, not a dead end

The first ticket is a hypothesis: *this code, here, is what produces that
symptom*. When the reproduction cannot be written — the test passes against the
named code, or the tester refuses because that code already does what the
report asks for — the hypothesis has been **disproved**. The report has not.
Somebody saw the behavior they described.

So the planner is asked for a different cause, with three things in front of
it: the report unchanged, what disproved the last explanation, and every
hypothesis already ruled out — so the next guess cannot be the last one again.
The ticket is rewritten around the new cause, the old one is dropped, and
reproduction runs again.

This is what one real run needed and did not have. A report said the game
starts at level 0. The Rust set it to 1, so no test of that code could fail; the
loop parked and told the reporter to sharpen a report that was accurate. The
answer was one layer away, in a JavaScript entry point that threw before it ever
read the level. A re-diagnosis is the step that gets to look there.

`loop.bugHypotheses` bounds it — 3 by default, `1` to park on the first wrong
guess. When the budget runs out, or when the planner has nothing better than
another guess, the ticket parks with **every hypothesis it tried** written into
the block, because that is the work it did and the next person should not repeat
it.

### A ruled-out cause stays ruled out

Respec runs between retry cycles and rewrites a ticket that keeps failing. It
judges a revision against `original_spec` — the human's text — because every
revision is derived from the last, and without a fixed point the loop drifts
away from the plan one plausible step at a time.

A re-diagnosed bug ticket breaks that rule, because there `original_spec` is
*the first hypothesis* — a cause the loop has already disproved by running a
test. Anchoring on it inverts the whole mechanism. The run above got as far as
`BUG-001: reproduced. tests/bug_001_test.rs fails against the code as it
stands` in `web/main.js`, and the next respec reverted it, reasoning:

> the previous revision drifted into build/JS paths, but the original intent and
> all failures point to a Rust initialization

Scope went back to `src/lib.rs` and the executor blocked, because the code it
had been told to fix was no longer inside it.

So once a hypothesis has been ruled out, the anchor becomes the **report** —
which no revision rewrites — and the current spec is the live theory rather than
drift to be undone. The disproved specs are shown to respec as dead ends it may
not propose again, and a revision that re-proposes one anyway is refused whole,
scope included. A reproduction that would not fail against a cause is the
strongest evidence available that the cause is not where the bug is; it is not
a reason to go back.

---

## What a ticket may read, and what it may write

These are different permissions and used to be granted as one. The planner named
`src/lib.rs` for both, so every role saw one 62-byte file — and in that crate
`lib.rs` is four `pub mod` lines, with `Game` in `src/game.rs`. The executor's
last word was that the struct it had been told to fix "is likely defined in
`src/game.rs` ... outside the allowed scope I'm permitted to modify", which was
exactly right and reached nobody.

**Writable scope stays narrow.** That is the one worth being strict about, and
nothing below widens it on its own.

**Read scope is widened around it**, to the modules a module-list file declares,
the files the report's own words grepped to, and the source siblings in the same
directory — capped, because a read scope of forty files is a directory listing
nobody reads carefully.

**A scope that is only re-export files is called out at filing time.**
`lib.rs`, `mod.rs`, `__init__.py`, `index.js` declare modules and hold no
behavior to fix. Judged on contents, not on the name: an `__init__.py` with real
code in it is a fine thing to scope a ticket to.

**A block that names a file is answered rather than filed.** The executor is
told `BLOCKED:` names the file it needs and "can widen the ticket". Now it does:
a path named in the block that **already exists in the repository** is granted
once, and the attempt is not charged. Existence is what makes that safe without
a human — a model cannot invent its way into scope — and `neverDelegate` is
enforced here exactly as everywhere else. A ticket that blocks a second time
after getting what it asked for is saying something a person should read.

**A test file is never granted this way.** Whatever the block says, however
reasonable the request. The party being judged does not get write access to the
assertion judging it; that is settled below.

---

## When an older test asserts the bug

The founding problem, in its purest form. From a real run:

```rust
// tests/tt_001_test.rs:87, written by an earlier ticket
assert_eq!(piece::color(kind), (kind as u8) + 1);   // so color(0) == 1
```

A report then says the I-piece renders black, and `color(0)` should be `255`.
The two assertions are opposites; both cannot hold. The fix landed, the
reproduction passed, and the suite failed on a file the ticket could not touch —
so the attempt scored as a failure and the executor was asked again, five times,
for an edit that cannot exist. It ended `gave up after 5 attempts`, which reads
as a fix nobody could write rather than a contract nobody can satisfy.

This is what the reproduce-first design is *for*: a ticket that writes both the
code and the assertion judging it will encode its bugs as passing tests. TT-001
did exactly that. What was missing was any way to undo it.

**Detected, not inferred.** Three conditions, all required. The ticket is a bug
ticket, so a reproduction exists to be the contract. That reproduction **passes**
— otherwise the fix is simply not working yet. And what fails is a **test file
outside the ticket's scope**; a broken source file is an ordinary regression the
executor should fix, and treating it as a contradiction would make this a way to
widen scope by breaking things.

The ticket then blocks *immediately* rather than at attempt five, with both
demands and their locations in the note.

**Retiring the assertion is argued, not asserted.** At respec the contradiction
is put to the planner, which may propose adding the test file to
`allowed_files` — or reply `impossible`, which is the right answer whenever the
report contradicts something the project deliberately decided.

Proposing is as far as respec gets. Its job is making a failing ticket pass,
which makes it the wrong role to also rule that the assertion in its way is
wrong, so the scope is held back and put to the **reviewer**, which gains
nothing from the ticket going green. It must answer `GRANT:` or `REFUSE:` and
then make the case: name the file, quote what it asserts, say what the report
claims instead, and say which is right and how it knows. A `GRANT:` that never
names the file, or that runs to two lines, is recorded as a refusal — *"the
ticket cannot pass otherwise"* is true of every contradiction and settles none
of them. The argument goes into the run log verbatim either way, because what a
person wants later is not that scope changed but why somebody thought the old
assertion was wrong.

A refusal leaves the ticket parked with the contradiction in its note. Two
demands disagree and nothing here could tell which is right, which is a fine
thing for a loop to say.

---

## What it refuses to do

**Fix a bug it could not reproduce.** Once the hypotheses are spent, the ticket
parks rather than fixing on faith, with the last test on disk to start from. The
green that would follow a guessed fix means exactly what the green that shipped
those two defects meant.

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

**Reports filed back to back land on one backlog.** A report joins the open
backlog when there is one — the newest run nothing has been spent on yet,
whether that run came from `forge bug` or from `forge ingest` — and is appended
to the end of its reading order.

```
forge bug "the score stops updating after I clear a line"
forge bug "rotation clips the wall on the right edge"   (added to run 4 — 2 ticket(s) waiting)
forge go                                                 works both, in the order filed
```

A run already in flight is never joined, because a ticket appended behind the
orchestrator's position is one it has already walked past. A report filed
against one opens a run of its own — and is still worked, because **`forge go`
drains its whole queue, oldest first**. That is the other half of the same fix.
Every command that files work opens a run — `ingest`, `bug`, `go --plan`,
`retry` — and the loop used to take the highest id and stop there, so a bug
filed while an earlier backlog sat blocked waited for a human to clear the block
first. `forge status` shows one run too, so the stranded ticket was not on
screen to be noticed.

Blocked is no longer a reason to abandon what is behind it; the runs in the
queue are separate work. **Stopped** and **failed** are: the first is a person
asking the loop to stop, and the second means something outside the backlog is
wrong and the next run would hit it too. Either breaks off the drain and says
how many runs were left untouched.

`maxRuntimeSeconds` caps the queue, not each run in it — it means unattended
wall-clock time, and a fresh clock per run would let three runs spend three
times the cap.

A bug whose scope touches a `neverDelegate` path is routed
`withheld:never-delegate` and left for a person, on the same reasoning that
governs the build loop: a defect in code the project marked off-limits is
exactly the kind that wants a human. That reason is the one the harness can
prove — a glob matched — which is why it is spelled out rather than left as
`unspecified`.

---

## Trying it

The two defects above are still in the test project, deliberately unfixed. They
are the honest first test of this feature: a plain-language report, a fix, and
the existing suite still passing afterwards.

```bash
forge bug "after the tab is in the background for a few seconds, switching \
back locks three pieces in a row instead of one"
```
