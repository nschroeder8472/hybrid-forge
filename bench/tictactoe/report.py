"""Tabulate every arm run under the bench output directory.

    python report.py [--out DIR] [--markdown]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from run_arm import DEFAULT_OUT

LOCAL = ("qwen", "nemotron")


def row(result: dict) -> dict:
    acc = result.get("acceptance", {})
    host = result.get("host", {})
    usage = host.get("usage") or {}
    forge = result.get("forge", {})
    seats = forge.get("usage", {})
    tickets = forge.get("tickets", [])
    opus_in = sum(usage.get(k, 0) for k in (
        "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    return {
        "arm": f"{result['arm']}-{result['rep']}",
        "accept": f"{acc.get('total', {}).get('passed', 0)}/{acc.get('total', {}).get('of', 49)}",
        "game": f"{acc.get('game', {}).get('passed', 0)}/19",
        "assets": f"{acc.get('assets', {}).get('passed', 0)}/16",
        "page": f"{acc.get('page', {}).get('passed', 0)}/14",
        "opus $": f"{host.get('usd') or 0:.2f}",
        "seat $": f"{sum(v['usd'] for k, v in seats.items()):.2f}",
        "claude $": f"{result.get('claude_usd_total', 0):.2f}",
        "opus out tok": f"{usage.get('output_tokens', 0):,}",
        "opus in+cache": f"{opus_in:,}",
        "local tok": f"{sum(v['in'] + v['out'] for k, v in seats.items() if k in LOCAL):,}",
        "renders": sum(v.get("images", 0) for v in seats.values()),
        "turns": host.get("turns"),
        "wall": f"{result.get('wall_seconds', 0) / 60:.0f}m",
        "tickets": (f"{sum(1 for t in tickets if t['status'] == 'done')}/{len(tickets)}"
                    if tickets else "-"),
        "attempts": sum(t["attempts"] for t in tickets) if tickets else "-",
        "overrules": forge.get("overrules", "-"),
        "own tests": f"{result.get('own_tests', {}).get('js_tests', 0)}"
        + ("" if result.get("own_tests", {}).get("js_ok") else " (red)"),
        "uncommitted": len(result.get("uncommitted", [])),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    rows = [row(json.loads(p.read_text(encoding="utf-8")))
            for p in sorted(args.out.glob("*/result.json"))]
    if not rows:
        print("no results yet")
        return 0
    keys = list(rows[0])
    print("| " + " | ".join(keys) + " |")
    print("|" + "---|" * len(keys))
    for r in rows:
        print("| " + " | ".join(str(r[k]) for k in keys) + " |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
