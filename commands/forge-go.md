---
description: Start the autonomous loop and let it run until the backlog is done or you stop it
argument-hint: [optional path to a spec or plan]
---

Start the run loop for this repository.

The loop is a daemon, not a conversation. Once it starts, **you are not driving
it** — it owns the state machine, calls the models, runs the checks, and decides
what happens next from SQLite. Your job here is to get it started correctly and
then get out of the way.

1. If `$ARGUMENTS` names a file, run `forge ingest "$ARGUMENTS"` first and show
   the resulting backlog. Otherwise check `forge status` for an existing run.
2. **Show the user the ticket list and the route on each one before starting.**
   This is the last cheap moment to catch a ticket routed `delegate` that
   should have been `claude-only`. Wait for them to say go.
3. Run `forge go`. Report the dashboard URL it prints.
4. Do not poll the loop or narrate its progress. It writes its own log, and the
   dashboard shows it live. Tell the user to watch there.

If the user wants it running unattended past this session, tell them to start it
detached themselves (`nohup forge go --no-ui &`, or a terminal that outlives the
session) — a loop started inside a Claude Code session dies with the session.

Stopping: `forge pause`, `forge resume`, `forge stop`, or the dashboard buttons.
All of them take effect after the current step, never mid-patch.
