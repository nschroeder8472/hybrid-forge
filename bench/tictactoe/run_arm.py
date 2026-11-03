"""Run one arm of the tic-tac-toe benchmark, then score it with the hidden suite.

    python run_arm.py A|B|C [--rep N] [--budget USD] [--out DIR]

Arms (see README.md):
  A  Opus as host through forge; local seats (Qwen, Nemotron), Flux 2 renders,
     Claude (Opus) only as the seat that looks at pictures.
  B  Opus alone: no forge, no image model.
  C  Opus as host through forge; Claude seats (Sonnet executor and reviewer,
     Haiku tester, Sonnet viewer), Flux 2 renders.

Every arm gets the same TASK.md, starts from the same seed repository, runs
headless as `claude -p --model opus`, and is scored by acceptance/, which no
arm ever sees. Results land in <out>/<arm>-<rep>/result.json.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
FORGE_ROOT = HERE.parents[1]
DEFAULT_OUT = Path(r"D:\Users\Nik\Documents\Projects\forge-bench")
HOOK = FORGE_ROOT / "plugins" / "forge" / "hooks" / "guard_judging_files.py"
SKILLS = [
    FORGE_ROOT / "plugins" / "forge" / "skills" / "forge-protocol" / "SKILL.md",
    FORGE_ROOT / "plugins" / "forge" / "skills" / "claim-verification" / "SKILL.md",
]
ARMS = {
    "A": {"forge": True, "config": "A-forge-local.config.json"},
    "B": {"forge": False},
    "C": {"forge": True, "config": "C-forge-claude.config.json"},
}
BASE_TOOLS = [
    "Read", "Edit", "Write", "Glob", "Grep", "TodoWrite",
    "Bash(node:*)", "Bash(npm:*)", "Bash(npx:*)", "Bash(python:*)", "Bash(git:*)",
    "Bash(ls:*)", "Bash(mkdir:*)", "Bash(cat:*)", "Bash(cd:*)",
]


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "user.email=bench@localhost", "-c", "user.name=bench", *args],
        cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:].lstrip()
    return text


def prepare(arm: str, run_dir: Path) -> Path:
    repo = run_dir / "repo"
    shutil.copytree(HERE / "seed", repo)
    if ARMS[arm]["forge"]:
        state = repo / ".hybridforge"
        (state / "comfy").mkdir(parents=True)
        shutil.copy(HERE / "arms" / ARMS[arm]["config"], state / "config.json")
        for workflow in (HERE / "arms" / "forge-comfy").glob("*.json"):
            shutil.copy(workflow, state / "comfy" / workflow.name)
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "seed")
    return repo


def system_prompt(arm: str, run_dir: Path) -> Path:
    parts = [(HERE / "arms" / ("host.md" if ARMS[arm]["forge"] else "alone.md")).read_text(
        encoding="utf-8")]
    if ARMS[arm]["forge"]:
        parts += [strip_frontmatter(p.read_text(encoding="utf-8")) for p in SKILLS]
    path = run_dir / "system-prompt.md"
    path.write_text("\n\n".join(parts), encoding="utf-8")
    return path


def claude_argv(arm: str, run_dir: Path, repo: Path, budget: float) -> list[str]:
    argv = [
        "claude", "-p", "--model", "opus", "--output-format", "json",
        "--no-session-persistence", "--permission-mode", "acceptEdits",
        "--max-budget-usd", str(budget),
        # User and plugin settings stay out: every arm sees the same harness.
        "--setting-sources", "project,local",
        "--append-system-prompt-file", str(system_prompt(arm, run_dir)),
        "--strict-mcp-config",
    ]
    tools = list(BASE_TOOLS)
    if ARMS[arm]["forge"]:
        mcp = run_dir / "mcp.json"
        mcp.write_text(json.dumps({"mcpServers": {"forge": {
            "type": "stdio", "command": "forge", "args": ["--root", str(repo), "mcp"],
        }}}), encoding="utf-8")
        settings = run_dir / "settings.json"
        settings.write_text(json.dumps({"hooks": {"PreToolUse": [{
            "matcher": "Edit|Write|MultiEdit|NotebookEdit",
            "hooks": [{"type": "command", "command": f'python "{HOOK}"'}],
        }]}}), encoding="utf-8")
        argv += ["--mcp-config", str(mcp), "--settings", str(settings)]
        tools.append("mcp__forge")
    argv += ["--allowedTools", ",".join(tools)]
    return argv


def stop_servers(repo: Path) -> None:
    for kind in ("llama", "comfy"):
        subprocess.run(["forge", "--root", str(repo), kind, "stop"],
                       capture_output=True, text=True)


def forge_metrics(repo: Path) -> dict:
    db = repo / ".hybridforge" / "run.db"
    if not db.exists():
        return {}
    c = sqlite3.connect(db)
    usage = {
        r[0]: {"calls": r[1], "in": r[2] or 0, "out": r[3] or 0, "usd": round(r[4] or 0, 4),
               "images": r[5] or 0}
        for r in c.execute(
            "SELECT model, COUNT(*), SUM(prompt_tokens), SUM(completion_tokens), "
            "SUM(cost_usd), SUM(images) FROM usage GROUP BY model")
    }
    tickets = [
        {"id": r[0], "status": r[1], "attempts": r[2], "kind": r[3], "route": r[4]}
        for r in c.execute(
            "SELECT ticket_id, status, attempts + attempt_base, kind, route FROM tickets "
            "ORDER BY run_id, position")
    ]
    overrules = c.execute(
        "SELECT COUNT(*) FROM contract_log WHERE field = 'overrule'").fetchone()[0]
    revisions = c.execute(
        "SELECT COUNT(*) FROM contract_log "
        "WHERE field NOT IN ('created', 'overrule')").fetchone()[0]
    jobs = dict(c.execute("SELECT kind, COUNT(*) FROM jobs GROUP BY kind").fetchall())
    return {"usage": usage, "tickets": tickets, "overrules": overrules,
            "revisions": revisions, "jobs": jobs}


def own_tests(repo: Path) -> dict:
    js = subprocess.run('node --test "tests/*.test.js"', cwd=repo, shell=True,
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
    py = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests",
                         "-p", "*_test.py", "-t", "."], cwd=repo,
                        capture_output=True, text=True, encoding="utf-8", errors="replace")

    def count(label: str, text: str) -> int:
        for line in text.splitlines():
            if line.startswith(f"\u2139 {label} "):
                return int(line.split()[-1])
        return 0

    return {"js_ok": js.returncode == 0, "js_tests": count("tests", js.stdout),
            "py_ok": py.returncode == 0}


def score(repo: Path, run_dir: Path) -> dict:
    env = {**os.environ, "PROJECT": str(repo)}
    # TAP, because the counting below reads `ok` / `not ok` lines; node's
    # default reporter on a pipe is `spec`, which has neither (every arm of
    # the first run scored 0/19 on that).
    game = subprocess.run(["node", "--test", "--test-reporter=tap",
                           str(HERE / "acceptance" / "game.accept.mjs")],
                          env=env, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    lines = [line.strip() for line in game.stdout.splitlines()]
    passed = sum(1 for line in lines if line.startswith("ok "))
    failed = [line.split(" - ", 1)[-1] for line in lines if line.startswith("not ok ")]
    assets = json.loads(subprocess.run(
        [sys.executable, str(HERE / "acceptance" / "assets_accept.py"), str(repo)],
        capture_output=True, text=True, encoding="utf-8").stdout or "{}")
    page = json.loads(subprocess.run(
        [sys.executable, str(HERE / "acceptance" / "page_accept.py"), str(repo),
         str(run_dir / "screenshot.png")],
        capture_output=True, text=True, encoding="utf-8").stdout or "{}")
    return {
        "game": {"passed": passed, "total": 19, "failed": failed},
        "assets": assets,
        "page": page,
        "total": {"passed": passed + assets.get("passed", 0) + page.get("passed", 0),
                  "of": 19 + 16 + 14},
    }


def rescore(run_dir: Path) -> dict:
    """Score a finished run again with today's acceptance suite."""
    result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    result["acceptance"] = score(run_dir / "repo", run_dir)
    (run_dir / "result.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    return result["acceptance"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("arm", choices=sorted(ARMS))
    parser.add_argument("--rep", type=int, default=1)
    parser.add_argument("--budget", type=float, default=40.0)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--timeout-hours", type=float, default=5.0)
    args = parser.parse_args()

    run_dir = args.out / f"{args.arm}-{args.rep}"
    if run_dir.exists():
        sys.exit(f"{run_dir} exists; pick another --rep")
    run_dir.mkdir(parents=True)
    repo = prepare(args.arm, run_dir)
    argv = claude_argv(args.arm, run_dir, repo, args.budget)
    (run_dir / "argv.json").write_text(json.dumps(argv, indent=1), encoding="utf-8")
    task = (HERE / "TASK.md").read_text(encoding="utf-8")

    env = {**os.environ, "MCP_TOOL_TIMEOUT": "1900000", "MCP_TIMEOUT": "60000"}
    started = time.time()
    try:
        proc = subprocess.run(argv, input=task, cwd=repo, env=env, capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=args.timeout_hours * 3600)
        stdout, stderr, code = proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as exc:
        stdout = (exc.stdout or b"").decode("utf-8", "replace") if isinstance(
            exc.stdout, bytes) else (exc.stdout or "")
        stderr, code = "timed out", -1
    wall = time.time() - started
    (run_dir / "claude.json").write_text(stdout, encoding="utf-8")
    (run_dir / "claude.err").write_text(stderr, encoding="utf-8")
    if ARMS[args.arm]["forge"]:
        stop_servers(repo)

    try:
        host = json.loads(stdout)
    except ValueError:
        host = {"parse_error": stdout[-2000:]}
    metrics = forge_metrics(repo)
    seat_usd = sum(v["usd"] for v in metrics.get("usage", {}).values())
    result = {
        "arm": args.arm,
        "rep": args.rep,
        "exit": code,
        "wall_seconds": round(wall),
        "host": {
            "usd": host.get("total_cost_usd"),
            "turns": host.get("num_turns"),
            "usage": host.get("usage"),
            "is_error": host.get("is_error"),
            "summary": str(host.get("result", ""))[:3000],
        },
        "claude_usd_total": round((host.get("total_cost_usd") or 0) + seat_usd, 4),
        "forge": metrics,
        "uncommitted": git(repo, "status", "--porcelain").stdout.strip().splitlines(),
        "commits": len(git(repo, "log", "--oneline").stdout.splitlines()),
        "own_tests": own_tests(repo),
        "acceptance": score(repo, run_dir),
    }
    (run_dir / "result.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    summary = ("arm", "exit", "wall_seconds", "claude_usd_total")
    print(json.dumps({k: result[k] for k in summary}, indent=1))
    print(json.dumps(result["acceptance"]["total"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
