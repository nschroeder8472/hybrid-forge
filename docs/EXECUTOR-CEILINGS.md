# What stops the executor on a repository of ordinary size

Every fixture in `examples/sample-project` holds files of one and two
kilobytes. This repository's are a hundred. Between 2026-09-08 and 2026-09-09,
four runs of one ticket against this tree found four different ceilings, each
hidden behind the last, and none of them reachable from the fixture.

This is the record of what they were, what was done, and what is still open.
Written because the findings cost roughly 4.2M tokens to obtain and are worth
more than the code they produced.

---

## The four ceilings, in the order they appeared

### 1. Whole-file output — fixed

The executor returned **whole files**, so the cost of an edit was the size of
the *existing file*, not of the change. `forge/state.py` is 96,548 characters
(~24,137 tokens to restate) and `forge/cli.py` is 110,903 (~27,725), against an
executor budget of 16,384 of which roughly 6,144 goes to reasoning.

**RT-001 was impossible before its first call, and nothing checked.** Run 8:
three attempts, two retry cycles, 36 calls, **2,647,873 tokens, zero files
written**. The loop's own advice — *"emit the same implementation in fewer
output tokens — fewer files per response"* — cannot be taken when one file
exceeds the budget alone.

**Fixed** by the replacement block: a body containing `<<<<<<< SEARCH` is read
as edits rather than a file. The outer shape is unchanged, so path handling,
double-fence unwrapping and the long-fence rule all still apply. Two rules
carry the safety — a search must match **exactly once** (a block matching twice
and applied to the first corrupts a file quietly, which is worse than the
failure it replaces), and every replacement lands on one in-memory copy written
once, so a refusal partway through leaves the file untouched.

`FileEdit.content` stays populated with the replacement halves, because
`foreign_bindings`, `laundered_assertions` and `weakened_criteria` all read it
and an empty field would have taken three guards out silently.

### 2. Read exhaustion — diagnosed, not fixed

With output fixed, the next wall was reads. Run 9: the executor spent every
tool turn reading and emitted a text-shaped tool call instead of an answer.

**Raising `toolTurns` does not help, and this was measured.** At 8 turns the
executor made 14 reads; at 16 it made **28**, then 21, 25, 22, 23, 24 — and
ended the same way. The read appetite scales to whatever budget it is given.
Run 10 cost 810,622 tokens and `toolTurns` was reverted to 8.

Still open. See *What is still open* below.

### 3. The loop misdiagnosed the result — fixed

A tool call written out as text puts the path it wants on a line of its own:

    <parameter=path>
    forge/config.py
    </parameter>

`_BARE_PATH` matched `forge/config.py`, the next non-blank line was not a
fence, and `describe_unparsed` therefore reported *"Your response named files
but did not fence their contents."* The model had not named a file. It had made
a tool call. **The loop's single free correction was answering a mistake that
had not been made**, which is why the re-ask never recovered — verified against
runs 8, 9 and 10, all three of which got that message.

**Fixed**: `_TEXT_TOOL_CALL` is checked before the path and fence heuristics,
and says what actually happened, that tools are gone on the final turn, and
what to send instead.

### 4. Tool insufficiency — fixed

Measured over **597 recorded tool calls**: `read_file` 61.6%, `grep` 31.3%,
`list_dir` 5.4%, `outline` **1.7%**. And **104 of 187 greps were followed
immediately by a `read_file` of the file just matched** — a match is a line
number and nothing else, so understanding it cost a second call.

**Fixed** by `grep(context=…)`, capped at 20 lines, and `read_symbol(path,
name)` which reads one definition whole — function, class, or `Class.method` —
because the file knows where a definition ends, where a guessed line range does
not. A name that is absent answers with the names that are present.

**`outline` was retired on measurement.** `scripts/tool_probe.py` ran three
arms over one task:

| arm | `outline` | `read_symbol` | `read_file` | `grep` |
|---|---:|---:|---:|---:|
| as-shipped | **0/21** | — | 42.9% | 42.9% |
| description rewritten to sell it | **0/13** | — | 46.2% | 38.5% |
| `read_symbol` added | **0/15** | 13.3% | 20.0% | 53.3% |

Zero calls in 49, including an arm whose description said *"call this FIRST on
any file you have not read"* — so not discoverability. The same model adopted
`read_symbol` on first exposure — so not habit. What `outline` could not do is
**end a lookup**: six of its ten production calls were followed straight by a
read and none ever concluded anything. `read_symbol` displaced `read_file`
because it returns the thing that was wanted.

`outline_python` survives: the repository map is built from it, and
`read_symbol` uses it to list a file's declarations when the name asked for is
absent — outline's one useful moment, delivered where it is needed.

