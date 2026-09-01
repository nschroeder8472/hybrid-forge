"""Why a ticket was withheld from the executor, as a value rather than prose.

`claude-only` names the party that decided. What a reader six weeks later needs
— and what a dashboard has to render — is the objection. `withheld:security`
carries both: it still fails every `route != "delegate"` gate in the codebase
unchanged, and it says why.

The colon form rather than a second column, for that reason. Every gate is
already written against the whole value, so a reason that travels inside it
costs no call site; a `route_reason` column would have to be joined at each one,
and a row where it was empty would read as delegable.

Nothing stored is rewritten. `claude-only` gates correctly as it stands, and
minting a reason nobody recorded would be inventing evidence — it reads as
`withheld:unspecified` at the display layer and nowhere else.
"""

from __future__ import annotations

DELEGATE = "delegate"
WITHHELD = "withheld"

# What the route said before it said why. Still accepted, still gates, never
# written by anything new.
LEGACY_WITHHELD = "claude-only"

# Closed, and drawn from the categories the delegation-protocol skill already
# lists — this is the existing prose made countable rather than a new taxonomy.
REASONS: dict[str, str] = {
    "security": "authentication, authorization, session handling, secrets",
    "concurrency": "locking, async ordering, shared mutable state",
    "interface": "public API surface, published interfaces, database migrations",
    "compliance": "cryptography, payment flows, anything with a compliance dimension",
    "performance": "a path where the fix depends on profiling judgment",
    "unresolved": "what this should do is still genuinely open",
    # The one the harness can prove: a glob matched, which is a mechanical fact
    # in the way an error code is.
    "never-delegate": "a neverDelegate glob matched",
    # The reason was not stated. Accepted and reported, never minted — a route
    # that cannot parse its reason is still a route that withholds the ticket,
    # and failing closed is the whole point of the gate.
    "unspecified": "no reason was recorded",
}

UNSPECIFIED = "unspecified"


def is_withheld(route: str) -> bool:
    """Whether this route keeps the ticket away from the executor.

    Written against the value rather than against a list, so a reason this
    module has never heard of still withholds. The gate failing open on an
    unknown value is the one outcome worth designing against.
    """
    route = (route or "").strip().lower()
    return bool(route) and route != DELEGATE


def reason_of(route: str) -> str:
    """The reason a route names, or `unspecified` when it names none.

    `claude-only` and a bare `withheld` both answer `unspecified`: neither
    recorded one, and saying so is more useful than either guessing or
    returning empty and making every caller handle it.
    """
    route = (route or "").strip().lower()
    if not is_withheld(route):
        return ""
    _, _, reason = route.partition(":")
    reason = reason.strip()
    return reason if reason in REASONS else UNSPECIFIED


def describe(route: str) -> str:
    """A route as a person reads it: `withheld: security`, or `delegate`."""
    if not is_withheld(route):
        return DELEGATE
    return f"{WITHHELD}: {reason_of(route)}"


def normalise(route: str) -> tuple[str, str]:
    """A route as it should be stored, and a warning about it, or "".

    Accepts what a plan may write and settles it into one spelling. The warning
    is returned rather than logged so the caller — ingest, which knows the
    ticket id — can name the ticket in it.
    """
    raw = (route or "").strip()
    if not raw:
        return DELEGATE, ""
    lowered = raw.lower()
    if lowered == DELEGATE:
        return DELEGATE, ""
    if lowered == LEGACY_WITHHELD:
        return f"{WITHHELD}:{UNSPECIFIED}", (
            f"route {LEGACY_WITHHELD!r} is the old spelling and records no "
            f"reason; read as withheld:{UNSPECIFIED}. Write "
            f"`withheld:<reason>` instead — one of "
            f"{', '.join(sorted(REASONS))}."
        )

    head, _, reason = lowered.partition(":")
    if head != WITHHELD:
        # Unknown, and therefore withholding. A route nobody can parse must not
        # become a delegable one.
        return f"{WITHHELD}:{UNSPECIFIED}", (
            f"route {raw!r} is not `delegate` or `withheld:<reason>`; the "
            f"ticket is withheld and its reason read as {UNSPECIFIED}."
        )
    reason = reason.strip()
    if not reason:
        return f"{WITHHELD}:{UNSPECIFIED}", ""
    if reason not in REASONS:
        return f"{WITHHELD}:{UNSPECIFIED}", (
            f"withheld reason {reason!r} is not one of "
            f"{', '.join(sorted(REASONS))}; read as {UNSPECIFIED}."
        )
    return f"{WITHHELD}:{reason}", ""
