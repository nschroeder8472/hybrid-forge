# Loop invariants

Rules that hold across the whole harness, extracted from the bug-loop repair of
2026-08-17. Every one of them was learned by breaking it, and most were broken
*again* while fixing something else — which is why they are written down here
rather than left in the commit that fixed each one.

Read this before adding a step, a role, or a check that attributes blame.

---

## 1. Reading and writing are different permissions

`allowed_files` is the ticket's contract. `reference_files` is what it can see.
Granting them as one list is how a role ends up diagnosing through a keyhole.

The case: a ticket scoped to `src/lib.rs`, which in that crate is four `pub mod`
lines and 62 bytes. Every role saw that file and nothing else, and the executor's
final answer was that the struct it had been told to fix "is likely defined in
`src/game.rs` ... outside the allowed scope I'm permitted to modify". It was
right, and it had no way to check.

**Widen reading freely; keep writing narrow.** `evidence.reading_scope` does the
first, and is wired into every path that sets a ticket's scope — `forge ingest`,
`forge bug`, and re-diagnosis. It costs nothing on a greenfield plan, because it
keeps only files that exist.

A corollary worth stating separately: **a scope that is only re-export files is
a mis-scope.** `lib.rs`, `mod.rs`, `__init__.py`, `index.js` declare modules and
hold no behavior to fix. `evidence.is_module_list` judges that on contents, not
on the name — an `__init__.py` with real code in it is a fine thing to scope a
ticket to.

---

## 2. Provenance decides what is immutable, not position

The loop protects a human's intent from its own drift. That protection is only
correct where the thing being protected actually came from a human.

`_merge_criteria` already had this right: criteria are scoped by who wrote them,
because a blanket freeze made a machine-invented criterion as immutable as a
human-authored one. The spec anchor did not get the same treatment, and it cost
a run.

Respec anchors on `original_spec` and is told "the original is the intent; treat
any difference as drift you should undo". On a **re-diagnosed bug ticket**,
`original_spec` is the *first hypothesis* — a cause the loop already disproved by
running a test. So the ratchet dragged the ticket back to the explanation it had
just ruled out. It re-diagnosed to `web/main.js`, reproduced the bug there, and
the next respec reverted the scope on the reasoning that "the previous revision
drifted into build/JS paths, but the original intent points to a Rust
initialization".

**Before treating any stored field as an anchor, ask who wrote it.** If the loop
did, it is a hypothesis and the anchor is somewhere further back — for a bug
ticket, the report, which no revision rewrites.

---

## 3. The party that benefits does not rule

Respec's job is making a failing ticket pass. That makes it the wrong role to
also decide that the assertion standing in its way is wrong.

So respec *proposes* retiring a stale assertion and the **reviewer** rules on it,
because the reviewer gains nothing from the ticket going green. Same shape
wherever a role would otherwise grade its own work: the executor does not author
the tests it is judged by, and the tester writes a bug's reproduction before any
fix exists.

**And an argument has to be enforced, not requested.** "Instruction-following is
not an access control" is already written in three places in this codebase about
*permissions*; it applies equally to *reasoning*. A `GRANT:` that never names the
file, or runs to two lines, is recorded as a refusal — because "the ticket cannot
pass otherwise" is true of every contradiction and settles none of them. The
argument goes to the run log verbatim either way: what a person wants later is
not that scope changed but why somebody thought the old assertion was wrong.

---

## 4. "Mentions the path" is not "is blamed for the path"

The most expensive bug of the repair, and it was in a helper three call sites
shared.

`errors_naming` scanned raw tool output for a filename. Every test runner
announces the targets it is about to run, and cargo does it by path:

```
     Running tests\bug_001_test.rs (target\debug\deps\bug_001_test-737.exe)
```

That line is printed whether the target passed or failed. So a ticket was found
in its own **success banner**, the amnesty concluded "the reproduction itself is
failing — not excused", and the ticket was failed **fifteen times across three
cycles** for a different ticket's red reproduction while its own was green.

**Attribute from diagnostic blocks, never from raw text.** `failures._blocks` is
the only correct input for any question of the form "is this file implicated".
`errors_naming` and `files_blamed` both read blocks now.

---

## 5. Every attribution needs the baseline

