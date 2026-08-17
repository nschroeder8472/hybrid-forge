"""Re-read what a past run recorded, using the parsers as they stand now.

Every parser in this harness reads text somebody else's tool or model produced,
and the shapes that text arrives in are not knowable from the code. They are
knowable from the recordings, and the recordings are already on disk: the
untruncated model replies and tool output under `.hybridforge/artifacts/`, and
the clipped copies in the `steps` table when artifacts were off.

Two changes in one afternoon were wrong on the first attempt and passed their
unit tests, because the fixture was written from what the author believed the
output looked like:

- A contradiction check asked whether a reproduction was implicated in a
  failure and found it in cargo's `Running tests\\bug_001_test.rs` banner —
  printed whether the target passed or failed. It concluded the fix was not
  working and detected nothing.
- A recovery for replies that forgot their path line accepted one whose only
  fenced block was a quoted fragment. It would have overwritten a whole file
  with three lines of it.

Both were obvious the moment the code met a real recording. So this module runs
the current parsers over those recordings and, where the run recorded what the
parser produced at the time, says whether the answer has changed.

It is read-only. Nothing here writes to the repository, the database, or the
artifacts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from .artifacts import ARTIFACTS_DIR
from .config import Config
from .failures import files_blamed, signatures
from .patch import infer_single_file, normalize_path, parse_output
from .state import Store

# Roles whose replies are parsed into files. The reviewer and the recorder
# answer in prose and verdicts; there is nothing here to re-read for them.
_FILE_ROLES = ("executor", "tester")

# `Artifacts.record` clips these to twenty entries, so a step with more than
# that cannot be compared exactly and is reported as partial rather than as a
# difference nobody can act on.
_RECORDED_CAP = 20


@dataclass
class Record:
    """One recorded step, with the text it produced."""

    run_id: int
    ticket_id: str
    attempt: int
    step: str
    status: str
    text: str
    meta: dict[str, Any] = field(default_factory=dict)
    # Where it came from, so a surprising line can be opened by hand.
    origin: str = ""
    # Position within the attempt, from the filename `Artifacts` writes. The
    # order is what pairs a reply with the apply that followed it; an attempt
    # holds several replies and at most one apply, so "same attempt" is not
    # enough to say which reply the apply is a record of.
    index: int = 0

    @property
    def role(self) -> str:
        return str(self.meta.get("role", ""))

    @property
    def is_reply(self) -> bool:
        """A model completion, as opposed to a command's output."""
        return bool(self.role)

    @property
    def is_shell(self) -> bool:
        return "command" in self.meta


@dataclass
class Finding:
    """What a parser makes of one record now, beside what was recorded then."""

    record: Record
    lens: str
    now: str
    then: str = ""
    # None when the run recorded nothing to compare against.
    changed: bool | None = None
    note: str = ""


