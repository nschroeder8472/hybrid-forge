"""Score a local decision model (Deem, OpenJev — /v1/systemone) against the baselines.

    python evaluate.py --url http://127.0.0.1:8300 --model deem-9b

Two decisions, from forge's recorded runs (data/):

- failure_kind: what kind of failure is this verify output — setup, compile,
  lint or test? Labels are hand-assigned from the text. Baseline: keyword
  rules (baseline.failure_kind), which were written after reading the same
  outputs and so are flattered by this set.
- first_try: will this ticket pass verification and review on its first
  attempt? Labels are the recorded outcomes. Baseline: the base rate.

Writes results/<model>.json with per-item answers, accuracy, and calibration.
"""

from __future__ import annotations

import argparse
import json
import math
import time
import urllib.request
from pathlib import Path

import baseline

HERE = Path(__file__).resolve().parent
STATE_LIMIT = 6000

KIND_CRITERIA = {
    "setup": "The command never reached the project's code: the project or tool is not set up "
             "(no build manifest, a missing entry point or test target, a tool or runtime that "
             "will not start).",
    "compile": "The code does not build: compiler or type errors, unresolved imports or symbols, "
               "syntax errors, link errors.",
    "lint": "The code builds, but a linter or style checker reports problems (unused names, "
            "line length, style rules, lints promoted to errors).",
    "test": "The code builds and the tests ran, but one or more tests failed or errored.",
}


def ask(url: str, state: str, questions: dict) -> dict:
    body = json.dumps({"state": state[:STATE_LIMIT], "questions": questions}).encode()
    request = urllib.request.Request(f"{url}/v1/systemone", data=body,
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=300) as response:
        return json.loads(response.read())


def failure_kind(url: str) -> dict:
    rows = [json.loads(line) for line in open(HERE / "data" / "kind.jsonl", encoding="utf-8")]
    items, seconds = [], []
    for row in rows:
        started = time.time()
        answer = ask(url, f"Verify step: {row['step']}\n\nOutput:\n{row['output']}", {
            "kind": {"type": "choice", "criteria": KIND_CRITERIA,
                     "instructions": "What kind of failure is this verify step output?"},
        })["answers"]["kind"]
        seconds.append(time.time() - started)
        items.append({"idx": row["idx"], "label": row["label"], "model": answer["choice"],
                      "confidence": answer.get("confidence"),
                      "probabilities": answer.get("probabilities"),
                      "baseline": baseline.failure_kind(row["step"], row["output"])})
    n = len(items)
    model_ok = sum(i["model"] == i["label"] for i in items)
    base_ok = sum(i["baseline"] == i["label"] for i in items)
    # Escalation: answers below a confidence line go to Claude; what is left must be right.
    gates = {}
    for line in (0.5, 0.7, 0.9):
        kept = [i for i in items if (i["confidence"] or 0) >= line]
        gates[str(line)] = {"kept": len(kept),
                            "kept_accuracy": round(sum(i["model"] == i["label"] for i in kept)
                                                   / max(1, len(kept)), 3)}
    return {"n": n, "model_accuracy": round(model_ok / n, 3),
            "baseline_accuracy": round(base_ok / n, 3), "escalation": gates,
            "p50_seconds": round(sorted(seconds)[n // 2], 3), "items": items}


def ticket_state(t: dict) -> str:
    criteria = "\n".join(f"- {c}" for c in t["criteria"])
    files = "\n".join(f"- {f}" for f in t["allowed_files"])
    return (f"A ticket handed to a mid-size local code model (an executor that writes the "
            f"files, a tester that writes tests from the criteria, then lint, typecheck, tests "
            f"and a model review).\n\nTicket {t['id'].split('#')[-1]} ({t['kind']}): "
            f"{t['title']}\n\nSpec:\n{t['spec']}\n\nAcceptance criteria:\n{criteria}\n\n"
            f"Files it may write:\n{files}")


def first_try(url: str) -> dict:
    rows = [json.loads(line) for line in open(HERE / "data" / "tickets.jsonl", encoding="utf-8")]
    rate = sum(r["landed_first_try"] for r in rows) / len(rows)
    items, seconds = [], []
    for row in rows:
        started = time.time()
        answer = ask(url, ticket_state(row), {
            "lands": {"type": "noul",
                      "instructions": "Will this ticket pass verification and review on its "
                                      "first attempt?",
                      "criteria": {"true": "It lands on the first attempt",
                                   "false": "It needs more attempts, or fails"}},
        })["answers"]["lands"]
        seconds.append(time.time() - started)
        items.append({"id": row["id"], "label": bool(row["landed_first_try"]),
                      "p": float(answer["noul"])})
    return {"n": len(items), "base_rate": round(rate, 3), **_probability_scores(items, rate),
            "p50_seconds": round(sorted(seconds)[len(seconds) // 2], 3), "items": items}


def _probability_scores(items: list[dict], rate: float) -> dict:
    def brier(ps):
        return sum((p - float(i["label"])) ** 2 for p, i in zip(ps, items)) / len(items)

    def logloss(ps):
        eps = 1e-6
        return -sum(math.log(max(eps, p if i["label"] else 1 - p))
                    for p, i in zip(ps, items)) / len(items)

    model = [i["p"] for i in items]
    flat = [rate] * len(items)
    pos = [i["p"] for i in items if i["label"]]
    neg = [i["p"] for i in items if not i["label"]]
    auc = (sum((a > b) + 0.5 * (a == b) for a in pos for b in neg) / (len(pos) * len(neg))
           if pos and neg else None)
    bins = []
    for lo in (0.0, 0.2, 0.4, 0.6, 0.8):
        group = [i for i in items if lo <= i["p"] < lo + 0.2 or (lo == 0.8 and i["p"] == 1.0)]
        if group:
            bins.append({"range": f"{lo:.1f}-{lo + 0.2:.1f}", "n": len(group),
                         "mean_p": round(sum(i["p"] for i in group) / len(group), 3),
                         "landed": round(sum(i["label"] for i in group) / len(group), 3)})
    return {"auc": round(auc, 3) if auc is not None else None,
            "brier": round(brier(model), 4), "brier_base_rate": round(brier(flat), 4),
            "logloss": round(logloss(model), 4), "logloss_base_rate": round(logloss(flat), 4),
            "calibration": bins}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8300")
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    result = {"model": args.model, "failure_kind": failure_kind(args.url),
              "first_try": first_try(args.url)}
    out = HERE / "results"
    out.mkdir(exist_ok=True)
    (out / f"{args.model}.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    summary = {k: {kk: vv for kk, vv in v.items() if kk != "items"}
               for k, v in result.items() if isinstance(v, dict)}
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
