# Retention for the step log

`forge prune` clears the artifact trees of old runs and leaves the database
alone. `docs/ROADMAP.md` has carried the other half as deferred since the
review: *"`run.db` still grows without bound, and step detail is the bulk of
it."*

Measured on this repository's own `.hybridforge/run.db` on 2026-09-08: 2,551,808
bytes, of which `steps.detail` is **1,871,779** — 73% of the file, across 285
rows from seven runs. A daemon against a real backlog does not level off.

The fix is not to delete step rows. `forge status` accounts for work whose
transcripts `prune` has already removed, and the step log is what it counts, so
dropping rows would make an old run's history disappear rather than shrink.
What can go is the `detail` column of the runs whose artifacts are being
deleted anyway — the same runs, the same decision, one more thing cleared.

## RT-001: Clear step detail for the runs prune deletes

**Kind:** feature

### Spec

Add one method to `Store` in `forge/state.py`:

```python
def clear_step_detail(self, run_ids: Sequence[int]) -> tuple[int, int]:
```

It sets `detail` to the empty string on every row of `steps` whose `run_id` is
in `run_ids`, and returns `(rows, bytes)` — how many rows it changed and how
many bytes of `detail` those rows held before it did. Rows whose `detail` is
already empty are not counted in either number. An empty `run_ids` changes
nothing and returns `(0, 0)`.

Only the `detail` column is touched. `status`, `classes`, `findings`, `points`
and the timestamps stay exactly as they are, because those are what
`forge status` and the convergence machinery read, and this is a retention
change rather than a change to what the loop knows.

Then use it in `cmd_prune` in `forge/cli.py`. That command already decides
which runs to remove, as `doomed`; the run numbers of those directories are the
runs whose detail is cleared. It has to keep working the way it does now:

- `--dry-run` prints what would be removed and changes nothing, so it reports
  the row count and the byte total alongside the artifact total and stops.
- The run in progress is never touched, which `doomed` already guarantees.
- A run whose artifacts are gone but whose directory never existed is not an
  error: the command clears detail for the runs it deletes directories for,
  and nothing else.

After clearing, run `VACUUM` on the database so the file actually shrinks.
SQLite leaves freed pages in place otherwise, and a retention command that
reports freeing 1.8 MB while the file stays the same size has not done what it
said. Report the size of the database before and after, in the same style the
command already reports the artifact bytes.

### Allowed files

- `forge/state.py`
- `forge/cli.py`
- `tests/test_retention.py`

### Reference files

- `forge/cli.py`
- `forge/state.py`

### Acceptance criteria

- `Store.clear_step_detail([])` returns `(0, 0)` and changes no row.
- Given two runs each holding steps with non-empty `detail`,
  `clear_step_detail([first_run_id])` returns a row count equal to the number
  of steps in the first run and a byte count equal to the total length of their
  `detail` strings.
- After that call, every step of the first run has `detail` equal to `""` and
  every step of the second run has the `detail` it started with.
- After that call, the `status` of every step of the first run is unchanged.
- Calling `clear_step_detail` twice with the same run id returns `(0, 0)` the
  second time, because no row still holds detail to clear.
- `forge prune --dry-run` leaves every `steps.detail` value in the database
  exactly as it was.
