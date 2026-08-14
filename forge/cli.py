"""`forge` — the command line around the daemon.

    forge init [--defaults]        set up .hybridforge/ for this repo
    forge doctor                   probe every configured model
    forge ingest <file|->          turn a spec or plan into a backlog
    forge go [--plan f] [--open]   run the loop until done or stopped
    forge go --retries N           requeue what did not land, N more times
                                   (-1: until it is clean or you stop it)
    forge status                   one-shot summary
    forge retry [--respec]         put failed tickets back on the backlog
    forge bug "<report>"           reproduce a bug, then fix it
    forge toolchain [--language X] what tests each language; set up what nothing does
    forge criteria [ID --accept N] adopt a criterion respec proposed and lost
    forge prune [--keep N]         delete the artifact trees of old runs
    forge models                   write Modelfiles pinning what config cannot
    forge pause | resume | stop    control a running loop
    forge ui [--host H] [--port N] serve the dashboard on its own

`ingest` and `go` are separate on purpose: a backlog is reviewable text, and
the moment before an unattended run starts is the cheapest moment to catch a
ticket routed the wrong way. `forge go --plan <file>` collapses both when you
have already reviewed the plan elsewhere.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
import webbrowser
from collections import Counter
from pathlib import Path

from . import evidence, modelfiles, respec, toolchain, wizard
from .artifacts import ARTIFACTS_DIR
from .config import ANY_LANGUAGE, Config, ConfigError, default_config, normalize_language
from .ingest import ingest as ingest_document
from .ingest import write_tickets
from .loop import (
    CONTROL_KEY,
    CONTROL_PAUSE,
    CONTROL_RUN,
    CONTROL_STOP,
    Orchestrator,
    retries_key,
)
from .memory import MemoryClient
from .patch import matches_any
from .prompts import bug_prompt, locate_prompt, parse_bug, parse_locate
from .profile import Profile
from .providers import ProviderError
from .state import (
    RUN_IDLE,
    TICKET_BLOCKED,
    TICKET_BUG,
    TICKET_DONE,
    TICKET_FAILED,
    TICKET_SKIPPED,
    Store,
    Ticket,
)
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

    _report_modelfiles(config, wrote_config=True)

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


def _report_modelfiles(config: Config, *, wrote_config: bool) -> int:
    """Write a Modelfile per Ollama-backed model and say what to do with them.

    Generated rather than remembered. The settings that belong in a Modelfile
    are exactly the ones nothing else can reach — `num_ctx`, which a global
    `OLLAMA_CONTEXT_LENGTH` silently overrides, and `top_k`/`min_p`, which the
    OpenAI-compatible endpoint accepts and discards. Hand-written they drift:
    one setup carried `num_ctx 32768` across three models trained for eight
    times that, and nothing reported it.

    The files are written; `ollama create` is not run. Building a model takes
    minutes and rewrites something outside this repository, which is a
    decision for whoever is reading the output.
    """
    try:
        written = modelfiles.write(config)
    except Exception as exc:  # noqa: BLE001 - never fail an init over this
        print(f"\n(could not generate Modelfiles: {exc})")
        return 0
    if not written:
        return 0

    where = "Also wrote" if wrote_config else "Wrote"
    print(f"\n{where} {len(written)} Modelfile(s) in {config.config_dir / modelfiles.MODELS_DIR}:")
    for entry, path in written:
        print(f'  {entry.alias:<12} {entry.command} "{path}"')

    print(
        "\nThese pin what config.json cannot: num_ctx, which a global\n"
        "OLLAMA_CONTEXT_LENGTH would override, and top_k/min_p, which Ollama's\n"
        "OpenAI endpoint accepts and ignores. Review them, then run the commands\n"
        "above. Nothing is built for you."
    )

    # A block naming a base model directly cannot be rebuilt under that name:
    # the Modelfile is FROM those weights, and building over them replaces the
    # thing it derives from.
    renamed = [entry for entry, _ in written if entry.rename]
    if renamed:
        print("\nThese build under a new name, so config has to point at it too:")
        for entry in renamed:
            print(f"  models.{entry.alias}.model:  {entry.base}  ->  {entry.create_as}")
    return len(written)


def cmd_models(args: argparse.Namespace) -> int:
    config = _load(args.root)
    if not _report_modelfiles(config, wrote_config=False):
        print(
            "No Ollama-backed models in this config, so there is nothing to pin. "
            "A Modelfile means nothing to vLLM, OpenRouter or OpenAI."
        )
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
            f"max_output={format_tokens(caps.max_output_tokens)} "
            f"prompt_budget={format_tokens(caps.input_budget(caps.max_output_tokens))}"
        )
        # Not counted as failures: every one of these answers a health probe
        # perfectly well and then costs a run. Reported so they are fixed before
        # the run rather than diagnosed after it.
        try:
            notes = provider.diagnostics()
        except Exception as exc:  # noqa: BLE001 - a broken check must not fail doctor
            notes = [f"could not run configuration checks: {exc}"]
        for note in notes:
            print(f"      ! {note}")
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

    uncovered = _report_coverage(config)

    print()
    print("all checks passed" if not failures else f"{failures} check(s) failed")
    if uncovered:
        print(
            f"{len(uncovered)} language(s) in this project have no test command. "
            f"Work in them can only be checked by reading."
        )
    return 1 if failures else 0


def _report_coverage(config: Config) -> list[str]:
    """What runs against each language in the project, and what does not.

    A repository is not one language, and the loop's verification is only as
    wide as its commands. Printing the matrix is what makes a gap visible
    before it becomes a ticket that passed on review alone over code nothing
    ever ran.

    Returns the extensions with no test command, so the caller can say so.
    """
    census = Counter(
        suffix
        for path in evidence.repo_files(config.root, limit=4000)
        if (suffix := Path(path).suffix.lower()) in _SOURCE_SUFFIXES
    )
    if not census:
        for name in ("lint", "typecheck", "test"):
            commands = config.commands_for(name)
            shown = commands.get(ANY_LANGUAGE) or "; ".join(
                f"{key} {value}" for key, value in sorted(commands.items())
            )
            print(f"  {name} command: {shown or '(none configured)'}")
        return []

    width = max(len(suffix) for suffix in census)
    print("\n  language  files  test / lint")
    uncovered: list[str] = []
    for suffix, count in sorted(census.items(), key=lambda item: (-item[1], item[0])):
        test, how = config.covering("test", suffix)
        lint, _ = config.covering("lint", suffix)
        if not test and not config.exempt("test", suffix):
            uncovered.append(suffix)
        # A catch-all that cannot run the language is worse than none: it reads
        # as coverage in every report and proves nothing about the files.
        if config.exempt("test", suffix):
            shown = "(none — declared)"
        else:
            shown = test or (
                "(no test command" + (f" — the one configured {how})" if how else ")")
            )
        print(
            f"  {suffix:<{max(width, 8)}}  {count:>5}  {shown}"
            + ("  (catch-all)" if how == "catch-all" else "")
            + (f"  |  {lint}" if lint else "")
        )
    return uncovered


# Extensions worth reporting coverage for: languages whose behavior a test
# could assert. A stylesheet with no runner is not a gap.
_SOURCE_SUFFIXES = frozenset(
    """.rs .py .js .mjs .cjs .jsx .ts .tsx .go .rb .java .kt .swift .c .cc .cpp
    .h .hpp .cs .php .sh .ps1 .lua .ex .exs .scala .dart""".split()
)


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
        tickets, how, derived = ingest_document(
            text, provider=provider, force_plan=args.replan
        )
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
        waits = f"  [needs {', '.join(ticket.needs)}]" if ticket.needs else ""
        print(f"  {ticket.ticket_id}  {ticket.title}{marker}{waits}")
        print(f"      {path}")

    # An ordering nobody typed is the one worth showing. Two tickets writing
    # one file is ordinary — a ticket is a testable unit, not a file lease —
    # but which of them goes first is a decision just made on their behalf.
    if derived:
        print(f"\nOrdered {len(derived)} pair(s) that write the same file:")
        for later, earlier, path in derived:
            print(f"  {later} waits for {earlier}  ({path})")

    print("\nReview the tickets, then run `forge go`.")
    return 0


def cmd_go(args: argparse.Namespace) -> int:
    config = _load(args.root)

    # Flags override config for this run only; nothing is written back. The
    # usual shape is a config that hands blocked work to a human and one
    # deliberate `forge go --retries -1` before going to bed.
    retries = getattr(args, "retries", None)
    if retries is not None:
        if retries < -1:
            sys.exit(
                "error: --retries takes 0 (hand back to a human), a positive "
                "count, or -1 (retry until the backlog is clean or you stop it)."
            )
        config.loop.retry_cycles = retries
    if getattr(args, "no_respec", False):
        config.loop.respec_on_retry = False
    if getattr(args, "no_preflight", False):
        config.loop.preflight = False

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

    url = ""
    if config.ui.enabled and not args.no_ui:
        ui_server.serve(config, store)
        url = ui_server.url_for(config)
        print(f"Dashboard: {url}")
        if args.open:
            webbrowser.open(url)

    print(f"Run {run_id}: {run['goal']}")
    if config.loop.retry_cycles:
        cycles = (
            "until the backlog is clean or you stop it"
            if config.loop.retry_cycles < 0
            else f"up to {config.loop.retry_cycles} more time(s)"
        )
        respec_note = "with a respec" if config.loop.respec_on_retry else "without a respec"
        print(f"Unfinished tickets will be requeued {cycles}, {respec_note}.")
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
    spent = store.get_control(retries_key(run_id), "0")
    if spent != "0":
        print(f"  retry cycles: {spent}")
    for row in store.usage_summary():
        line = (
            f"  {row['model']}: {row['calls']} calls, "
            f"{format_tokens(row['total_tokens'])} tokens"
        )
        cached = row["cache_creation_tokens"] + row["cache_read_tokens"]
        if cached:
            line += f" ({format_tokens(cached)} cached)"
        if row["cost_usd"]:
            line += f", ${row['cost_usd']:.2f}"
        print(line)

    # The dashboard dies with this process, and the run it was showing is the
    # one worth reading — which failed, on which ticket, with what in the event
    # stream. Exiting the moment the loop stops takes that away at exactly the
    # moment it becomes interesting.
    if url and _should_wait(args):
        print(f"\nDashboard still serving at {url} — Ctrl-C to exit.")
        print("The run is over; this is only here so you can read it.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print()

    return 0 if final in ("done",) else 1


def _should_wait(args: argparse.Namespace) -> bool:
    """Whether to hold the dashboard open after the loop stops.

    Default is "yes if a human is watching". A `forge go` in a scheduled task,
    a CI step, or a `&&` chain must still exit on its own — a daemon that
    silently never returns is a worse failure than a dashboard you have to
    restart with `forge ui`.
    """
    wait = getattr(args, "wait", None)
    if wait is not None:
        return bool(wait)
    return wizard.interactive()


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

    # A run with nothing pending looks finished but may just be stuck. Say how
    # to reopen it here, where the failed tickets are already on screen.
    stuck = sum(state["counts"].get(s, 0) for s in ("failed", "blocked", "skipped"))
    if stuck and not state["counts"].get("pending", 0):
        print(f"\n{stuck} ticket(s) need another pass. Requeue them with: forge retry")
    return 0


def _respec(
    config: Config,
    store: Store,
    run_id: int,
    tickets: list,
    notes: dict[str, str],
    allow_criteria: bool = False,
) -> None:
    """Rewrite requeued tickets from the evidence of why they failed.

    The revision itself lives in `forge.respec`, which the loop's automatic
    retry cycles use too. What is here is the reporting: a human watching a
    terminal wants each ticket's verdict as it lands.
    """
    try:
        provider = config.provider_for("planner")
    except ConfigError as exc:
        print(f"\nwarning: cannot respec — {exc}")
        return

    # Ask for what this planner can actually produce. A hardcoded ceiling is
    # too small for a thinking model, which spends most of its budget before
    # the first character of the answer.
    budget = provider.capabilities().max_output_tokens

    def call(messages, limit):
        return provider.complete(messages, max_tokens=limit, temperature=0.0)

    locked = not (config.loop.respec_criteria or allow_criteria)
    print("\nRe-speccing from the recorded failures…")
    revised: list = []
    for ticket in tickets:
        result = respec.revise(
            store,
            run_id,
            ticket,
            notes.get(ticket.ticket_id, ""),
            call=call,
            budget=budget,
            # The planner has no filesystem either, and a spec written about
            # code nobody showed it is a guess the executor is then judged on.
            sources=respec.sources_for(config.root, ticket),
            criteria_locked=locked,
        )
        if result.impossible:
            # Parked rather than requeued: the planner has just explained that
            # no attempt can pass, so letting the loop spend a full attempt
            # budget proving it is the most expensive way to learn nothing.
            ticket.status = TICKET_BLOCKED
            ticket.blocked_note = f"respec: {result.impossible}"
            store.update_ticket(run_id, ticket)
            print(f"  {ticket.ticket_id:<10} CANNOT BE SATISFIED — {result.impossible}")
            print("      Parked for you. Fix the criterion, then retry it by name.")
            continue
        if result.refused_criteria:
            print(
                f"  {ticket.ticket_id:<10} tried to change {len(result.refused_criteria)} "
                f"criterion(s) from the plan; put back."
            )
            for criterion in result.refused_criteria:
                print(f"      kept: {criterion}")
        if result.admitted_criteria:
            print(
                f"  {ticket.ticket_id:<10} added {len(result.admitted_criteria)} "
                f"criterion(s) restating the spec; kept."
            )
            for criterion in result.admitted_criteria:
                print(f"      added: {criterion}")
        if result.minted_criteria:
            # The one refusal a human may want to overturn, so it comes with
            # the command that overturns it rather than with an instruction to
            # go and edit the plan.
            print(
                f"  {ticket.ticket_id:<10} proposed {len(result.minted_criteria)} "
                f"criterion(s) the plan states nowhere; refused."
            )
            for criterion in result.minted_criteria:
                print(f"      proposed: {criterion}")
            print(f"      Adopt one: forge criteria {ticket.ticket_id} --accept N")
        if result.refused_decisions:
            print(
                f"  {ticket.ticket_id:<10} dropped {len(result.refused_decisions)} "
                f"decision(s) the plan settled; the spec revision was refused."
            )
            for decision in result.refused_decisions:
                print(f"      kept: {decision}")
        if result.restored_context:
            print(
                f"  {ticket.ticket_id:<10} replaced the plan's context; put back, "
                f"with the revision appended to it."
            )
        if not result.revised:
            # The rationale is the content of this outcome: "kept as written"
            # only says the planner declined to act, not why.
            print(f"  {ticket.ticket_id:<10} unchanged — {result.note}")
            if result.rationale:
                print(f"      {result.rationale}")
            continue
        revised.append(ticket)
        print(f"  {ticket.ticket_id:<10} revised {', '.join(result.changed)}")
        if result.rationale:
            print(f"      {result.rationale}")

    if not revised:
        print("\nNo ticket was revised; the files on disk are unchanged.")
        return

    # Tickets are the artifact a human reviews before the loop acts on them,
    # so the files on disk have to carry the revision too. Only the revised
    # ones: rewriting the whole backlog reported "6 ticket file(s)" for one
    # revision, which reads as respec having touched work it never looked at.
    names = ", ".join(ticket.ticket_id for ticket in revised)
    write_tickets(config.tickets_dir, revised)
    print(f"\nRewrote {len(revised)} ticket file(s) in {config.tickets_dir}: {names}.")
    print("Read the revised specs before starting — respec is a suggestion, not a fix.")


def cmd_prune(args: argparse.Namespace) -> int:
    """Drop the artifact trees of old runs.

    Artifacts are the record of what every step actually did, and they are the
    reason a ticket that failed at 2am can be diagnosed at 9. But nothing ever
    removed them: one small project reached 979 files across eight runs, and a
    daemon left running against a real backlog does not level off.

    Only whole runs are removed, newest kept, and only the artifact tree — the
    database keeps every run's history either way, so `forge status` still
    accounts for work this deletes the transcripts of.
    """
    config = _load(args.root)
    store = _store(config)

    base = config.config_dir / ARTIFACTS_DIR
    if not base.is_dir():
        print(f"Nothing to prune: {base} does not exist.")
        return 0

    runs = sorted(
        (path for path in base.iterdir() if path.is_dir() and path.name.startswith("run-")),
        key=lambda path: _run_number(path.name),
    )
    latest = store.latest_run()
    current = int(latest["id"]) if latest is not None else 0

    doomed = runs[: max(0, len(runs) - args.keep)]
    # Never the run in progress, however low `--keep` goes: deleting the
    # transcripts of a live run as it writes them is the one case where this
    # command could destroy something nobody has read yet.
    doomed = [path for path in doomed if _run_number(path.name) != current]

    if not doomed:
        print(
            f"Nothing to prune: {len(runs)} run(s) on disk, keeping {args.keep}."
        )
        return 0

    total = sum(
        entry.stat().st_size for path in doomed for entry in path.rglob("*") if entry.is_file()
    )
    if args.dry_run:
        print(f"Would remove {len(doomed)} run(s), {_megabytes(total)}:")
        for path in doomed:
            print(f"  {path.name}")
        print("\nRe-run without --dry-run to delete.")
        return 0

    removed = 0
    for path in doomed:
        try:
            shutil.rmtree(path)
        except OSError as exc:
            print(f"  {path.name}: could not remove — {exc}")
            continue
        removed += 1

    print(f"Removed {removed} run(s) of artifacts, {_megabytes(total)} freed.")
    print(f"Kept the {args.keep} newest. Run history stays in {config.db_path.name}.")
    return 0


def _run_number(name: str) -> int:
    try:
        return int(name.split("-", 1)[1])
    except (IndexError, ValueError):
        return 0


def _megabytes(size: int) -> str:
    return f"{size / 1_048_576:.1f} MB" if size >= 1_048_576 else f"{size / 1024:.0f} KB"


def cmd_retry(args: argparse.Namespace) -> int:
    """Put failed work back on the backlog so the loop can pick it up again.

    Without this a run that ends `blocked` is a dead end: `forge go` finds the
    run but no ticket is `pending`, so it re-declares the backlog exhausted and
    the only way forward is re-ingesting the whole plan and redoing the work
    that already succeeded.
    """
    config = _load(args.root)
    store = _store(config)

    if args.run:
        run = store.get_run(args.run)
        if run is None:
            sys.exit(f"error: no run {args.run}. List them with `forge status`.")
    else:
        run = store.latest_run()
        if run is None:
            sys.exit("error: no runs yet. Ingest a spec first:\n  forge ingest plan.md")
    run_id = int(run["id"])

    wanted = list(args.ticket) if args.ticket else None
    if wanted:
        known = {t.ticket_id for t in store.list_tickets(run_id)}
        missing = [t for t in wanted if t not in known]
        if missing:
            sys.exit(
                f"error: run {run_id} has no ticket {', '.join(missing)}. "
                "Check the ids with `forge status`."
            )

    statuses = None
    if args.all and not wanted:
        # Everything except work still queued — re-running a pending ticket is
        # a no-op, and resetting it would discard attempts already spent.
        statuses = (TICKET_DONE, TICKET_FAILED, TICKET_BLOCKED, TICKET_SKIPPED)

    # Captured before the reset clears it — for a ticket the executor gave up
    # on with `BLOCKED:`, this note is the only record of what it could not
    # decide, and no step was logged as failed.
    notes = {t.ticket_id: t.blocked_note for t in store.list_tickets(run_id)}

    reset = store.reset_tickets(run_id, ticket_ids=wanted, statuses=statuses)
    if not reset:
        counts = store.ticket_counts(run_id)
        print(
            f"Nothing to retry in run {run_id} (tickets: {json.dumps(counts)}).\n"
            "Name a ticket with --ticket ID, or --all to redo completed work too."
        )
        return 0

    store.set_run_status(run_id, RUN_IDLE, note=f"retrying {len(reset)} ticket(s)")
    store.log(run_id, f"Retry queued {len(reset)} ticket(s).", kind="control")
    # A human stepping in restores the automatic budget too: the next `forge
    # go` should get its full `retryCycles` rather than inheriting a count
    # spent on the specs this retry has just replaced.
    store.set_control(retries_key(run_id), "0")

    print(f"Run {run_id}: queued {len(reset)} ticket(s) for retry.")
    for ticket in reset:
        prior = f"{ticket.attempt_base} prior attempt(s)" if ticket.attempt_base else "fresh"
        print(f"  {ticket.ticket_id:<10} {ticket.title} ({prior})")

    if args.respec:
        _respec(config, store, run_id, reset, notes, allow_criteria=args.respec_criteria)

    # A newer run would shadow this one, since the loop always takes the
    # highest run id. Say so rather than letting `forge go` look broken.
    latest = store.latest_run()
    if latest is not None and int(latest["id"]) != run_id:
        print(
            f"\nwarning: run {latest['id']} is newer and `forge go` will pick it "
            f"up instead of run {run_id}."
        )
        return 0

    if args.go:
        return cmd_go(
            argparse.Namespace(
                root=args.root,
                plan="",
                goal="",
                no_ui=args.no_ui,
                open=False,
                retries=args.retries,
                no_respec=False,
                wait=None,
            )
        )

    print("\nStart it with: forge go")
    return 0


def cmd_bug(args: argparse.Namespace) -> int:
    """Turn a prose bug report into one ticket the loop can reproduce and fix.

    Separate from `ingest` because the shapes differ at the root. Ingest turns
    a document into a backlog and takes the author's acceptance criteria as the
    contract; a report is one symptom, its file scope is unknown, and its
    contract is written afterwards by a test that has to fail first.
    """
    report = _read_report(args)
    if not report.strip():
        sys.exit("error: the report is empty. Say what you saw and what you expected.")

    config = _load(args.root)
    store = _store(config)

    try:
        provider = config.provider_for("planner")
    except (ConfigError, ValueError) as exc:
        sys.exit(f"error: {exc}")

    # Gathered here rather than asked of the model: the planner has no
    # filesystem, and the file that needs changing is the thing being looked
    # for. See forge/evidence.py.
    found = evidence.gather(config.root, report)
    if not found:
        # Says which of the two it is, because the fixes are different: an
        # empty project is a wrong --root, and a project whose files are all
        # ignored is a .gitignore doing more than it meant to.
        print(
            f"warning: nothing to search in {config.root}. Either that is the "
            f"wrong directory, or everything in it is ignored. The ticket will "
            f"be scoped from the report alone, and the planner will probably "
            f"refuse it."
        )

    budget = max(2048, provider.capabilities().max_output_tokens // 4)

    # Two passes, because one is not enough for the report a person actually
    # files. "The score sometimes stops updating" names no function and no
    # file, so a single call would be choosing scope from a list of filenames.
    # The first pass spends a little context deciding what to read; the second
    # writes the ticket against the contents.
    sources: dict[str, str] = {}
    if found:
        print("Looking for where the problem lives...")
        try:
            located = provider.complete(
                locate_prompt(report, found), max_tokens=budget, temperature=0.0
            )
            candidates = parse_locate(located.text, evidence.repo_files(config.root))
        except (ProviderError, ValueError) as exc:
            # Best effort by design: the second pass still has the file list
            # and the grep hits, which is what it had before this existed.
            print(f"warning: could not narrow the search ({exc}); reading nothing first.")
            candidates = []
        if candidates:
            sources = evidence.read_files(config.root, candidates)
            print(f"  reading {', '.join(sources)}")

    print("Writing the ticket...")
    try:
        completion = provider.complete(
            bug_prompt(report, found, sources), max_tokens=budget, temperature=0.1
        )
        fields = parse_bug(completion.text)
    except (ProviderError, ValueError) as exc:
        sys.exit(f"error: {exc}")

    ticket_id = args.id or _next_bug_id(store)
    ticket = Ticket(
        ticket_id=ticket_id,
        title=fields["title"] or "Bug report",
        kind=TICKET_BUG,
        # Never delegated blindly: a bug in code the project marked off-limits
        # is exactly the kind that wants a person.
        route="claude-only"
        if any(matches_any(p, config.never_delegate) for p in fields["allowed_files"])
        else "delegate",
        spec=fields["spec"],
        allowed_files=fields["allowed_files"],
        reference_files=fields["reference_files"],
        criteria=fields["criteria"],
        # What the reproduction should assert, read by the tester before it
        # writes anything and by the executor as the shape of the fix.
        context=fields["reproduce"],
    )

    run_id = store.create_run(f"bug: {ticket.title}", source=report[:2000])
    store.add_tickets(run_id, [ticket])
    store.log(
        run_id,
        f"{ticket.ticket_id}: filed from a bug report. It must be reproduced "
        f"before anything is allowed to fix it.",
        kind="ticket",
        data={"report": report[:2000]},
    )

    try:
        write_tickets(config.tickets_dir, [ticket])
    except OSError as exc:
        print(f"warning: could not write the ticket file ({exc}).")

    print(f"\nRun {run_id} — {ticket.ticket_id}: {ticket.title}")
    print(f"  scope     {', '.join(ticket.allowed_files) or '(none named)'}")
    _warn_uncovered(config, ticket.allowed_files)
    if ticket.reference_files:
        print(f"  reads     {', '.join(ticket.reference_files)}")
    if ticket.context:
        print(f"  reproduce {ticket.context}")
    print(f"\n{ticket.spec}\n")
    if ticket.route != "delegate":
        print(
            "Routed claude-only: the scope touches a neverDelegate path, so the "
            "loop will leave it for you."
        )

    if args.go:
        return cmd_go(
            argparse.Namespace(
                root=args.root, plan="", goal="", no_ui=args.no_ui, open=False,
                retries=None, no_respec=False, wait=None,
            )
        )
    print("Read the scope above, then: forge go")
    return 0


def _warn_uncovered(config: Config, paths: list[str]) -> list[str]:
    """Say up front when planned work lands in a language nothing tests.

    The loop blocks such a ticket when it reaches it, which is correct and
    late: by then a run exists and a human has walked away. Ingest and `forge
    bug` know the scope before anything is spent, and that is the cheapest
    moment to hear it.
    """
    if not config.commands_for("test"):
        return []
    uncovered = sorted(
        {
            suffix
            for path in paths
            if not any(ch in path for ch in "*?[")
            and (suffix := Path(path).suffix.lower())
            and suffix in _SOURCE_SUFFIXES
            and not config.covers("test", suffix)
        }
    )
    if uncovered:
        print(
            f"\nwarning: this writes {', '.join(uncovered)}, which no test "
            f"command covers. The loop will block rather than check it by "
            f"reading:\n  forge toolchain --language {uncovered[0]}"
        )
    return uncovered


def _read_report(args: argparse.Namespace) -> str:
    """The report itself, from an argument, a file, or stdin."""
    if args.file:
        try:
            return Path(args.file).read_text(encoding="utf-8")
        except OSError as exc:
            sys.exit(f"error: cannot read {args.file} ({exc})")
    if args.report == "-":
        return sys.stdin.read()
    return args.report or ""


def _next_bug_id(store: Store) -> str:
    """The next free `BUG-nnn`, counting across every run in this repository.

    Across runs rather than within one, because the id names a test file and
    two runs reusing `BUG-001` would have the second overwrite the first's
    reproduction — throwing away the evidence for a bug nobody said was fixed.
    """
    used = set()
    for run in store.list_runs(limit=200):
        for ticket in store.list_tickets(int(run["id"])):
            match = re.fullmatch(r"BUG-(\d+)", ticket.ticket_id)
            if match:
                used.add(int(match.group(1)))
    return f"BUG-{max(used) + 1 if used else 1:03d}"


def cmd_toolchain(args: argparse.Namespace) -> int:
    """Show what runs against each language, and set up what does not.

    The other half of the gate. A ticket in a language nothing tests blocks
    with a note pointing here, and a note pointing at a command that does not
    exist is worse than no note.

    Nothing is written without being asked for. Detection reads the repo's own
    CI, build files and contributing guide and proposes; `--accept` or `--set`
    writes. Changing what verification means is not a decision the loop gets to
    make while nobody is watching.
    """
    config = _load(args.root)

    if not args.language:
        uncovered = _report_coverage(config)
        if uncovered:
            print(
                f"\nNo test command covers: {', '.join(uncovered)}. Work in "
                f"{'that language' if len(uncovered) == 1 else 'those languages'} "
                f"cannot be checked by anything here, so the loop blocks tickets "
                f"that write it.\n  forge toolchain --language {uncovered[0]}"
            )
        return 0

    language = args.language if args.language.startswith(".") else f".{args.language.lstrip('.')}"
    suffixes = normalize_language(args.language)
    kind = args.kind

    if args.skip:
        # On the record, and readable as one: `false` cannot be mistaken for a
        # command, and the gate stops asking about a language somebody has
        # already decided about.
        return _write_command(config, kind, language, False, suffixes=suffixes)

    if args.set:
        return _write_command(config, kind, language, args.set, suffixes=suffixes)

    try:
        provider = config.provider_for("planner")
    except (ConfigError, ValueError) as exc:
        sys.exit(f"error: {exc}")

    print(f"Reading this repository for its {language} commands...")
    detection = toolchain.detect(config.root, provider, language=language)
    if not detection.ok:
        sys.exit(
            f"error: {detection.error}\n"
            f"Set it by hand instead:\n"
            f'  forge toolchain --language {language} --set "<command>"'
        )

    proposed = detection.commands.get(kind, "")
    if not proposed:
        print(
            f"Nothing in this repository states a {kind} command for {language}"
            + (f" (read: {', '.join(detection.evidence)})" if detection.evidence else "")
            + ".\nSet it by hand:\n"
            f'  forge toolchain --language {language} --set "<command>"'
        )
        return 1

    print(f"\n  {kind} for {language}:  {proposed}")
    if detection.source:
        print(f"  from: {detection.source}")
    print(f"  confidence: {detection.confidence}")

    if not args.accept:
        print(
            "\nNothing was written. Accept it with:\n"
            f"  forge toolchain --language {language} --accept"
        )
        return 0
    return _write_command(config, kind, language, proposed, suffixes=suffixes)


def _write_command(
    config: Config, kind: str, language: str, command: str | bool, suffixes=()
) -> int:
    """Put one language's command into config, turning a string into a map.

    An existing single command becomes the `*` entry rather than being
    replaced: it was covering everything, and this is adding a language beside
    it, not taking its place.
    """
    existing = config.commands.get(kind, "")
    block = {ANY_LANGUAGE: existing} if isinstance(existing, str) and existing else dict(existing or {})
    for key in suffixes or (language,):
        if key != ANY_LANGUAGE:
            block[key] = command
    config.commands[kind] = block

    try:
        config.validate()
    except ConfigError as exc:
        sys.exit(f"error: {exc}")

    written = config.write()
    print(f"\nWrote {written}")
    if command is False:
        print(f"  commands.{kind}[{language}] = false  (nothing runs it, on purpose)")
        print("\nTickets writing it are no longer blocked. Their work is checked")
        print("at review rather than by running anything, and the run says so.")
        return 0
    print(f'  commands.{kind}[{language}] = "{command}"')
    print("\nCheck it runs, then requeue whatever was blocked:")
    print("  forge doctor")
    print("  forge retry --ticket <ID>")
    return 0


def cmd_criteria(args: argparse.Namespace) -> int:
    """Show the criteria respec proposed and refused, and adopt one.

    Respec may not add to the standard it is judged against — it runs on a
    ticket that has just failed, and a ticket that keeps failing does not need
    a higher bar. But a refused proposal is sometimes right, and until now
    accepting one meant editing `plan.md` and re-ingesting the whole backlog:
    a fresh run, and the work that had already passed done again. This adopts
    it in place, as the plan's own, so the ratchet protects it from here on.
    """
    config = _load(args.root)
    store = _store(config)

    run = store.get_run(args.run) if args.run else store.latest_run()
    if run is None:
        sys.exit("error: no runs yet. Ingest a spec first:\n  forge ingest plan.md")
    run_id = int(run["id"])

    pending = store.proposed_criteria(run_id)
    if args.ticket:
        pending = {k: v for k, v in pending.items() if k == args.ticket}

    if not args.accept and not args.accept_all:
        return _print_pending(run_id, pending, args.ticket)

    if not args.ticket:
        sys.exit(
            "error: name the ticket to accept for.\n"
            "  forge criteria             list what is outstanding\n"
            "  forge criteria TT-006 --accept 1"
        )
    outstanding = pending.get(args.ticket, [])
    if not outstanding:
        sys.exit(
            f"error: run {run_id} has nothing outstanding for {args.ticket}. "
            f"List what there is with `forge criteria`."
        )

    if args.accept_all:
        chosen = list(outstanding)
    else:
        chosen = []
        for index in args.accept:
            if not 1 <= index <= len(outstanding):
                sys.exit(
                    f"error: {args.ticket} has {len(outstanding)} proposal(s); "
                    f"there is no {index}."
                )
            chosen.append(outstanding[index - 1])

    ticket, adopted = store.promote_criteria(run_id, args.ticket, chosen)
    if ticket is None:
        sys.exit(f"error: run {run_id} has no ticket {args.ticket}.")
    if not adopted:
        print(f"{args.ticket} already carries every criterion named.")
        return 0

    store.log(
        run_id,
        f"{ticket.ticket_id}: a human adopted {len(adopted)} criterion(s) into "
        f"the plan's contract. They are plan-authored from here — respec may "
        f"not drop or reword them.",
        kind="ticket",
        data={"adopted": adopted, "ticket": ticket.ticket_id},
    )

    # The tickets on disk are what a human reads to understand the run, and a
    # contract that has changed in the database alone makes them lie.
    try:
        write_tickets(config.tickets_dir, [ticket])
    except OSError as exc:
        print(f"warning: could not rewrite {args.ticket}.md ({exc}).")

    print(f"{ticket.ticket_id}: adopted {len(adopted)} criterion(s) as the plan's.")
    for criterion in adopted:
        print(f"      {criterion}")
    if ticket.status == TICKET_DONE:
        # Green against the old contract, which no longer exists. Said rather
        # than done: requeuing a passed ticket without being asked is how a
        # backlog gets redone underneath someone.
        print(
            f"\n{ticket.ticket_id} already passed, under the contract without "
            f"this. Hold it to the new one with:\n"
            f"  forge retry --ticket {ticket.ticket_id}"
        )
    return 0


def _print_pending(
    run_id: int, pending: dict[str, list[str]], only: str
) -> int:
    if not pending:
        scope = f" for {only}" if only else ""
        print(
            f"Run {run_id}: nothing outstanding{scope}. Respec has proposed no "
            f"criterion the plan states nowhere, or every one has been settled."
        )
        return 0

    print(f"Run {run_id}: criteria respec proposed and the loop refused.\n")
    for ticket_id, criteria in sorted(pending.items()):
        print(f"{ticket_id}")
        for index, criterion in enumerate(criteria, start=1):
            print(f"  {index}  {criterion}")
        print()
    first = sorted(pending)[0]
    print(
        "Each is something a failing ticket asked to be judged on that nobody "
        "wrote down. Adopt one — it becomes the plan's, and respec may not "
        "touch it after that:\n"
        f"  forge criteria {first} --accept 1"
    )
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

    # Overrides for this invocation, never written back. The usual reason to
    # reach for them is a run already in progress: the loop bound its dashboard
    # at startup and will not rebind, so a second dashboard on another address
    # or port is how you watch it from elsewhere without restarting it. Setting
    # them on `config.ui` before `serve` keeps the exposure warning and the
    # printed URL describing what actually got bound.
    if args.host is not None:
        config.ui.host = args.host
    if args.port is not None:
        config.ui.port = args.port

    try:
        ui_server.serve(config, store)
    except OSError as exc:
        # Overwhelmingly a port already taken — most often by the very run this
        # dashboard was meant to watch, which holds ui.port for its lifetime.
        sys.exit(
            f"error: could not serve on {config.ui.host}:{config.ui.port} — {exc}\n"
            f"If a run is in progress it already holds that port; pick another "
            f"with --port."
        )

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
    p.add_argument(
        "--no-preflight",
        action="store_true",
        help="skip the model probe before the first ticket (default: probe, so "
        "a dead endpoint fails in seconds instead of one ticket at a time)",
    )
    p.add_argument("--open", action="store_true", help="open the dashboard in a browser")
    p.add_argument(
        "--retries",
        type=int,
        default=None,
        metavar="N",
        help="requeue and respec unfinished tickets N more times when the run "
        "ends short of done; -1 keeps going until it is clean or you stop it "
        "(default: loop.retryCycles, normally 0)",
    )
    p.add_argument(
        "--no-respec",
        action="store_true",
        help="with --retries, requeue the tickets as written instead of having "
        "the planner revise them first",
    )
    p.add_argument(
        "--wait",
        dest="wait",
        action="store_true",
        default=None,
        help="keep the dashboard serving after the run ends (the default when "
        "a terminal is attached)",
    )
    p.add_argument(
        "--no-wait",
        dest="wait",
        action="store_false",
        help="exit as soon as the run ends, closing the dashboard with it",
    )
    p.set_defaults(func=cmd_go)

    p = sub.add_parser("status", help="one-shot summary of the current run")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("prune", help="delete the artifact trees of old runs")
    p.add_argument(
        "--keep", type=int, default=5, help="runs to keep, newest first (default: 5)"
    )
    p.add_argument(
        "--dry-run", action="store_true", help="list what would be removed and stop"
    )
    p.set_defaults(func=cmd_prune)

    p = sub.add_parser(
        "toolchain", help="show what tests each language, and set up what nothing does"
    )
    p.add_argument(
        "--language",
        default="",
        metavar="EXT",
        help="the language to set up, as an extension or a name (.js, javascript)",
    )
    p.add_argument(
        "--kind",
        default="test",
        choices=("test", "lint", "typecheck"),
        help="which command to set (default: test)",
    )
    p.add_argument(
        "--set", default="", metavar="CMD", help="write this command, without asking a model"
    )
    p.add_argument(
        "--accept", action="store_true", help="write the detected command to config"
    )
    p.add_argument(
        "--skip",
        action="store_true",
        help="declare that nothing runs this language, so tickets writing it "
        "stop being blocked (build scripts, shell wrappers)",
    )
    p.set_defaults(func=cmd_toolchain)

    p = sub.add_parser(
        "bug", help="turn a bug report into a ticket the loop must reproduce first"
    )
    p.add_argument(
        "report",
        nargs="?",
        default="",
        help="the report itself, or - to read it from stdin",
    )
    p.add_argument("--file", default="", metavar="PATH", help="read the report from a file")
    p.add_argument("--id", default="", metavar="ID", help="ticket id (default: the next BUG-nnn)")
    p.add_argument("--go", action="store_true", help="start the loop straight after")
    p.add_argument("--no-ui", action="store_true", help="with --go, skip the dashboard")
    p.set_defaults(func=cmd_bug)

    p = sub.add_parser(
        "criteria",
        help="show the criteria respec proposed and refused, and adopt one",
    )
    p.add_argument(
        "ticket",
        nargs="?",
        default="",
        metavar="ID",
        help="show only this ticket's proposals; required to accept one",
    )
    p.add_argument("--run", type=int, default=0, help="run id (default: the latest)")
    p.add_argument(
        "--accept",
        action="append",
        type=int,
        default=[],
        metavar="N",
        help="adopt the numbered proposal as the plan's own; repeatable",
    )
    p.add_argument(
        "--accept-all",
        action="store_true",
        help="adopt every outstanding proposal for the named ticket",
    )
    p.set_defaults(func=cmd_criteria)

    p = sub.add_parser("retry", help="put failed tickets back on the backlog")
    p.add_argument("--run", type=int, default=0, help="run id (default: the latest)")
    p.add_argument(
        "--ticket",
        action="append",
        default=[],
        metavar="ID",
        help="retry this ticket whatever its status; repeatable",
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="also reset tickets that succeeded, to redo the whole run",
    )
    p.add_argument(
        "--respec",
        action="store_true",
        help="have the planner revise each ticket from why it failed",
    )
    p.add_argument(
        "--respec-criteria",
        action="store_true",
        help="also let the respec rewrite the acceptance criteria. Off by "
        "default: they are the contract the attempts failed against, and a "
        "ticket that keeps rewriting its own drifts away from the plan",
    )
    p.add_argument("--go", action="store_true", help="start the loop straight after")
    p.add_argument(
        "--retries",
        type=int,
        default=None,
        metavar="N",
        help="with --go, requeue and respec unfinished tickets N more times "
        "without asking; -1 keeps going until the backlog is clean or you stop it",
    )
    p.add_argument("--no-ui", action="store_true", help="with --go, skip the dashboard")
    p.set_defaults(func=cmd_retry)

    for name, help_text in (
        ("pause", "pause after the current step"),
        ("resume", "resume a paused loop"),
        ("stop", "stop after the current step"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.set_defaults(func=cmd_control, command=name)

    p = sub.add_parser(
        "models", help="write Ollama Modelfiles for the configured models"
    )
    p.set_defaults(func=cmd_models)

    p = sub.add_parser("ui", help="serve the dashboard without running the loop")
    p.add_argument("--open", action="store_true", help="open the dashboard in a browser")
    p.add_argument(
        "--host",
        default=None,
        help=(
            "bind address for this invocation only; nothing is written back to "
            "config. The dashboard has no authentication and its stop button "
            "ends the run, so anything that can reach a non-loopback address "
            "can control the daemon"
        ),
    )
    p.add_argument(
        "--port",
        type=int,
        default=None,
        help="port for this invocation only (default: ui.port from config)",
    )
    p.set_defaults(func=cmd_ui)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
