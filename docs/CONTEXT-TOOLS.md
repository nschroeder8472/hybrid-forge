# Context by retrieval, not by paste

**Status:** phases 1-5 built, no live run yet. Written against run 1 of
`HANDBACK-DASHBOARD.md`, 2026-09-04, which is the first time this loop was run
against hybrid-forge's own tree rather than against `examples/sample-project`.

Every prior run in this repository was against a fixture with eleven Python
files. The first run against a real one — 59 Python files, a 1,815-line
`state.py`, a 4,000-line suite — failed in a way no fixture could have
produced, and the failure is about how context reaches a model rather than
about the ticket, the executor, or the spec.

## The run this is written from

Run 1 ingested two tickets. HD-001 asked for a function in
`forge/ui/server.py` and a key added to the dict `snapshot()` builds. It ended
`blocked` after 3 retry cycles, 9 attempts, 44 calls and 2.3M tokens, having
**written no files at all**.

Every one of the nine executor replies was a hallucinated tool call:

```
<tool_call>
<function=Bash>
<parameter=command>cat /testbed/forge/ui/server.py</parameter>
</function>
</tool_call>
```

The model asked for a shell nine times and never emitted a diff. The first
reply said why:

> I'll start by examining the actual source files I need to work with, since
> the reference files are truncated and I need the exact `Ticket` and `Store`
> definitions.

That statement is true. Reconstructing the prompt from the ticket's stored
scope gives 187,702 characters, of which:

| | characters |
|---|---|
| `forge/ui/server.py` (writable) | 12,405 |
| `forge/routes.py` (the one declared reference) | 4,864 |
| `forge/ui/__init__.py` | 44 |
| eleven files under `tests/`, five of them cut at the ceiling | 156,387 |

**83% of the prompt was the repository's own test suite**, five files ending
mid-line at `[truncated at 24000 characters]`. And `forge/state.py` — which
defines `Ticket` and the `Store.RETRYABLE` the spec instructs the executor to
read — was not in the prompt at all.

The cause is one interaction. The ticket listed its test file under
`Allowed files`, which `docs/` and the `spec-contract` skill both require so
the tester writes inside the ticket's scope. `evidence.reading_scope` then
expands the *source siblings of every writable file*, and the siblings of
`tests/test_handback_ui.py` are the entire suite:

```
reading_scope(['forge/ui/server.py'])                              -> 2 files
reading_scope(['forge/ui/server.py', 'tests/test_handback_ui.py']) -> 12 files
```

Both halves of that failure are the same mistake in opposite directions. A
scope computed before anyone has read a line included 156k characters nobody
needed and excluded the one file the spec named.

## Why no amount of tuning fixes it

The rule that would have saved this run — *do not expand siblings when the
writable file is itself a test* — is correct, cheap, and would be the third
such rule. `reading_scope` already carries the sibling expansion because a fix
must stay consistent with the module beside it, and `OPAQUE.md` run 1 exists
because that same rule handed over a file a ticket meant to withhold.

The pattern is that **relevance is a function of the task, and the task is only
legible after reading**. Which files matter for "add an `evidence` key to
`snapshot()`" cannot be computed before reading `snapshot()`. Every static
scope rule is a guess made at the one moment when the least is known, and each
new rule trades one class of wrong guess for another.

Large-model agents do not make that guess. They are given a map and read tools,
and they assemble context themselves, mid-task, from what they find. The model
knows what it lacks — this one said so, in plain English, nine times — and with
a read tool that knowledge costs one call instead of an entire run.

A large context window is a budget, not a target. Filling it with irrelevance
costs accuracy, and costs it worst in the middle of the prompt; it costs money
and latency linearly, on every attempt of every role. Both models in this run
had a 131k window and the prompt was 47k tokens. The window was never the
problem.

## What is built

Five phases. Each is useful on its own and none of the later ones is required
for the earlier ones to ship.

### 1. Tool calls, and a read-only toolset

