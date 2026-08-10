"""`forge` — the command line around the daemon.

    forge init [--defaults]        set up .hybridforge/ for this repo
    forge doctor                   probe every configured model
    forge ingest <file|->          turn a spec or plan into a backlog
    forge go [--plan f] [--open]   run the loop until done or stopped
    forge status                   one-shot summary
    forge pause | resume | stop    control a running loop
    forge ui                       serve the dashboard on its own

`ingest` and `go` are separate on purpose: a backlog is reviewable text, and
the moment before an unattended run starts is the cheapest moment to catch a
ticket routed the wrong way. `forge go --plan <file>` collapses both when you
have already reviewed the plan elsewhere.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import webbrowser
from pathlib import Path

from . import wizard
from .config import Config, ConfigError, default_config
from .ingest import ingest as ingest_document
from .ingest import write_tickets
from .loop import CONTROL_KEY, CONTROL_PAUSE, CONTROL_RUN, CONTROL_STOP, Orchestrator
from .memory import MemoryClient
from .profile import Profile
from .providers import ProviderError
from .state import Store
from .tokens import format_tokens
from .ui import server as ui_server


def _load(root: str) -> Config:
    try:
        return Config.load(root)
    except ConfigError as exc:
        sys.exit(f"error: {exc}")


def _store(config: Config) -> Store:
    return Store(config.db_path)


# ----------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    config_path = root / ".hybridforge" / "config.json"
    if config_path.exists() and not args.force:
        print(f"{config_path} already exists. Re-run with --force to overwrite.")
        return 1

    profile = Profile.load()

    if args.defaults:
        config = default_config(root)
        saved_profile = None
    else:
        # No TTY disables the questions rather than the wizard: the flow still
        # runs, prints what it chose, and takes every default. A setup command
        # that blocks on stdin nobody is watching is worse than one that guesses.
        prompter = wizard.Prompter(enabled=wizard.interactive())
        try:
            result = wizard.run(root, profile, prompter)
        except wizard.Aborted:
            print("\nAborted. Nothing was written.")
            return 1
        if result is None:
            print("\nDeclined. Nothing was written.")
            return 1
        config, saved_profile = result

    written = config.write()
    config.tickets_dir.mkdir(parents=True, exist_ok=True)

    gitignore = config.config_dir / ".gitignore"
    # The database is a mutable log, not a reviewable artifact — the tickets
    # and config are what belong in version control.
    gitignore.write_text("run.db\nrun.db-wal\nrun.db-shm\n", encoding="utf-8")

    print(f"\nWrote {written}")

    if saved_profile is not None:
        try:
            profile_path = saved_profile.save()
            print(f"Saved these endpoints to {profile_path}")
            print("  The next repo on this machine starts from them.")
        except OSError as exc:
            # A read-only or missing home directory is not a reason to fail an
            # init that already succeeded — the repo config is the deliverable.
            print(f"(could not save machine profile: {exc})")

    print("\nNext:")
    if args.defaults:
        print("  1. Edit `models` and `roles` to point at the models you want.")
        print("  2. Fill in `commands.lint` / `.typecheck` / `.test` for this repo.")
        print("  3. Run `forge doctor` to check every model answers.")
    else:
        print("  1. `forge doctor` — re-checks every endpoint.")
        print("  2. `forge ingest <spec>` — turn a plan into a reviewable backlog.")
        print("  3. `forge go` — run it.")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    config = _load(args.root)
    print(f"project: {config.root}")
    print(f"roles:   {json.dumps(config.roles)}\n")

    failures = 0
    for name in sorted(config.models):
        from .providers import build_provider

        try:
            # Same block the loop will use, cwd included — doctor that probes a
            # differently-configured provider than the run is not a check.
            provider = build_provider(name, config.model_block(name))
        except Exception as exc:  # noqa: BLE001 - report, don't abort the sweep
            print(f"  {name}: FAIL (config) {exc}")
            failures += 1
            continue

        report = provider.health()
        caps = provider.capabilities()
        print(f"  {name}: {report}")
        print(
            f"      context={format_tokens(caps.context_window)} "
            f"max_output={format_tokens(caps.max_output_tokens)}"
        )
        if report.startswith("FAIL"):
            failures += 1

    # Memory is optional, so an unconfigured one is reported, not counted as a
    # failure. A configured-but-broken one is worth failing on: silently
    # building without project history is the bug this feature exists to fix.
    memory = MemoryClient.from_config(config.memory, room=config.room)
    if memory is None:
        print("  memory: (not configured — the loop will run without project context)")
    else:
        report = memory.describe()
        print(f"  memory: {report}")
        if report.startswith("FAIL"):
            failures += 1

    for name in ("lint", "typecheck", "test"):
        command = config.commands.get(name, "")
        print(f"  {name} command: {command or '(none configured)'}")

    print()
    print("all checks passed" if not failures else f"{failures} check(s) failed")
    return 1 if failures else 0


def cmd_ingest(args: argparse.Namespace) -> int:
    config = _load(args.root)
    text = sys.stdin.read() if args.source == "-" else Path(args.source).read_text(encoding="utf-8")

    # The planner is only used when the document is not already ticket-shaped,
    # but it is built up front so a misconfigured planner role fails here
    # rather than halfway through parsing.
    try:
        provider = config.provider_for("planner")
    except ConfigError:
        provider = None

    try:
        tickets, how = ingest_document(text, provider=provider, force_plan=args.replan)
    except (ValueError, ProviderError) as exc:
        sys.exit(f"error: {exc}")

    store = _store(config)
    goal = args.goal or (tickets[0].title if tickets else "ingested plan")
    source = "-" if args.source == "-" else str(Path(args.source).resolve())
    run_id = store.create_run(goal=goal, source=source)
    store.add_tickets(run_id, tickets)
    paths = write_tickets(config.tickets_dir, tickets)
    store.log(run_id, f"Ingested {len(tickets)} ticket(s) ({how}) from {source}.", kind="ingest")

    verb = "parsed directly from your plan" if how == "parsed" else "planned from your spec"
    print(f"Run {run_id}: {len(tickets)} ticket(s) {verb}.\n")
    for ticket, path in zip(tickets, paths):
        marker = " (claude-only)" if ticket.route != "delegate" else ""
        print(f"  {ticket.ticket_id}  {ticket.title}{marker}")
        print(f"      {path}")
    print("\nReview the tickets, then run `forge go`.")
    return 0


def cmd_go(args: argparse.Namespace) -> int:
    config = _load(args.root)
    store = _store(config)

    if args.plan:
        # Same path as `forge ingest`, for a plan already reviewed elsewhere.
        namespace = argparse.Namespace(
            root=args.root, source=args.plan, goal=args.goal, replan=False
        )
        if cmd_ingest(namespace) != 0:
            return 1
        store = _store(config)

    run = store.resumable_run()
    if run is None:
        sys.exit(
            "error: no run to work on. Ingest a spec first:\n"
            "  forge ingest plan.md      (or: forge go --plan plan.md)"
        )
    run_id = int(run["id"])
    counts = store.ticket_counts(run_id)
    remaining = counts.get("pending", 0) + counts.get("running", 0)
    if run["status"] == "stopped":
        print(f"Resuming run {run_id} ({remaining} ticket(s) left).")

    if config.ui.enabled and not args.no_ui:
        ui_server.serve(config, store)
        url = ui_server.url_for(config)
        print(f"Dashboard: {url}")
        if args.open:
            webbrowser.open(url)

    print(f"Run {run_id}: {run['goal']}")
    print("Ctrl-C stops after the current step.\n")

    orchestrator = Orchestrator(config, store)
    try:
        final = orchestrator.run(run_id)
    except KeyboardInterrupt:
        store.set_control(CONTROL_KEY, CONTROL_STOP)
        print("\nStopping after the current step…")
        final = "stopped"

    counts = store.ticket_counts(run_id)
    print(f"\nFinished: {final}")
    print(f"  tickets: {json.dumps(counts)}")
    for row in store.usage_summary():
        total = row["prompt_tokens"] + row["completion_tokens"]
        print(f"  {row['model']}: {row['calls']} calls, {format_tokens(total)} tokens")

    return 0 if final in ("done",) else 1


def cmd_status(args: argparse.Namespace) -> int:
    config = _load(args.root)
    store = _store(config)
    state = ui_server.snapshot(store, config)

    run = state["run"]
    if run is None:
        print("No runs yet. Start with `forge ingest <spec>`.")
        return 0

    print(f"run {run['id']}: {run['status']}")
    if run["goal"]:
        print(f"  goal:    {run['goal']}")
    if run["note"]:
        print(f"  note:    {run['note']}")
    print(f"  control: {state['control']}")
    print(f"  tickets: {json.dumps(state['counts'])}\n")

    for ticket in state["tickets"]:
        marker = {"done": "+", "blocked": "!", "failed": "!", "running": ">"}.get(
            ticket["status"], "-"
        )
        print(f"  {marker} {ticket['id']:<10} {ticket['status']:<9} {ticket['title']}")
        if ticket["note"]:
            print(f"      {ticket['note']}")

    if state["usage"]:
        print()
        for row in state["usage"]:
            print(f"  {row['model']}: {row['calls']} calls, {row['display']} tokens")
    return 0


def cmd_control(args: argparse.Namespace) -> int:
    config = _load(args.root)
    store = _store(config)
    command = {"pause": CONTROL_PAUSE, "resume": CONTROL_RUN, "stop": CONTROL_STOP}[args.command]
    store.set_control(CONTROL_KEY, command)
    store.log(None, f"CLI requested: {args.command}", kind="control")
    print(f"Requested: {args.command}. The loop applies it after the current step.")
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    config = _load(args.root)
    store = _store(config)
    ui_server.serve(config, store)
    url = ui_server.url_for(config)
    print(f"Dashboard: {url}  (Ctrl-C to quit)")
    if args.open:
        webbrowser.open(url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        return 0


# ----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="forge", description=__doc__)
    parser.add_argument("--root", default=".", help="project directory (default: cwd)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="set up .hybridforge/ for this repo, with prompts")
    p.add_argument("--force", action="store_true", help="overwrite an existing config")
    p.add_argument(
        "--defaults",
        action="store_true",
        help="skip the questions and write a default config to edit by hand",
    )
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("doctor", help="probe every configured model")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("ingest", help="turn a spec or plan into a backlog")
    p.add_argument("source", help="path to a markdown spec/plan, or - for stdin")
    p.add_argument("--goal", default="", help="short description of the run")
    p.add_argument(
        "--replan",
        action="store_true",
        help="re-plan with the planner model even if the document already has tickets",
    )
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("go", help="run the loop until done or stopped")
    p.add_argument("--plan", default="", help="ingest this spec first, then start")
    p.add_argument("--goal", default="", help="short description, used with --plan")
    p.add_argument("--no-ui", action="store_true", help="do not start the dashboard")
    p.add_argument("--open", action="store_true", help="open the dashboard in a browser")
    p.set_defaults(func=cmd_go)

    p = sub.add_parser("status", help="one-shot summary of the current run")
    p.set_defaults(func=cmd_status)

    for name, help_text in (
        ("pause", "pause after the current step"),
        ("resume", "resume a paused loop"),
        ("stop", "stop after the current step"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.set_defaults(func=cmd_control, command=name)

    p = sub.add_parser("ui", help="serve the dashboard without running the loop")
    p.add_argument("--open", action="store_true", help="open the dashboard in a browser")
    p.set_defaults(func=cmd_ui)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
