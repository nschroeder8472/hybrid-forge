"""Replay the recorded reviews and price a skip rule before building one.

`docs/ROADMAP.md`, *Deferred from the review*: review is roughly one call per
ticket and close to all of the money on a hybrid run, so skipping review for
tickets whose diff is trivial would cut the bill -- "at the cost of the thing
that keeps a cheap executor honest. Needs evidence before it is worth the
risk."

This is that evidence, and it is a replay rather than a run. Every model call
the loop makes is recorded beside its step as `NN-<step>.json` carrying the
role, the verdict and the exact token usage, so a candidate skip rule can be
scored against reviews that already happened: how much would it have saved,
and which rejections would it have thrown away.

A skip rule of the shape *skip review when `measure` < T* is safe only if no
rejection sits below T. So rather than guessing a threshold, this reports the
safe ceiling each measure admits -- `min(measure)` over the rejections -- and
how many approvals fall under it. That number is the whole of the money any
threshold rule of that shape can buy without silencing a rejection the loop
actually made.

Reads artifacts only. Nothing here touches a database or a running loop.

    python scripts/review_cost.py [--wrote-something] <tree> [<tree> ...]

A tree is any directory; every `attempt-*` under it is read.
`ratify-reviewer` steps are skipped -- sign-off is a different protocol with
its own budget, counted separately in the report.

`--wrote-something` drops the attempts that wrote no file at all. Those are
review's degenerate case and the loop reviews them on purpose -- an empty diff
does not distinguish "already on disk" from "never written", so
`Orchestrator._attempt` shows the reviewer the file state instead. They are
also the shape a triviality rule would skip first, so the run is worth
repeating without them: a finding that only holds because of them is a finding
about one fixed defect rather than about review.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# A review is skippable only on evidence available *before* the call is made,
# so every measure here is a property of the diff or of the work that produced
# it -- never of the verdict.
MEASURES = (
    ("files written", "files"),
    ("executor+tester chars", "work_chars"),
    ("review prompt tokens", "prompt_tokens"),
)


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return {}


def attempts(root: Path) -> list[Path]:
    """Every attempt directory under `root`, in path order."""
    return sorted(p for p in root.rglob("attempt-*") if p.is_dir())


def review_of(attempt: Path) -> tuple[Path, dict] | None:
    """The attempt's loop review, or None. Sign-off reviews are not it."""
    for path in sorted(attempt.glob("*.json")):
        if not path.name.endswith("-review.json"):
            continue
        record = _load(path)
        if record.get("step") == "review":
            return path, record
    return None


def observe(attempt: Path) -> dict | None:
    """One row per recorded review: verdict, price, and the diff's shape."""
    found = review_of(attempt)
    if found is None:
        return None
    path, review = found

    files: set[str] = set()
    work_chars = 0
    work_tokens = 0
    signoff_tokens = 0
    for other in sorted(attempt.glob("*.json")):
        record = _load(other)
        step = record.get("step") or ""
        if step == "apply":
            files.update(record.get("written") or [])
        usage = record.get("usage") or {}
        tokens = int(usage.get("total_tokens") or 0)
        if step.startswith("ratify"):
            signoff_tokens += tokens
        elif record.get("role") in {"executor", "tester"}:
            work_tokens += tokens
            body = other.with_suffix(".md")
            if body.exists():
                work_chars += len(body.read_text(encoding="utf-8", errors="replace"))

    usage = review.get("usage") or {}
    return {
        "path": path,
        "ticket": review.get("ticket") or "",
        "attempt": review.get("attempt"),
        "approved": bool(review.get("approved")),
        "truncated": bool(review.get("truncated")),
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "review_tokens": int(usage.get("total_tokens") or 0),
        "work_tokens": work_tokens,
        "signoff_tokens": signoff_tokens,
        "files": len(files),
        "work_chars": work_chars,
    }