The wire types (`ToolSpec`, `ToolCall`, `ToolResult`), `tools=` on
`Provider.complete`, and a `supports_tools` capability declared per provider
rather than assumed. A provider that cannot take tools is not broken and is not
refused — the role falls back to the pasted-sources prompt, which is what every
role does today.

The tools are read-only, and that is the whole safety argument:

| tool | what it answers |
|---|---|
| `read_file(path, start, end)` | the contents, or a slice of them |
| `grep(pattern, glob)` | where a symbol is used or defined |
| `list_dir(path)` | what is in a directory |
| `outline(path)` | a file's definitions and signatures, without its bodies |

No shell, no write, no network. Every path is normalised and confined to the
repository root by `patch.is_safe_path`, which already guards the write side.

**The property worth keeping is that a model cannot change the tree except
through a reviewed patch.** That survives untouched. *A model cannot look at
the tree* was never the safety property — it was an accident of the executor
having no filesystem, and the run above is what it costs.

### 2. A repository map

Generated mechanically from the AST: every file, its definitions and their
signatures, one line of purpose taken from the docstring, and the imports that
link them. Not bodies — an index, not a payload. For this repository it is a
few thousand tokens against the 1.9M characters a full paste would be.

The map is what makes the read tools cheap: a model that can see
`forge/state.py: class Store … RETRYABLE = (...)` reads one file instead of
grepping for it.

### 3. A stable prefix

Ordering, so that prefix caching does the thing "restore the model's context"
is reaching for. Models are stateless; there is nothing to restore but tokens
you send again, and both Anthropic and llama.cpp reuse a KV cache for an
identical prefix.

- **Stable, first, cached** — system prompt, repository map, project
  conventions, toolchain rules. Identical across every ticket and every role.
- **Volatile, last** — the ticket, its criteria, this attempt's failures, the
  diff under review.

The current builder interleaves them, so nothing caches. Run 1 rebuilt a 47k
prompt 44 times.

### 4. Scope grants reading; it does not limit it

`Reference files` becomes a hint about where to start rather than a ceiling.
A role with tools may read any file in the repository. `Allowed files` is
unchanged and still enforced on apply, mechanically — the gate that matters is
the write gate.

This is what removes the failure above rather than renaming it: with reading
granted, the sibling expansion that produced 156k characters of tests has
nothing left to protect against, and can stop guessing.

### 5. No silent truncation

A file too large to send whole is answered with its map entry and an
invitation to read a range, never with its first 24,000 characters and a note
saying it is reference only. A model told *this file is large, ask for the part
you want* behaves. A model handed five files cut mid-line concludes its whole
context is unreliable, and this one did.

## What this costs

The executor's step stops being one call and becomes a bounded conversation:
model, tool results, model again, until it answers or hits a turn cap. The
loop's step log, artifact recording and budget accounting are all written
around one call per step, and all three have to learn about turns.

That is real work and it is bounded — the bug loop already runs multi-step
roles, and `_call` is a single funnel every role goes through.

Three limits are set rather than discovered:

- **A turn cap per attempt.** A model that has not answered after it is not
  going to.
- **A byte cap per tool result.** `read_file` on a 200k file returns a slice
  and says so.
- **The budget gate runs per turn**, not per step, so a conversation that grows
  is stopped by the same mechanism that stops a prompt that does not fit.

## What it measures, before any model has seen it

HD-001's own prompt, rebuilt from the ticket's stored scope:

| | characters | tokens |
|---|---|---|
| pasted scope, as run 1 sent it | 187,480 | ~46,900 |
| map, tools and named references | 64,626 | ~16,200 |

Of the 16,200, roughly 9,000 is the repository map, which is identical for
every ticket in the run and sits in the cached prefix. The part that changes
per ticket is about 7,000 tokens against 47,000.

That is a measurement of the prompt, not of the loop: it says the pile is gone
and says nothing about whether the model reads better without it. The run below
is what would say that.

## What would falsify this

The measurement is HD-001, which failed nine times: reference files reduced to
nothing, read tools on, one run. If the executor reads `forge/state.py`, writes
the patch and lands, the argument holds. If it reads twelve files and still
writes nothing, the problem was never context and this document is wrong.
