"""Replay the review-point splitter over recorded rejections.

`docs/ADAPTIVE-TICKET-LOOP.md` §3.1 step 2. The volume axis in §4 wants to
count objections, and a reviewer's rejection currently classes as one thing
however many complaints it carries. Whether that is worth fixing depends on a
number nobody has: how many points a real rejection actually makes.

So this runs `failures.review_points` over every recorded `*-review.md` in the
artifact trees it is given and reports the distribution. If most rejections
carry one point, the volume axis reads tool classes alone and the draft's
"objection" framing was about diagnostics all along.

Reads artifacts only. Nothing here touches a database or a running loop.

    python scripts/review_points.py <tree> [<tree> ...]

A tree is any directory; every `*-review.md` under it is read, and
`*-ratify-reviewer.md` is skipped because sign-off is a different protocol with
its own SIGNOFF/BLOCKING format.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forge.failures import review_points  # noqa: E402
from forge.prompts import parse_verdict  # noqa: E402


def reviews(root: Path) -> list[Path]:
    """Every recorded loop review under `root`, oldest path order."""
    return sorted(
        path
        for path in root.rglob("*review*.md")
        if path.name.endswith("-review.md")
    )


def main(argv: list[str]) -> int:
    roots = [Path(arg) for arg in argv]
    if not roots:
        print(__doc__)
        return 2

    totals: Counter[int] = Counter()
    rejections = 0
    accepts = 0
    unreadable = 0
    zero_point: list[Path] = []
    per_tree: dict[str, Counter[int]] = {}

    for root in roots:
        if not root.exists():
            print(f"missing: {root}")
            return 1
        counts: Counter[int] = Counter()
        for path in reviews(root):
            text = path.read_text(encoding="utf-8", errors="replace")
            approved, reason = parse_verdict(text)
            if approved:
                accepts += 1
                continue
            if "no readable ACCEPT or REJECT verdict" in reason:
                unreadable += 1
                continue
            rejections += 1
            points = review_points(reason)
            counts[len(points)] += 1
            totals[len(points)] += 1
            if not points:
                zero_point.append(path)
        per_tree[str(root)] = counts

    print(f"reviews read:      {accepts + rejections + unreadable}")
    print(f"  accepted:        {accepts}")
    print(f"  rejected:        {rejections}")
    print(f"  unreadable:      {unreadable}")
    if not rejections:
        return 0

    print("\npoints per rejection:")
    for points in sorted(totals):
        share = 100 * totals[points] / rejections
        bar = "#" * totals[points]
        print(f"  {points:>2}: {totals[points]:>3} ({share:4.1f}%)  {bar}")
    multi = sum(n for points, n in totals.items() if points > 1)
    counted = sum(points * n for points, n in totals.items())
    print(
        f"\ncarrying more than one point: {multi}/{rejections} "
        f"({100 * multi / rejections:.1f}%)"
    )
    print(f"points in total: {counted} over {rejections} rejections "
          f"({counted / rejections:.2f} per rejection)")

    print("\nby tree:")
    for name, counts in per_tree.items():
        total = sum(counts.values())
        if not total:
            print(f"  {name}: no rejections")
            continue
        points = sum(k * v for k, v in counts.items())
        print(f"  {name}: {total} rejection(s), {points / total:.2f} points each")

    if zero_point:
        print(f"\nrejections the splitter read as zero points: {len(zero_point)}")
        for path in zero_point[:5]:
            print(f"  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
