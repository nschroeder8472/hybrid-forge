"""Whether a per-ticket write may be served, and what to say when it may not.

The dashboard's read endpoints answer anyone who can reach the port, and
`POST /api/control` hands them four enumerated commands. A per-ticket write is
a different order of exposure: a note and a criterion land in an executor
prompt, and a release puts a ticket a person withheld back in front of a model
that writes files. `exposure_warning` in `server.py` is the right posture for
the first two surfaces and not for this one — a warning printed to stderr at
startup is read by whoever started the daemon, which is not who reaches the
port afterwards.

So this refuses rather than warns, and the operator turns it on by name:
`ui.allowRemoteWrites`. Off, a write is served to a loopback dashboard from a
loopback client and to nothing else. On, it is served wherever the dashboard is
bound and to whoever reaches it, which is a decision a person makes once, in a
file, rather than one that follows from a bind address they set for another
reason.

Both halves are checked — where the server is bound *and* where the request
came from — because the bind address alone stops describing the caller the
moment anything is put in front of the port. A reverse proxy on a host with a
loopback-bound dashboard behind it forwards requests that arrive from
127.0.0.1, and a gate reading only `ui.host` would serve every one of them.
"""

from __future__ import annotations

from urllib.parse import unquote

from ..config import Config
from ..routes import DELEGATE, describe, is_withheld
from ..state import TICKET_WITHHELD, Store
from . import server

# What a dual-stack socket hands back for a loopback IPv4 client.
# `server.LOOPBACK_HOSTS` is the vocabulary a person writes in config; this is
# the one `accept()` produces, and a write from it is a write from this machine.
MAPPED_LOOPBACK = "::ffff:127.0.0.1"

# A note or criterion a person writes from the dashboard. Longer than this is
# not advice, it is a document, and the executor prompt is not where documents
# live.
MAX_WRITE_TEXT = 2000


def write_refusal(config: Config, peer: str) -> str:
    """Empty when this write may be served, or why it was not.

    The empty string is the permission, which reads backwards for a moment and
    is deliberate: the caller sends what comes back as the body of a 403, so
    the refusal and its explanation are the same value and cannot come apart.
    A gate returning a bool leaves the message to the caller, and the caller is
    where a message goes missing.

    `server.is_exposed` and `server.LOOPBACK_HOSTS` are read through the module
    rather than imported by name, so the two lists cannot drift and neither can
    the import order: `server` imports this module to serve the endpoints, and
    a `from .server import ...` here fails whenever `server` is the one loaded
    first.
    """
    if config.ui.allow_remote_writes:
        return ""

    host = config.ui.host
    address = (peer or "").strip()
    bound_to_loopback = not server.is_exposed(host)
    came_from_loopback = address in server.LOOPBACK_HOSTS or address == MAPPED_LOOPBACK
    if bound_to_loopback and came_from_loopback:
        return ""

    return (
        f"the dashboard is bound to {host or '(unset)'} and this write arrived "
        f"from {address or '(no peer address)'}; per-ticket writes are served to "
        f"loopback only. A note and a criterion are read by the executor and a "
        f"release hands it a ticket a person withheld, and this server "
        f"authenticates nobody. Set ui.allowRemoteWrites to true to serve them "
        f"anyway, behind a proxy that does."
    )


def apply_write(
    store: Store,
    config: Config,
    path: str,
    payload: dict,
    peer: str,
) -> tuple[int, dict]:
    """Turn one decoded dashboard request into one of the three writes.

    The three writes are a note (`Store.advise`), a criterion
    (`Store.promote_criteria`) and a release — the same sequence `cmd_release`
    in `forge/cli.py` performs, in the same order, so a ticket released from
    the dashboard and one released from the terminal leave the same record.

    The gate runs before the path is parsed, so a refused caller learns
    nothing about which tickets exist: a 403 that named a ticket would be an
    inventory of the backlog for anyone who can reach the port.

    Returns the HTTP status and the body to send as JSON. No socket, no
    request object, no subprocess — the handler is the only thing that knows
    a request existed.
    """
    refusal = write_refusal(config, peer)
    if refusal:
        return 403, {"error": refusal}

    segments = [segment for segment in path.split("/") if segment]
    if len(segments) != 4 or segments[0] != "api" or segments[1] != "ticket":
        return 404, {"error": "not found"}
    ticket_id = unquote(segments[2])
    action = segments[3]
    if action not in ("note", "criterion", "route"):
        return 404, {"error": "not found"}

    run = server._live_run(store) or store.latest_run()
    if run is None:
        return 409, {"error": "no runs yet"}
    run_id = int(run["id"])

    if "run" in payload:
        requested = payload["run"]
        if not isinstance(requested, int) or isinstance(requested, bool) or requested != run_id:
            return 409, {"error": f"this write is for run {run_id}, not run {requested}"}

    ticket = next((t for t in store.list_tickets(run_id) if t.ticket_id == ticket_id), None)
    if ticket is None:
        return 404, {"error": f"run {run_id} has no ticket {ticket_id}"}

    text = str(payload.get("text", "")).strip()
    if not text:
        missing = {
            "note": "a note needs text",
            "criterion": "a criterion needs text",
            "route": "releasing a withheld ticket needs a reason",
        }[action]
        return 400, {"error": missing}
    if len(text) > MAX_WRITE_TEXT:
        return 400, {"error": f"the text is {len(text)} characters; the limit is {MAX_WRITE_TEXT}"}

    if action == "note":
        store.advise(run_id, ticket, text)
        store.log(
            run_id,
            f"{ticket.ticket_id}: a person advised — {text[:200]}",
            kind="ticket",
            data={
                "ticket": ticket.ticket_id,
                "author": "human",
                "via": "dashboard",
                "peer": peer,
            },
        )
        return 200, {"ok": True, "notes": len(ticket.human_note)}

    if action == "criterion":
        _, adopted = store.promote_criteria(run_id, ticket.ticket_id, [text])
        store.log(
            run_id,
            f"{ticket.ticket_id}: a person added a criterion — {text[:200]}",
            kind="ticket",
            data={
                "ticket": ticket.ticket_id,
                "author": "human",
                "via": "dashboard",
                "peer": peer,
            },
        )
        return 200, {"ok": True, "adopted": adopted}

    if payload.get("route") != DELEGATE:
        message = f"this endpoint releases a ticket to the executor; route must be {DELEGATE!r}"
        return 400, {"error": message}
    if not is_withheld(ticket.route):
        return 200, {"ok": True, "route": ticket.route, "released": False}

    was = ticket.route
    store.advise(run_id, ticket, f"Released from {describe(was)}: {text}")
    store.set_route(run_id, ticket, DELEGATE)
    if ticket.status == TICKET_WITHHELD:
        store.reset_tickets(run_id, ticket_ids=[ticket.ticket_id])
    store.log(
        run_id,
        f"{ticket.ticket_id}: released from {describe(was)} by a person — {text[:200]}",
        level="warn",
        kind="ticket",
        data={
            "ticket": ticket.ticket_id,
            "was": was,
            "author": "human",
            "via": "dashboard",
            "peer": peer,
        },
    )
    return 200, {"ok": True, "route": DELEGATE, "released": True}