def safe_ceiling(rows: list[dict], key: str) -> tuple[int, list[dict]]:
    """The largest threshold that silences no rejection, and what it would skip.

    `skip when measure < T` keeps every rejection exactly while
    `T <= min(measure over rejections)`. At that ceiling the approvals below it
    are the entire saving the rule shape admits.
    """
    rejected = [row for row in rows if not row["approved"]]
    if not rejected:
        return 0, []
    ceiling = min(row[key] for row in rejected)
    return ceiling, [row for row in rows if row["approved"] and row[key] < ceiling]


def main(argv: list[str]) -> int:
    wrote_something = "--wrote-something" in argv
    roots = [Path(arg) for arg in argv if not arg.startswith("--")]
    if not roots:
        print(__doc__)
        return 2

    rows: list[dict] = []
    per_tree: dict[str, list[dict]] = {}
    for root in roots:
        if not root.exists():
            print(f"missing: {root}")
            return 1
        found = [row for row in (observe(a) for a in attempts(root)) if row]
        if wrote_something:
            found = [row for row in found if row["files"]]
        per_tree[str(root)] = found
        rows.extend(found)

    if not rows:
        print("no recorded reviews found")
        return 1

    rejected = [row for row in rows if not row["approved"]]
    review_tokens = sum(row["review_tokens"] for row in rows)
    work_tokens = sum(row["work_tokens"] for row in rows)
    signoff_tokens = sum(row["signoff_tokens"] for row in rows)
    total = review_tokens + work_tokens + signoff_tokens

    print(f"reviews replayed:  {len(rows)}")
    print(f"  approved:        {len(rows) - len(rejected)}")
    print(
        f"  rejected:        {len(rejected)} "
        f"({100 * len(rejected) / len(rows):.0f}%)"
    )
    print(f"  truncated:       {sum(1 for r in rows if r['truncated'])}")

    print("\ntokens in the attempts that reached review:")
    for name, value in (
        ("review", review_tokens),
        ("executor+tester", work_tokens),
        ("sign-off", signoff_tokens),
    ):
        print(f"  {name:<16} {value:>12,}  ({100 * value / total:4.1f}%)")
    print(f"  {'total':<16} {total:>12,}")
    print(f"\n  mean review call: {review_tokens // len(rows):,} tokens")

    print("\nsafe skip thresholds -- `skip review when measure < T`:")
    for label, key in MEASURES:
        ceiling, skipped = safe_ceiling(rows, key)
        saved = sum(row["review_tokens"] for row in skipped)
        values = sorted(row[key] for row in rows)
        rej = sorted(row[key] for row in rejected)
        print(f"\n  {label}")
        print(f"    all reviews:   min {values[0]:,}  max {values[-1]:,}")
        print(f"    rejections:    min {rej[0]:,}  max {rej[-1]:,}")
        print(f"    safe ceiling:  T = {ceiling:,}")
        print(
            f"    would skip:    {len(skipped)}/{len(rows)} review(s), "
            f"{saved:,} tokens ({100 * saved / review_tokens:.1f}% of review spend)"
        )

    print("\nby tree:")
    for name, found in per_tree.items():
        if not found:
            print(f"  {name}: no reviews")
            continue
        bad = sum(1 for row in found if not row["approved"])
        tokens = sum(row["review_tokens"] for row in found)
        print(
            f"  {name}: {len(found)} review(s), {bad} rejected, "
            f"{tokens:,} review tokens"
        )

    print("\nevery review, as replayed:")
    print(
        f"  {'verdict':<9} {'ticket':<10} {'att':>3} {'files':>5} "
        f"{'work chars':>11} {'prompt tok':>11}"
    )
    for row in rows:
        print(
            f"  {'REJECT' if not row['approved'] else 'accept':<9} "
            f"{row['ticket']:<10} {row['attempt']:>3} {row['files']:>5} "
            f"{row['work_chars']:>11,} {row['prompt_tokens']:>11,}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
