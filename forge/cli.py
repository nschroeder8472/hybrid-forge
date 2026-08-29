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
    forge replay [--changed]       re-read past output with today's parsers
    forge prune [--keep N]         delete the artifact trees of old runs
    forge models                   write the llama.cpp preset the local models serve from
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

from . import evidence, presets, replay, respec, toolchain, wizard
from .artifacts import ARTIFACTS_DIR, GITIGNORE_LINES
from .config import (
    ANY_LANGUAGE,
    REPO_ROOT,
    Config,
    ConfigError,
    Workspace,
    default_config,
    normalize_language,
    normalize_workspace_root,
)
from .ingest import ingest as ingest_document
from .ingest import undeclared_order, write_tickets
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
    # and config are what belong in version control. Written from the same
    # list `Artifacts` repairs against, so a fresh repo does not start one
    # entry short and get patched on its first run.
    gitignore.write_text(GITIGNORE_LINES, encoding="utf-8")

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

    _report_preset(config, wrote_config=True)

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


def _report_preset(config: Config, *, wrote_config: bool) -> int:
    """Write the llama.cpp preset for the local models and say how to serve it.

    Generated rather than remembered, because the numbers in it have to agree
    with `config.json` and keeping two files in step by hand is where the
    silent failures live. `ctx-size` here and `contextWindow` there are the
    pair that matters: forge plans a prompt against config and the server
    truncates against the preset, and what a too-large window loses is the
    front of the prompt — the system message and the spec.

    The file is written; `llama-server` is not started. It owns the GPU and
    outlives any one forge command, so starting it is a decision for whoever is
    reading this.
    """
    try:
        path = presets.write(config)
    except Exception as exc:  # noqa: BLE001 - never fail an init over this
        print(f"\n(could not generate the llama.cpp preset: {exc})")
        return 0
    if path is None:
        return 0

    entries = presets.plan(config)
    where = "Also wrote" if wrote_config else "Wrote"
    print(f"\n{where} a llama.cpp preset with {len(entries)} model(s): {path}")
    for entry in entries:
        print(f"  {entry.alias:<12} {entry.model_id:<24} {entry.path}")

    print("\nServe it with:")
    print(f'  llama-server --models-preset "{path}" --models-max 1')
    print(
        "\n--models-max 1 keeps one checkpoint resident at a time. Raise it only\n"
        "if every model in the preset fits in VRAM together; the role that finds\n"
        "out otherwise is the one whose child server exits during a run."
    )
    return len(entries)