def _artifact_records(config: Config, run_id: int | None, ticket: str | None) -> list[Record]:
    """Every recorded step on disk, in run then ticket then attempt order."""
    base = config.config_dir / ARTIFACTS_DIR
    if not base.is_dir():
        return []

    found: list[Record] = []
    for run_dir in sorted(base.glob("run-*")):
        try:
            recorded_run = int(run_dir.name.split("-", 1)[1])
        except (IndexError, ValueError):
            continue
        if run_id is not None and recorded_run != run_id:
            continue
        for sidecar in sorted(run_dir.glob("*/attempt-*/*.json")):
            try:
                meta = json.loads(sidecar.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if ticket and str(meta.get("ticket", "")) != ticket:
                continue
            body = sidecar.with_suffix(".md")
            try:
                text = body.read_text(encoding="utf-8") if body.is_file() else ""
            except OSError:
                text = ""
            prefix = sidecar.stem.split("-", 1)[0]
            found.append(
                Record(
                    run_id=recorded_run,
                    ticket_id=str(meta.get("ticket", "")),
                    attempt=int(meta.get("attempt", 1) or 1),
                    step=str(meta.get("step", "")),
                    status=str(meta.get("status", "")),
                    text=text,
                    meta=meta,
                    origin=str(sidecar),
                    index=int(prefix) if prefix.isdigit() else 0,
                )
            )
    return found


def _step_records(config: Config, store: Store, run_id: int | None, ticket: str | None) -> list[Record]:
    """The database's copy, for runs recorded before artifacts existed.

    Clipped at 20k characters by the writer, and carrying no record of what the
    parser made of it at the time — so these can be re-read but not compared.
    """
    rows = store.steps_for_replay(run_id=run_id, ticket_id=ticket)
    return [
        Record(
            run_id=int(row["run_id"]),
            ticket_id=str(row["ticket_id"] or ""),
            attempt=0,
            step=str(row["name"] or ""),
            status=str(row["status"] or ""),
            text=str(row["detail"] or ""),
            meta={},
            origin=f"steps#{row['id']}",
        )
        for row in rows
    ]


def records(
    config: Config,
    store: Store,
    *,
    run_id: int | None = None,
    ticket: str | None = None,
) -> tuple[list[Record], str]:
    """Everything available to replay, and which source it came from.

    Artifacts are preferred: they are untruncated and they carry what the run
    made of each step, which is the half that turns a re-read into a check.
    """
    found = _artifact_records(config, run_id, ticket)
    if found:
        return found, "artifacts"
    return _step_records(config, store, run_id, ticket), "the steps table"


def pair_applies(all_records: list[Record]) -> dict[int, Record]:
    """Map each reply to the `apply` that recorded what was read out of it.

    The apply's `written` is the only durable statement of what the parser
    produced at the time, which is what makes a re-read a check rather than a
    printout. But an attempt holds more than one reply — a reprompted build
    writes two, and a bug attempt writes the reproduction before the build —
    while it holds at most one apply. Matching on "same attempt" therefore
    pairs the wrong two, and does it invisibly.

    It did, and this tool caught it about itself on the first real run: a
    tester's reproduction was compared against the executor's apply and a
    reprompted build against its predecessor's, reporting three parser changes
    where the parser had not changed at all.

    So an apply belongs to the **nearest preceding reply**, by the sequence
    number `Artifacts` writes into each filename. A reply with another reply
    between it and the apply gets nothing to compare against, which is correct:
    nothing recorded what was read out of it.
    """
    ordered = sorted(
        all_records, key=lambda r: (r.run_id, r.ticket_id, r.attempt, r.index)
    )
    paired: dict[int, Record] = {}
    pending: Record | None = None
    key: tuple[Any, ...] = ()
    for record in ordered:
        here = (record.run_id, record.ticket_id, record.attempt)
        if here != key:
            key, pending = here, None
        if record.step == "apply":
            if pending is not None:
                paired[id(pending)] = record
            pending = None
        elif record.is_reply:
            pending = record
    return paired


def replay_parse(
    applied: Record | None, record: Record, writable: list[str], root: Path
) -> Finding:
    """Re-read one model reply as files.

    Reports the recovery separately from the parse: a reply that parses cleanly
    never reaches it, and one that does not is only recoverable against the file
    as it exists *now*, which is not the file the reply was answering about.
    """
    parsed = parse_output(record.text)
    if parsed.is_blocked:
        now = "BLOCKED"
    elif parsed.edits:
        now = ", ".join(sorted(normalize_path(edit.path) for edit in parsed.edits))
    else:
        now = "(no files)"

    # Recovery is the one part of this that cannot be reconstructed faithfully.
    # It depends on the ticket's scope and on the file's contents, and both are
    # read as they are *now* rather than as they were — a ticket whose scope was
    # later widened, or a file since rewritten, gives a different answer than
    # the run would have. Said out loud rather than left as a silent absence.
    note = ""
    if not parsed.edits and not parsed.is_blocked:
        if len(writable) != 1:
            note = (
                f"not recoverable: the ticket now writes {len(writable)} files, "
                f"so there is no single destination to infer"
            )
        else:
            try:
                current = (root / writable[0]).read_text(encoding="utf-8", errors="replace")
            except OSError:
                current = ""
            if infer_single_file(record.text, current):
                note = f"recoverable as {writable[0]} (against the tree as it stands now)"
            else:
                note = "not recoverable from the tree as it stands now"

    if applied is None:
        return Finding(record, "parse", now, note=note)

    written = sorted(normalize_path(p) for p in applied.meta.get("written", []))
    then = ", ".join(written) or "(nothing written)"
    # Scope rejection happens after parsing, so a path the parser produced and
    # the ticket refused is absent from `written` without the parser differing.
    rejected = applied.meta.get("rejected") or []
    if rejected:
        then += f"  [+{len(rejected)} rejected by scope]"
        return Finding(record, "parse", now, then, changed=None,
                       note=note or "scope rejected some edits; not comparable")

    changed = sorted(normalize_path(e.path) for e in parsed.edits) != written
    return Finding(record, "parse", now, then, changed=changed, note=note)


def replay_blame(record: Record) -> Finding:
    """Re-read one command's output as blame.

    Compared against the run's own `introduced` where the sidecar has it — that
    set is `signatures(output) - pre_existing`, both of which are recorded, so
    a parser that now sees more or fewer diagnostics shows up here.
    """
    pre_existing = list(record.meta.get("pre_existing", []) or [])
    found = signatures(record.text)
    blamed = sorted(files_blamed(record.text, exclude=set(pre_existing)))
    now = f"{len(found)} signature(s)" + (f", blames {', '.join(blamed)}" if blamed else "")

    if "introduced" not in record.meta:
        return Finding(record, "blame", now)

    recorded = sorted(record.meta.get("introduced") or [])
    then = f"{len(recorded)} introduced"
    if len(recorded) >= _RECORDED_CAP or len(pre_existing) >= _RECORDED_CAP:
        return Finding(record, "blame", now, then, changed=None,
                       note=f"recorded sets clipped at {_RECORDED_CAP}; not comparable")

    introduced = sorted(found - set(pre_existing)) if pre_existing else []
    if not pre_existing:
        # The run could not attribute either, so there is nothing to disagree
        # with — `introduced` was empty by rule, not by measurement.
        return Finding(record, "blame", now, then, changed=None,
                       note="no baseline recorded; nothing to compare")
    return Finding(record, "blame", now, then, changed=introduced != recorded)


def replay(
    config: Config,
    store: Store,
    *,
    run_id: int | None = None,
    ticket: str | None = None,
    lens: str = "all",
) -> tuple[list[Finding], str]:
    """Every finding for the selected records, and the source they came from."""
    found, source = records(config, store, run_id=run_id, ticket=ticket)
    scope = {t.ticket_id: t for t in _tickets_for(store, found)}
    paired = pair_applies(found)

    findings: list[Finding] = []
    for record in found:
        if lens in ("all", "parse") and record.role in _FILE_ROLES and record.text.strip():
            held = scope.get(record.ticket_id)
            writable = [
                path
                for path in (held.allowed_files if held else [])
                if not any(c in path for c in "*?[")
            ]
            findings.append(
                replay_parse(paired.get(id(record)), record, writable, config.root)
            )
        elif lens in ("all", "blame") and record.is_shell and record.text.strip():
            findings.append(replay_blame(record))
        elif lens in ("all", "blame") and not record.meta and record.text.strip():
            # Steps-table fallback: no sidecar says which kind it was, so the
            # blame lens is the one that reads anything usefully.
            findings.append(replay_blame(record))
    return findings, source


def _tickets_for(store: Store, found: list[Record]) -> Iterator[Any]:
    """The tickets these records belong to, once per run."""
    for run in {record.run_id for record in found}:
        yield from store.list_tickets(run)