`_attempt` computes `already` (failing before this ticket started) and
`introduced` (this ticket's doing). Any new check that asks "is this file
implicated" must consume the same baseline, or it re-blames the whole repository.

The contradiction detector shipped without it and reported a *level* bug scoped
to `src/game.rs` as contradicted by an assertion about piece geometry — which had
been failing since before the ticket was filed, one line under an amnesty log
saying exactly that.

`files_blamed(output, exclude=already)` is the shape. It shares `_block_key` with
`signatures` so both agree on what counts as the same error; they have to, since
one is producing the set the other filters by.

---

## 6. A prompt is a promise the harness has to keep

The executor has been told for a long time that `BLOCKED:` "names the file you
need and why ... can widen the ticket". Nothing widened anything. The note
reached a human and the ticket parked, with the sentence naming the missing file
sitting in the block being read by nobody.

**Grep the prompts for capabilities before assuming they exist.** Where the
promise is worth keeping, keep it: `_widen_scope` now grants a named path that
already exists in the repository, once, without charging the attempt. Existence
is what makes it safe without a human — a model cannot invent its way into
scope — and `neverDelegate` is enforced exactly as everywhere else.

**A test file is never granted this way**, whatever the block says. The party
being judged does not get write access to the assertion judging it, and phrasing
the request as a block does not change that. That path goes through §3.

---

## 7. Recover the content before refusing the format

A reply that arrives unreadable may still contain the right answer.

The reprompt assumes the model misunderstood the format. Often it did not — it
reasoned at length about a hard ticket, quoted the current code in one fence,
emitted the whole corrected file in another, and left off the path line. Asked
again it produced *the same shape*, because the reasoning is what filled the
reply. Three of one ticket's five attempts, six of another's nine, every one
carrying a complete correct file.

Recovery is only safe with structural evidence, never on shape alone:

- The destination must be unambiguous — exactly one writable file, so nothing
  is guessed.
- The block must retain **80% of the top-level lines already on disk**. Measured
  against real replies the quoted fragment scored 17% and the whole file 100%,
  and one real reply held *only* fragments — writing it would have deleted three
  items from the file with a successful apply and nothing in the log.
- **Never** into a file that does not exist. With nothing to compare against,
  an illustrative snippet becomes the whole contents of a new module.

---

## 8. One consumer taking the newest strands every other producer

Four commands open runs — `ingest`, `bug`, `go --plan`, `retry` — and `forge go`
took the highest id. Anything filed behind a run that then blocked waited for a
human to notice it, and `forge status` shows one run too, so it was not on screen
to be noticed. Two bug reports filed a minute apart: the first sat `pending`,
`attempts: 0`, for four days.

**A queue with several producers needs a consumer that drains it.** `forge go`
works every resumable run oldest-first, and `blocked` no longer abandons what is
behind it — only `stopped` (a person said so) and `failed` (something outside the
backlog is wrong) break off.

The same change breaks anything that assumed "newest run is the live run". The
dashboard did; it now follows the run the loop recorded itself entering, and
falls back to newest when that one is terminal.

---

## 9. Validate against recorded artifacts, not against reasoning

Every fix in this repair was replayed over the real step output in
`.hybridforge/artifacts/` and the `steps` table before it was believed. **Two of
them were wrong on the first attempt and only replay caught it:**

- The contradiction detector asked `errors_naming` whether the reproduction was
  implicated, and found it in its own `Running` banner — so it concluded the fix
  was not working and detected nothing. Synthetic fixtures passed.
- The unlabeled-file recovery first accepted a reply whose only block was a
  fragment. Replay showed it would have destroyed `src/piece.rs`.

A unit test asserts what you thought the output looked like. The artifacts hold
what it actually was. Write the fixture **from** the recording — the banner line
is in the regression test now because it is what broke the first attempt.

`forge replay` is this made routine:

```bash
forge replay                     # re-read every recording with today's parsers
forge replay --changed           # only what now reads differently
forge replay --run 3 --lens parse
```

It runs the current parsers over `.hybridforge/artifacts/` and, where the run
recorded what the parser produced at the time, says whether the answer changed.
Two lenses: **parse** reads model replies as files and compares against the
`written` list in the `apply` record beside them; **blame** reads command output
as diagnostics and compares `signatures(output) - pre_existing` against the
`introduced` set the run recorded. Exit status is 1 when anything reads
differently, so it drops into a pre-commit hook.

A difference is not automatically a regression. It is the set of past output
your change alters the reading of, which is the set worth looking at by hand.

Read-only, and honest about what it cannot reconstruct. Runs without artifacts
fall back to the `steps` table, which is clipped at 20k and records nothing
about what was read out of it — those can be re-read but not checked. Recorded
signature sets are clipped at twenty and say so rather than reporting a
difference nobody can act on. And the recovery of an unlabeled reply depends on
the ticket's scope and the file's contents *as they are now*, so it is labelled
rather than presented as what the run would have done.

Its own first real run is the argument for it: it reported three parser changes
that were not parser changes, because it paired each `apply` with the first
reply of the attempt rather than the nearest preceding one. An attempt holds
several replies and one apply. The tool caught that about itself, and the
pairing rule is now `pair_applies` with a test named for it.

---

## What is bug-loop-specific and what is not

| Mechanism | Scope |
|---|---|
| `failures._blocks` / `_ERROR` / `_CONTINUATION` | every step, every language |
| `errors_naming` reading blocks | all three call sites |
| `_widen_scope` on a blocked ticket | every ticket |
| `_recover_unlabeled` | every ticket with one writable file |
| `evidence.reading_scope` | ingest, bug, re-diagnosis |
| `resumable_runs` draining | every command that opens a run |
| Reproduction, re-diagnosis, contradiction | bug tickets only |
