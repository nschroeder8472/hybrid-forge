# The half of retention that was under-specified

**Built.** The loop wrote RT-002 against this repository and it passed all
eight criteria. One thing none of them asserted turned out to be wrong anyway:
the command reported a file size that the `VACUUM` had not yet produced,
because in WAL mode the rebuilt database sits in the log until a checkpoint.
`Store.checkpoint` is the fix, and the story is in `docs/EXECUTOR-CEILINGS.md`.

`Store.clear_step_detail` landed as RT-001 and nothing calls it. The ticket's
prose asked for the `cmd_prune` wiring too, and its acceptance criteria did
not — the only one mentioning the command said `forge prune --dry-run` must
leave every `detail` value as it was, which is trivially true of code that
never touches `detail`. The loop delivered what the criteria demanded and
passed honestly. This is the same work, specified as criteria rather than as
prose.

`forge/cli.py` is 110,903 characters. Under the old whole-file protocol that
was 27,725 tokens to restate against an executor budget of roughly 10,240, and
the ticket was impossible before the first call. It is a replacement-block
ticket now.

## RT-002: Prune the step log with the artifacts

**Kind:** feature

### Spec

`cmd_prune` in `forge/cli.py` already decides which runs to remove — the list
it calls `doomed` — and deletes their artifact directories. Those same runs are
the ones whose step detail should go, because the transcripts on disk and the
transcripts in the database are the same record of the same work.

Call `Store.clear_step_detail` with the run numbers of the directories it
removes, after the directories are gone. Report what it cleared in the same
style the command already reports the artifact bytes.

`--dry-run` must stay a report that changes nothing: it prints the rows and
bytes that *would* be cleared, alongside the artifact total, and returns
without touching the database.

After a real prune, run `VACUUM` on the database. SQLite leaves freed pages in
place, so a command reporting that it freed 1.8 MB while the file stays the
same size has not done what it said. Report the file's size before and after.

The run in progress is never touched, which `doomed` already guarantees.

### Allowed files

- `forge/cli.py`
- `tests/test_prune_retention.py`

### Reference files

- `forge/cli.py`
- `forge/state.py`
- `tests/test_retention.py`

### Acceptance criteria

- Given a workspace whose database holds two runs with non-empty `steps.detail`
  and artifact directories for both, `cmd_prune` with `keep=1` and
  `dry_run=False` leaves every step of the older run with `detail` equal to
  `""`.
- In that same case, every step of the newer run keeps the `detail` it started
  with.
- In that same case, the `status` of every step of the older run is unchanged.
- `cmd_prune` with `dry_run=True` leaves every `steps.detail` value in the
  database exactly as it was, for every run.
- `cmd_prune` with `dry_run=True` prints a line containing both the number of
  step rows it would clear and the word `detail`.
- `cmd_prune` with `dry_run=False` prints a line containing the number of step
  rows it cleared.
- After `cmd_prune` with `dry_run=False`, the database file is no larger in
  bytes than it was immediately before the call.
- `cmd_prune` returns `0` in every case above.