def cmd_models(args: argparse.Namespace) -> int:
    config = _load(args.root)
    if not _report_preset(config, wrote_config=False):
        print(
            "No llama.cpp models in this config, so there is no preset to write. "
            "Cloud endpoints are configured entirely in config.json."
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
    """What runs against each language of each build, and what does not.

    A repository is not one language and not one build, and the loop's
    verification is only as wide as its commands. Printing the matrix is what
    makes a gap visible before it becomes a ticket checked by reading.

    The list of files no workspace owns is the other half, and it is the one
    worth scanning first. A root that resolves to nothing owns nothing, every
    file falls through to whichever workspace does match, and the config looks
    entirely reasonable while a whole build goes unverified — the typo is
    refused at load, but a root that is real and simply wrong is not, and this
    is where it shows.

    Returns the extensions with no test command, so the caller can say so.
    """
    census = Counter(
        (workspace.root if (workspace := config.workspace_for(path)) else None, suffix)
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

    uncovered: list[str] = []
    for workspace in config.workspaces:
        counts = {
            suffix: count
            for (root, suffix), count in census.items()
            if root == workspace.root
        }
        # Named only when there is more than one, so a single-build project's
        # doctor output reads exactly as it did before workspaces existed.
        if len(config.workspaces) > 1:
            print(f"\n  workspace {workspace.root}")
        if not counts:
            print("    (no source files)")
            continue
        uncovered.extend(_print_matrix(config, workspace, counts))

    _report_typecheck_gaps(config, census)
    _report_undeclared_builds(config)
    _report_duplicated_typecheck(config)

    orphans = sorted(
        {
            suffix
            for (root, suffix), _count in census.items()
            if root is None
        }
    )
    if orphans:
        print(
            f"\n  owned by no workspace: {', '.join(orphans)}\n"
            f"    Nothing lints, type-checks or tests these. A ticket writing "
            f"one is refused at ingest."
        )
    return uncovered


def _report_undeclared_builds(config: Config) -> None:
    """Directories that hold their own manifest and are not declared as builds.

    Discovery proposes and the person decides — `_ask_builds` says so, and
    saying no is a real answer. What was missing is the price of saying no,
    which is not visible from the config and is not small.

    A repository configured as one workspace runs every `*` command against
    every ticket, whatever the ticket writes. On one run that meant a
    TypeScript ticket under `tools/path_forge` paid for the whole Godot gdUnit
    suite on every attempt: 908 runs at about 8.2 seconds, 2.1 hours of a
    18-hour run, and 229 MB of passing output under `.hybridforge/artifacts`
    for a single ticket. Nothing failed and nothing was learned.

    Reported, never gated. A monorepo whose subdirectories genuinely share one
    toolchain is a real shape and this is wrong about it.
    """
    if len(config.workspaces) > 1:
        return
    found = [
        build
        for build in toolchain.discover_workspaces(config.root)
        if build != REPO_ROOT
    ]
    if not found:
        return
    print(
        f"\n  undeclared builds: {', '.join(found)}\n"
        f"    Each holds its own manifest and none is declared under "
        f"`workspaces`,\n"
        f"    so this repository verifies as one. Every `*` command runs on "
        f"every\n"
        f"    ticket — including the ones whose files it cannot see.\n"
        f"    Declare them:  forge init   (it offers this when it finds two)"
    )


def _report_duplicated_typecheck(config: Config) -> None:
    """Test commands that re-run the type check the previous step just ran.

    Cheap in seconds and worth a line anyway: the two kinds are filled in
    independently, by a model or by hand, and a `test` that opens with the
    whole `typecheck` command means one of them is not what the operator
    thinks it is. The observed case was `tsc --noEmit -p tools/path_forge &&
    npm run test` sitting under `test[".ts"]` while `typecheck[".ts"]` held
    `tsc --noEmit -p tools/path_forge` on its own — the type check ran twice
    per attempt, 757 times in one run.
    """
    for workspace in config.workspaces:
        checks = workspace.commands_for("typecheck")
        tests = workspace.commands_for("test")
        # One finding per command pair, not per extension. The same pair is
        # normally set for every extension of a language, and printing it four
        # times for `.ts .tsx .mts .cts` buries the other checks.
        duplicated: dict[tuple[str, str], list[str]] = {}
        for suffix, test in sorted(tests.items()):
            check = checks.get(suffix, "")
            if not check or not test or check == test:
                continue
            if not test.startswith(check):
                continue
            duplicated.setdefault((check, test), []).append(suffix)
        where = (
            ""
            if workspace.is_repo_root or len(config.workspaces) == 1
            else f" in {workspace.root}"
        )
        for (check, test), suffixes in duplicated.items():
            print(
                f"\n  test[{', '.join(suffixes)}]{where} re-runs the typecheck "
                f"command:\n"
                f"    typecheck  {check}\n"
                f"    test       {test}\n"
                f"    The check already ran and passed before this step "
                f"started. Drop the\n"
                f"    prefix, or clear the `typecheck` entry if the test "
                f"command is the check."
            )


def _report_typecheck_gaps(config: Config, census) -> None:
    """Languages whose test command does not type-check them, and nothing else does.

    Reported, never gated — the same weight `LANGUAGE-COVERAGE.md` gives lint,
    and for the same reason: a project that has decided not to type-check its
    Python has decided, and `--skip` says so on the record.

    Worth reporting at all because of what an empty one cost. `cargo test` and
    `go test` compile the project, so a missing entry there is a redundancy;
    `npm test` loads the modules its tests reach and nothing else, so a missing
    entry there is a hole the size of every file no test imports. One run put
    4,000 lines through that hole.
    """
    gaps: list[str] = []
    # `root` is `None` for a file no workspace owns, which sorts against a
    # string and raises. Those are the orphan list's business, not this one's.
    owned = {key: count for key, count in census.items() if key[0] is not None}
    for (root, suffix), _count in sorted(owned.items()):
        workspace = next((w for w in config.workspaces if w.root == root), None)
        if workspace is None:
            continue
        suggested = workspace.unchecked(suffix)
        if not suggested:
            continue
        where = "" if workspace.is_repo_root or len(config.workspaces) == 1 else f" in {root}"
        gaps.append(f"    {suffix}{where}  —  try `{suggested[0]}`")
    if not gaps:
        return
    print(
        "\n  no type check:\n"
        + "\n".join(gaps)
        + "\n    Their test command loads the modules its tests reach and nothing\n"
        "    else, so a file no test imports is parsed by nothing here.\n"
        "    Set one:   forge toolchain --kind typecheck --language <lang>\n"
        "    Or not:    forge toolchain --kind typecheck --language <lang> --skip"
    )


def _print_matrix(config: Config, workspace, census: dict[str, int]) -> list[str]:
    """One build's language matrix. Returns its uncovered extensions."""
    width = max(max(len(suffix) for suffix in census), 8)
    print("\n  language  files  test / lint")
    uncovered: list[str] = []
    for suffix, count in sorted(census.items(), key=lambda item: (-item[1], item[0])):
        test, how = workspace.covering("test", suffix)
        lint, _ = workspace.covering("lint", suffix)
        if not test and not workspace.exempt("test", suffix):
            uncovered.append(suffix)
        # A catch-all that cannot run the language is worse than none: it reads
        # as coverage in every report and proves nothing about the files.
        if workspace.exempt("test", suffix):
            shown = "(none — declared)"
        else:
            shown = test or (
                "(no test command" + (f" — the one configured {how})" if how else ")")
            )
        print(
            f"  {suffix:<{width}}  {count:>5}  {shown}"
            + ("  (catch-all)" if how == "catch-all" else "")
            + (f"  |  {lint}" if lint else "")
        )
    return uncovered


# Extensions worth reporting coverage for: languages whose behavior a test
# could assert. A stylesheet with no runner is not a gap.
_SOURCE_SUFFIXES = frozenset(
    """.rs .py .js .mjs .cjs .jsx .ts .tsx .go .rb .java .kt .swift .c .cc .cpp
    .h .hpp .cs .php .sh .ps1 .lua .ex .exs .scala .dart .gd""".split()
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

    # Same widening a bug ticket gets, for the same reason: what a ticket may
    # read is not what it may write, and the planner names one list for both.
    # A feature ticket handed only the file it writes cannot check a call
    # against the function it calls. Costs nothing on a greenfield plan —
    # `reading_scope` keeps only files that exist, and on an empty repository
    # that is none of them.
    #
    # The specification itself goes first, when the backlog was planned from
    # one. `reading_scope` takes `reference` in order and caps the rest, so
    # first is what guarantees it survives.
    origin = _source_reference(config, args.source, how)
    for ticket in tickets:
        references = list(ticket.reference_files)
        if origin and origin not in references:
            references.insert(0, origin)
        ticket.reference_files = evidence.reading_scope(
            config.root, ticket.allowed_files, references
        )

    unverifiable = _workspace_problems(config, tickets)
    if unverifiable:
        sys.exit(
            "error: this backlog cannot be verified as written:\n  "
            + "\n  ".join(unverifiable)
            + "\n\nNothing was ingested. `forge doctor` shows what each build "
            "covers."
        )

    _warn_missing_manifests(config, tickets)

    # Said after `derive_needs` has run, so a shared writable file has already
    # been ordered and is not what this is about.
    shape = undeclared_order(config.root, tickets)
    if shape:
        print(f"\nwarning: {shape}")

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


def _source_reference(config: Config, source: str, how: str) -> str:
    """The specification a planned backlog came from, as a readable path.

    Empty for anything that is not one: stdin has no path, a document outside
    the repository cannot be pasted from one, and a backlog *parsed* from a
    ticket-shaped document already carries that document's words verbatim —
    attaching it there would show every ticket every other ticket's spec, for
    nothing.

    The planned path is where it matters, because that is the lossy one. A
    planner reads a specification and writes a summary of it; the executor is
    then handed the summary and never sees the source. One run put a
    seven-hundred-line spec through that: section 2 of it was labelled
    normative and held the complete legal alphabet as a table of eighteen
    characters, the seven exact error strings, and the order the checks run in.
    What reached the executor was "reject bad input with exact error strings",
    naming none of them, and every ticket in the backlog had
    `reference_files: []`. The document that generated a backlog is, by
    construction, the most relevant reference for every ticket in it.
    """
    if how != "planned" or source == "-":
        return ""
    try:
        path = Path(source).resolve()
        relative = path.relative_to(config.root)
    except (OSError, ValueError):
        return ""
    return relative.as_posix() if path.is_file() else ""


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
    if getattr(args, "allow_red_baseline", False):
        config.loop.require_green_baseline = False
    if getattr(args, "no_quarantine", False):
        config.loop.quarantine_failed = False

    store = _store(config)

    if args.plan:
        # Same path as `forge ingest`, for a plan already reviewed elsewhere.
        namespace = argparse.Namespace(
            root=args.root, source=args.plan, goal=args.goal, replan=False
        )
        if cmd_ingest(namespace) != 0:
            return 1
        store = _store(config)

    # The whole queue, oldest first, not just the newest run. Anything filed
    # behind a run that then blocked used to wait for a human to notice it, and
    # `forge status` shows one run, so it was not on screen to be noticed.
    runs = store.resumable_runs()
    if not runs:
        # Nothing is queued anywhere, but a run exhausted with blocked tickets
        # is still worth entering: it re-reports what needs a human, which is
        # the long-standing answer to `forge go` on a spent backlog.
        stalled = store.resumable_run()
        runs = [stalled] if stalled is not None else []
    if not runs:
        sys.exit(
            "error: no run to work on. Ingest a spec first:\n"
            "  forge ingest plan.md      (or: forge go --plan plan.md)"
        )

    url = ""
    if config.ui.enabled and not args.no_ui:
        ui_server.serve(config, store)
        url = ui_server.url_for(config)
        print(f"Dashboard: {url}")
        if args.open:
            webbrowser.open(url)

    if len(runs) > 1:
        print(f"{len(runs)} runs queued: {', '.join(str(r['id']) for r in runs)}.")
    if config.loop.retry_cycles:
        cycles = (
            "until the backlog is clean or you stop it"
            if config.loop.retry_cycles < 0
            else f"up to {config.loop.retry_cycles} more time(s)"
        )
        respec_note = "with a respec" if config.loop.respec_on_retry else "without a respec"
        print(f"Unfinished tickets will be requeued {cycles}, {respec_note}.")
    print("Ctrl-C stops after the current step.\n")

    # Shared across the queue so `maxRuntimeSeconds` caps the whole unattended
    # session, which is what it means, rather than resetting per run.
    started_at = time.time()
    outcomes: list[tuple[int, str]] = []

    for index, run in enumerate(runs):
        run_id = int(run["id"])
        counts = store.ticket_counts(run_id)
        remaining = counts.get("pending", 0) + counts.get("running", 0)
        if run["status"] == "stopped":
            print(f"Resuming run {run_id} ({remaining} ticket(s) left).")

        if index:
            print()
        print(f"Run {run_id}: {run['goal']}")

        # One per run. The orchestrator accumulates run-scoped state — which
        # tickets authored tests, whether project memory has gone away — and
        # carrying it into the next run would report the last run's tickets in
        # this one's coverage summary.
        orchestrator = Orchestrator(config, store, started_at=started_at)
        try:
            final = orchestrator.run(run_id)
        except KeyboardInterrupt:
            store.set_control(CONTROL_KEY, CONTROL_STOP)
            print("\nStopping after the current step…")
            final = "stopped"
        outcomes.append((run_id, final))

        counts = store.ticket_counts(run_id)
        label = f"Finished run {run_id}: {final}" if len(runs) > 1 else f"Finished: {final}"
        print(f"\n{label}")
        print(f"  tickets: {json.dumps(counts)}")
        spent = store.get_control(retries_key(run_id), "0")
        if spent != "0":
            print(f"  retry cycles: {spent}")

        # `blocked` is not a reason to abandon the rest — that is the stranding
        # this queue exists to end, and the runs behind it are separate work.
        # `stopped` and `failed` are: the first is a person asking the loop to
        # stop, and the second means something outside the backlog is wrong.
        if final in ("stopped", "failed"):
            skipped = len(runs) - index - 1
            if skipped:
                print(f"  {skipped} queued run(s) left untouched.")
            break

    if len(runs) > 1:
        worked = ", ".join(f"{run_id} {status}" for run_id, status in outcomes)
        print(f"\nFinished: {worked}")
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

    # Green only if the whole queue is green. A drain that broke off early left
    # runs untouched, which is not a success however the last one ended.
    drained = len(outcomes) == len(runs)
    return 0 if drained and all(status == "done" for _, status in outcomes) else 1


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
            # So a revised read scope is checked against the tree rather than
            # taken on the planner's word for where a file lives.
            root=config.root,
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


def cmd_replay(args: argparse.Namespace) -> int:
    """Re-read a past run's recorded output with the parsers as they stand now.

    The check that unit tests cannot make. A fixture asserts what its author
    believed the output looked like; the artifacts hold what it actually was,
    and two parser changes in one afternoon passed their tests and were wrong
    against the first real recording they met.

    Read-only. Nothing here writes to the repository, the database, or the
    artifacts.
    """
    config = _load(args.root)
    store = _store(config)

    findings, source = replay.replay(
        config, store, run_id=args.run, ticket=args.ticket, lens=args.lens
    )
    if not findings:
        print(
            "Nothing recorded to replay. Artifacts live in "
            f"{config.config_dir / 'artifacts'} and are written as a run works; "
            "a run from before they existed leaves only the steps table, and "
            "`forge prune` removes old ones."
        )
        return 0

    if args.json:
        print(
            json.dumps(
                [
                    {
                        "run": f.record.run_id,
                        "ticket": f.record.ticket_id,
                        "attempt": f.record.attempt,
                        "step": f.record.step,
                        "lens": f.lens,
                        "now": f.now,
                        "then": f.then,
                        "changed": f.changed,
                        "note": f.note,
                        "origin": f.record.origin,
                    }
                    for f in findings
                ],
                indent=2,
            )
        )
        return 1 if any(f.changed for f in findings) else 0

    changed = [f for f in findings if f.changed]
    shown = changed if args.changed else findings

    print(f"Replaying {len(findings)} record(s) from {source}.\n")
    for finding in shown:
        record = finding.record
        mark = "!" if finding.changed else " "
        where = f"run {record.run_id} {record.ticket_id}"
        if record.attempt:
            where += f" attempt {record.attempt}"
        print(f"{mark} {where} — {record.step} [{finding.lens}]")
        print(f"      now:  {finding.now}")
        if finding.then:
            print(f"      then: {finding.then}")
        if finding.note:
            print(f"      note: {finding.note}")
        print(f"      {record.origin}")

    if args.changed and not changed:
        print("No record reads differently than it did when it was written.")

    comparable = [f for f in findings if f.changed is not None]
    print(
        f"\n{len(changed)} of {len(comparable)} comparable record(s) read "
        f"differently now; {len(findings) - len(comparable)} had nothing "
        f"recorded to compare against."
    )
    if changed:
        print(
            "A difference is not automatically a regression — it is the set of "
            "past output your change alters the reading of, which is the set "
            "worth looking at by hand."
        )
    return 1 if changed else 0


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

    # A newer run no longer shadows this one — `forge go` drains its queue
    # oldest first — but it does go first, and a person who just requeued this
    # run is waiting on it rather than on whatever is in front.
    ahead = [r for r in store.resumable_runs() if int(r["id"]) < run_id]
    if ahead:
        print(
            f"\nnote: run(s) {', '.join(str(r['id']) for r in ahead)} are queued "
            f"ahead of run {run_id} and `forge go` works them first."
        )

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
    candidates: list[str] = []
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

    # What the ticket may READ, which is not what it may write. The planner
    # named one file for both and the roles were then diagnosing a fault
    # through a keyhole. Writable scope is left exactly as the planner set it.
    fields["reference_files"] = evidence.reading_scope(
        config.root,
        fields["allowed_files"],
        fields["reference_files"],
        extra=candidates,
    )
    _warn_module_list(config, fields["allowed_files"])

    ticket_id = args.id or _next_bug_id(store)

    # Filed onto the open backlog when there is one, rather than into a run of
    # its own. `forge go` works a single run, so a second report filed before
    # the first was started used to shadow it: the older run kept its pending
    # ticket, `forge status` showed only the newer one, and the bug nobody
    # could see waited for the newer run to finish before it was picked up.
    # Joining is only ever offered a run that has never been worked, so
    # nothing in flight is disturbed.
    existing = store.unstarted_run()
    run_id = int(existing["id"]) if existing else 0
    position = store.next_position(run_id) if existing else 0

    ticket = Ticket(
        ticket_id=ticket_id,
        position=position,
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

    if not existing:
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

    joined = ""
    if existing:
        waiting = len(store.list_tickets(run_id))
        joined = f"  (added to run {run_id} — {waiting} ticket(s) waiting)"
    print(f"\nRun {run_id} — {ticket.ticket_id}: {ticket.title}{joined}")
    print(f"  scope     {', '.join(ticket.allowed_files) or '(none named)'}")
    for problem in _workspace_problems(config, [ticket]):
        print(f"\nwarning: {problem}")
    _warn_missing_manifests(config, [ticket])
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


def _warn_module_list(config: Config, paths: list[str]) -> list[str]:
    """Say when a ticket's writable scope is only re-export files.

    `lib.rs`, `mod.rs`, `__init__.py`, `index.js` declare modules; they hold no
    behavior to fix. A ticket scoped to one can never succeed, and it fails
    slowly — the executor cannot see the code it was told to change, so it
    blocks, and the block reads as a scoping refusal rather than as a mis-scope.
    One run spent seven retry cycles that way against four `pub mod` lines.

    A warning rather than a correction: which sibling holds the fault is the
    planner's call to make, not this function's, and the reading scope has
    already been widened to show it every one of them.
    """
    listing = []
    for path in paths:
        if any(character in path for character in "*?["):
            continue
        candidate = config.root / path
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if evidence.is_module_list(text, candidate.name):
            listing.append(path)
    if listing and len(listing) == len([p for p in paths if "*" not in p]):
        print(
            f"\nwarning: the only writable file(s) here — {', '.join(listing)} — "
            f"re-export other modules and contain no behavior to fix. The fault "
            f"is almost certainly in one of the modules they declare, which the "
            f"ticket can now read but not write. Widen the scope before running "
            f"it, or expect the executor to block asking for exactly that."
        )
    return listing


def _workspace_problems(config: Config, tickets: list) -> list[str]:
    """Tickets no build can verify, in the words a person can act on.

    Two shapes, and they need different fixes:

    A ticket whose writable files land in **no** workspace has no command of
    any kind — not a missing runner but a missing build, which `forge
    toolchain` cannot supply. Only reachable in a repository that declares
    `workspaces` and leaves a gap in them; the implicit root workspace claims
    everything, so a project that never heard of the feature never sees this.

    A ticket whose writable files land in **two** is a scoping error. Each
    build has its own commands and its own working directory, so only one of
    them can verify it, and which one is an accident of resolution order.

    Ingest is where these are free. The loop parks such a ticket when it
    reaches it, which is correct and late — by then a run exists, a human has
    walked away, and the answer was knowable before a token was spent.
    """
    problems: list[str] = []
    for ticket in tickets:
        found, unowned = config.workspaces_for(ticket.allowed_files)
        if unowned:
            roots = ", ".join(workspace.root for workspace in config.workspaces)
            problems.append(
                f"{ticket.ticket_id} writes {', '.join(unowned[:4])}, which no "
                f"workspace owns (declared roots: {roots}). No build here would "
                f"lint, type-check or test it."
            )
        if len(found) > 1:
            roots = ", ".join(sorted(workspace.root for workspace in found))
            problems.append(
                f"{ticket.ticket_id} writes into {len(found)} builds ({roots}). "
                f"Each has its own commands and working directory, so only one "
                f"of them can verify it — split it into one ticket per build."
            )
    return problems


def _warn_missing_manifests(config: Config, tickets: list) -> list[str]:
    """Say which languages this backlog writes that nothing here can build.

    At ingest, because that is the moment the fix — one more ticket, ordered
    first — costs nothing. See `toolchain.manifest_gaps` for why it warns
    rather than refuses.
    """
    gaps = toolchain.manifest_gaps(config, tickets)
    for gap in gaps:
        print(f"\nwarning: {gap}")
    if gaps:
        print("  Add the build file as a ticket of its own, before the ones that need it.")
    return gaps


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
            # Asked of the build that owns the file. Repository-wide, one
            # workspace's runner answers for another's files, which is the
            # absorption the feature exists to stop surviving inside the
            # warning meant to catch it. A file no build owns is
            # `_workspace_problems`, which refuses rather than warns.
            and (workspace := config.workspace_for(path)) is not None
            and not workspace.covers("test", suffix)
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
    workspace = _target_workspace(config, getattr(args, "workspace", ""))

    if args.skip:
        # On the record, and readable as one: `false` cannot be mistaken for a
        # command, and the gate stops asking about a language somebody has
        # already decided about.
        return _write_command(
            config, workspace, kind, language, False, suffixes=suffixes
        )

    if args.set:
        return _write_command(
            config, workspace, kind, language, args.set, suffixes=suffixes
        )

    try:
        provider = config.provider_for("planner")
    except (ConfigError, ValueError) as exc:
        sys.exit(f"error: {exc}")

    where = "this repository" if workspace.is_repo_root else workspace.root
    print(f"Reading {where} for its {language} commands...")
    # From inside the build. A subproject states its own commands in its own
    # `package.json` and its own README, and the repository root's answer for
    # them is the answer for a different project.
    detection = toolchain.detect(
        workspace.path(config.root), provider, language=language
    )
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
    return _write_command(
        config, workspace, kind, language, proposed, suffixes=suffixes
    )


def _target_workspace(config: Config, wanted: str) -> Workspace:
    """Which build `forge toolchain` is talking about.

    A repository with one build needs no answer and is never asked. With
    several, the question has to be answered explicitly: writing a command into
    the wrong build is worse than writing none, because it reports as coverage
    for files that command cannot see — which is the failure this whole feature
    exists to remove.
    """
    if wanted:
        root = normalize_workspace_root(wanted)
        for workspace in config.workspaces:
            if workspace.root == root:
                return workspace
        sys.exit(
            f"error: no workspace with root {wanted!r}. Declared: "
            + ", ".join(w.root for w in config.workspaces)
        )
    if len(config.workspaces) == 1:
        return config.workspaces[0]
    sys.exit(
        "error: this repository declares "
        f"{len(config.workspaces)} builds, so a command has to say which one "
        "it belongs to:\n  "
        + "\n  ".join(
            f"forge toolchain --workspace {w.root} --language <lang>"
            for w in config.workspaces
        )
    )


def _write_command(
    config: Config,
    workspace: Workspace,
    kind: str,
    language: str,
    command: str | bool,
    suffixes=(),
) -> int:
    """Put one language's command into one build, turning a string into a map.

    An existing single command becomes the `*` entry rather than being
    replaced: it was covering everything, and this is adding a language beside
    it, not taking its place.

    Written into the workspace rather than into the top-level `commands`. That
    used to be the only place it could go, and under a config that declares
    `workspaces` the top-level block is read by nothing — so the write
    succeeded, printed a confirmation, and changed no behaviour at all.
    """
    existing = workspace.commands.get(kind, "")
    block = {ANY_LANGUAGE: existing} if isinstance(existing, str) and existing else dict(existing or {})
    for key in suffixes or (language,):
        if key != ANY_LANGUAGE:
            block[key] = command
    workspace.commands[kind] = block

    try:
        config.validate()
    except ConfigError as exc:
        sys.exit(f"error: {exc}")

    written = config.write()
    where = "" if workspace.is_repo_root else f" in {workspace.root}"
    print(f"\nWrote {written}{where}")
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
    p.add_argument(
        "--allow-red-baseline",
        action="store_true",
        help="start even though the verify commands already fail (default: "
        "refuse, because a failure that pre-dates the run is excused for every "
        "ticket in it and the backlog would report green over a broken build)",
    )
    p.add_argument(
        "--no-quarantine",
        action="store_true",
        help="leave a failed ticket's files in the tree (default: restore them "
        "and keep a copy under .hybridforge/abandoned/, so one abandoned file "
        "cannot stop every later ticket from being verified)",
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

    p = sub.add_parser(
        "replay",
        help="re-read a past run's recorded output with the current parsers",
    )
    p.add_argument("--run", type=int, help="only this run (default: every recorded run)")
    p.add_argument("--ticket", help="only this ticket")
    p.add_argument(
        "--lens",
        choices=("all", "parse", "blame"),
        default="all",
        help="parse: model replies read as files. blame: command output read as "
        "diagnostics. (default: all)",
    )
    p.add_argument(
        "--changed",
        action="store_true",
        help="list only records that read differently than they did at the time",
    )
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_replay)

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
        "--workspace",
        default="",
        metavar="ROOT",
        help="which build the command belongs to, when the repo declares several",
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
        "models", help="write the llama.cpp preset for the configured local models"
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
