# An executor that edits instead of restating

The executor returns whole files. So the cost of a change is the size of the
*existing file*, not the size of the change, and a one-method addition to a
96 KB module costs 96 KB of output.

Measured on 2026-09-08, running `RETENTION.md` against this repository: the
ticket allowed `forge/state.py` (96,548 characters, ~24,137 tokens to re-emit)
and `forge/cli.py` (110,903, ~27,725). The executor's whole output budget is
16,384, of which roughly 6,144 goes to reasoning. **The ticket was impossible
before the first call**, and nothing said so: three attempts across two retry
cycles, 36 calls, 2,647,873 tokens, and not one file written. The loop's own
advice to the model — *"emit the same implementation in fewer output tokens —
fewer files per response"* — cannot be taken when a single file exceeds the
budget on its own.

The fixture cannot show this. `examples/sample-project`'s files are one and two
kilobytes; this repository's are a hundred.

Nothing downstream needs the whole file. `apply_edits` writes what it is given,
the diff is computed from git afterwards, and the reviewer reads that diff. The
whole-file form is a convenience for the parser, and it is the only thing
standing between this loop and a repository of ordinary size.

## ED-001: A replacement block the executor can use instead of a whole file

**Kind:** feature

### Spec

Add a second body shape to the executor's existing block form in
`forge/patch.py`. The outer shape does not change — a path line, then a fence,
then a body — so `_BLOCK`, the double-fence unwrapping and the path handling
all keep working exactly as they do. What changes is that a body may now hold
one or more replacements instead of a file:

```
forge/state.py
​```
<<<<<<< SEARCH
    def end_step(self, step_id: int) -> None:
=======
    def clear_step_detail(self, run_ids):
        ...

    def end_step(self, step_id: int) -> None:
​```
```

A body is a set of replacements when it contains a line that is exactly
`<<<<<<< SEARCH`. Otherwise it is a whole file, as now.

Add a dataclass:

```python
@dataclass
class Replacement:
    search: str
    replace: str
```

and give `FileEdit` a third field, `replacements: list[Replacement]`, empty by
default. A `FileEdit` with replacements carries them; one without is a whole
file and behaves exactly as it does today.

`FileEdit.content` must stay populated for a replacement edit. Three guards
read it — `foreign_bindings`, `laundered_assertions` and `weakened_criteria` —
and a field left empty would take them out silently on any patch-shaped reply.
Set `content` to the replacement halves joined by newlines: that is the new
code, which is what those guards are looking at.

Then teach `apply_edits` to apply them. For each replacement, in order:

- The search text must occur in the file **exactly once**. Zero occurrences or
  more than one is an error naming the path, the count, and the first line of
  the search text. Nothing is written for that file.
- On a unique match, replace that occurrence with the replacement text.
- A file that does not exist cannot be edited: that is the same error, with a
  count of zero.

The uniqueness rule is the whole safety argument. A search block that matches
twice and is applied to the first occurrence corrupts a file in a way a test
suite may not catch, which is worse than the failure it replaces. Refusing is
loud, and the message tells the model what to make unique.

Every replacement for one file is applied to the same in-memory copy, so a
later search may match text an earlier replacement introduced, and the file is
written once at the end. If any replacement in a file fails, that file is left
untouched entirely — no half-applied edit reaches disk.

### Allowed files

- `forge/patch.py`
- `tests/test_edits.py`

### Reference files

- `forge/loop.py`

### Acceptance criteria

- Parsing a body holding `<<<<<<< SEARCH`, `=======` and `>>>>>>> REPLACE`
  yields one `FileEdit` whose `replacements` has one entry with the search and
  replace halves, and whose `path` is the path from the path line.
- Parsing a body with two such markers yields one `FileEdit` with two entries
  in `replacements`, in the order they appeared.
- Parsing a body with no `<<<<<<< SEARCH` line yields a `FileEdit` whose
  `replacements` is empty and whose `content` is the body, exactly as before.
- A `FileEdit` parsed from a replacement body has `content` equal to the
  replacement halves joined by `"\n"`.
- `apply_edits` given a replacement whose search text appears once in the file
  writes the file with that occurrence replaced and returns the path.
- `apply_edits` raises `ValueError` when the search text appears twice in the
  file, and the file on disk is unchanged.
- `apply_edits` raises `ValueError` when the search text appears zero times in
  the file, and the file on disk is unchanged.
- Given two replacements for one file where the first succeeds and the second
  matches nothing, `apply_edits` raises `ValueError` and the file on disk still
  holds its original text.
- `apply_edits` given a `FileEdit` with no replacements writes `content` as the
  whole file, exactly as it does now.