---

## What is still open

### Read exhaustion has no fix

Raising the cap is measured not to work. What is left:

- **The final turn is not binding.** `_converse` says *"That was your last
  read. Answer the ticket now"* at `remaining == 2` and the model reads anyway.
- **Reserve turns rather than announce them.** Hard-stop reads at `N-2` and
  give two turns to answering, so exhaustion is structural rather than a
  request.
- **A better reference-file habit.** Of 280 reads across runs 8–10, **38.6%
  were of `tests/`** — the model working out house conventions for a test file
  the ticket told it to write and gave it no example of. A ticket should list a
  reference for anything it has to *write*, not only for the code it changes.
  That is a spec habit, not a loop change, and it is likely the cheapest of
  these.

### The executor narrates before it acts

Run 11 attempt 1 read successfully, used `read_symbol`, correctly identified
everything it needed — and then spent its output budget writing that summary
and hit the limit before emitting any edit. The prose was accurate and
unnecessary. Worth a prompt rule: blocks first, prose never.

### SQLite connections are closed by the garbage collector — fixed 2026-09-09

`Store` kept one connection per thread in `threading.local()`. `close()`
closed only the calling thread's. The dashboard is a `ThreadingHTTPServer`, so
**every request runs on a new thread that opens a connection** — `connect`,
`PRAGMA journal_mode=WAL`, `PRAGMA busy_timeout` — and nothing closes it
explicitly.

**It is not a leak** — twenty request-like threads left exactly one live
connection, because CPython's refcounting closes each thread's connection when
its locals are cleared. **It is not cosmetic either.** Run 11's test step
failed four times with

    PermissionError: [WinError 32] The process cannot access the file
    because it is being used by another process

on a temp directory a loop-written test was cleaning up. An open SQLite handle
holds a Windows file lock, so a `Store` left to the garbage collector blocks
`shutil.rmtree` of the directory it lives in. This is the failure the
`ResourceWarning` was predicting, and it now costs whole test runs rather than
noise. What is real:

- **Test noise.** Tests construct `Store` on temp directories and never
  `close()`, so the suite emits `ResourceWarning` in volume — loud enough to
  bury a genuine warning, which is the actual cost.
- **Per-request setup.** Three statements before any dashboard request does
  work.
- **A latent assumption.** It relies on refcounting to release a file handle.
  True on CPython, not a property to lean on, and an open handle on Windows
  blocks the temp-directory cleanup some tests do.

**Applied.** `Store` now keeps a registry of every connection it opened, which
is what makes closing another thread's possible at all. `close()` is unchanged
and still touches only the caller's — the dashboard calls it from `Handler.finish`,
as each connection thread ends, which is where the trail of open handles came
from. `close_all()` closes the lot for a caller that can promise nobody else is
using the store, and `__enter__`/`__exit__` are that promise, so a test owns its
store with `with` and can then delete the directory. A `weakref.finalize` closes
the connections of a store nobody closed at all — the case that covers the tests
the loop writes, which will not know the context manager exists.

Measured on this suite: `tests/test_forge.py` emitted **721 `ResourceWarning`s**
before and none after; the nine left across the whole suite are `memory.py`'s
subprocess pipes and test sockets, not databases. The dashboard half is pinned
by `test_a_served_connection_does_not_leave_one_open`, which fails at `4 != 1`
with the `finish` override removed.

### `forge prune` still does not prune the step log

`Store.clear_step_detail` landed as RT-001 and **nothing calls it**. Measured on
this repository's own database: `steps.detail` was **1,871,779 bytes of
2,551,808 — 73% of the file**, from 285 rows across seven runs.

RT-002 (`PRUNE.md`) is the wiring plus the `VACUUM` that makes the file
actually shrink.

---

## Two lessons that generalise

### Prose in a spec body is not a contract; only criteria are

`RETENTION.md` described the `cmd_prune` wiring and the `VACUUM` in its spec
text. Its acceptance criteria covered the `Store` method and said only that
`forge prune --dry-run` must leave every `detail` value as it was — which is
trivially true of code that never touches `detail`.

**The loop delivered exactly what the criteria demanded and passed honestly.**
The gap was in the ticket. Anything the prose promises and the criteria do not
demand will not be built, and should not be expected to be.

### `failed` is not terminal while retry cycles remain

Run 10 was reported here as a failure on the strength of a mid-run database
read taken when the ticket showed `failed` at attempt 3. It went on through two
retry cycles and finished **done**, having written `clear_step_detail` and six
passing tests using three SEARCH blocks — the first ticket this loop has landed
against a repository of ordinary size.

Read the run's own summary before reporting an outcome. A monitor that breaks
on `TICKET failed` stops watching too early.
