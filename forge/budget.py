"""Context-window accounting and the rate-limit gate.

Two jobs, both about making the loop survive limits rather than die at them:

**Context.** Before a call, prove the prompt fits in the model's window with
room for the requested output. If it does not, trim the droppable context and
try again; if it still does not, fail loudly with a message that says the
ticket needs splitting rather than letting the backend truncate silently.

**Rate limits.** Two halves. Reactively, a `RateLimited` from any provider
parks the run until the reported reset time. Proactively, a configured
allowance is tracked in the usage ledger so the loop waits *before* crossing a
limit rather than discovering it by being cut off — which matters most for
subscription plans, where the reported reset can be an hour away.

The waiting itself is the point. A daemon that sleeps through a closed window
and resumes when it reopens is the difference between "the run finished
overnight" and "the run died at 2am and nobody noticed until morning."
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .providers import ContextOverflow, Message, Provider, Usage
from .tokens import format_tokens


class UsageLedger(Protocol):
    """The slice of the state store the gate needs.

    Kept narrow so the gate is testable without a database and so the store can
    change shape without touching this file.
    """

    def record_usage(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cache_creation_tokens: int = 0,
        cache_read_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None: ...

    def tokens_since(self, model: str, since: float) -> int: ...

    def cost_since(self, model: str, since: float) -> float: ...

    def requests_since(self, model: str, since: float) -> int: ...


@dataclass
class RateLimitPolicy:
    """A model's declared allowance.

    Every field is optional — an unset limit is simply not enforced. For
    subscription-backed providers where the real numbers are opaque, leave
    these unset and rely on the reactive path: the provider reports the limit
    when it hits it, and the gate parks the run until the reported reset.
    """

    requests_per_minute: int = 0
    tokens_per_minute: int = 0
    tokens_per_window: int = 0
    window_seconds: int = 0
    # A spend cap is denominated in dollars, not tokens, and providers that
    # price their own calls report that figure directly. Capping on it is the
    # only proactive defence against a billing limit — the reactive path only
    # learns about one after the provider has already refused a call.
    cost_per_window: float = 0.0

    @classmethod
    def from_config(cls, config: dict[str, Any] | None) -> "RateLimitPolicy":
        config = config or {}
        return cls(
            requests_per_minute=int(config.get("requestsPerMinute", 0) or 0),
            tokens_per_minute=int(config.get("tokensPerMinute", 0) or 0),
            tokens_per_window=int(config.get("tokensPerWindow", 0) or 0),
            window_seconds=int(config.get("windowSeconds", 0) or 0),
            cost_per_window=float(config.get("costPerWindow", 0) or 0),
        )

    @property
    def is_empty(self) -> bool:
        return not (
            self.requests_per_minute
            or self.tokens_per_minute
            or (self.tokens_per_window and self.window_seconds)
            or (self.cost_per_window and self.window_seconds)
        )


@dataclass
class Wait:
    """A decision to pause, with enough detail for the dashboard to explain it."""

    seconds: float
    reason: str
    until: float = field(init=False)

    def __post_init__(self) -> None:
        self.until = time.time() + self.seconds


class BudgetGate:
    """Decides whether a call can proceed now, later, or not at all."""

    def __init__(self, ledger: UsageLedger, policies: dict[str, RateLimitPolicy] | None = None):
        self.ledger = ledger
        self.policies = policies or {}
        # Reset times learned reactively from provider RateLimited errors,
        # keyed by model name. Outlives any single call, so a limit hit during
        # BUILD still gates the REVIEW step that follows.
        self._parked_until: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Rate limits
    # ------------------------------------------------------------------

    def park(self, model: str, until: float) -> None:
        """Record that a model is unavailable until a given time."""
        self._parked_until[model] = max(self._parked_until.get(model, 0.0), until)

    def check_rate_limit(self, model: str) -> Wait | None:
        """Return a Wait if this model cannot be called yet, else None."""
        now = time.time()

        parked = self._parked_until.get(model)
        if parked and parked > now:
            return Wait(
                seconds=parked - now,
                reason=f"{model} rate limited; window reopens at {_clock(parked)}",
            )

        policy = self.policies.get(model)
        if policy is None or policy.is_empty:
            return None

        if policy.requests_per_minute:
            used = self.ledger.requests_since(model, now - 60)
            if used >= policy.requests_per_minute:
                return Wait(
                    seconds=60,
                    reason=(
                        f"{model} at {used}/{policy.requests_per_minute} requests per minute"
                    ),
                )

        if policy.tokens_per_minute:
            used = self.ledger.tokens_since(model, now - 60)
            if used >= policy.tokens_per_minute:
                return Wait(
                    seconds=60,
                    reason=(
                        f"{model} at {format_tokens(used)}/"
                        f"{format_tokens(policy.tokens_per_minute)} tokens per minute"
                    ),
                )

        if policy.tokens_per_window and policy.window_seconds:
            used = self.ledger.tokens_since(model, now - policy.window_seconds)
            if used >= policy.tokens_per_window:
                return Wait(
                    seconds=policy.window_seconds / 4,
                    reason=(
                        f"{model} at {format_tokens(used)}/"
                        f"{format_tokens(policy.tokens_per_window)} tokens for the "
                        f"{policy.window_seconds // 3600}h window"
                    ),
                )

        if policy.cost_per_window and policy.window_seconds:
            spent = self.ledger.cost_since(model, now - policy.window_seconds)
            if spent >= policy.cost_per_window:
                return Wait(
                    seconds=policy.window_seconds / 4,
                    reason=(
                        f"{model} at ${spent:.2f}/${policy.cost_per_window:.2f} "
                        f"for the {policy.window_seconds // 3600}h window"
                    ),
                )

        return None

    def record(self, model: str, usage: Usage) -> None:
        self.ledger.record_usage(
            model,
            usage.prompt_tokens,
            usage.completion_tokens,
            usage.cache_creation_tokens,
            usage.cache_read_tokens,
            usage.cost_usd,
        )
        # A successful call means the window is open again.
        self._parked_until.pop(model, None)

    # ------------------------------------------------------------------
    # Context window
    # ------------------------------------------------------------------

    def fit(
        self,
        provider: Provider,
        messages: list[Message],
        *,
        max_output: int,
        droppable: Callable[[Message], bool] | None = None,
    ) -> list[Message]:
        """Return a message list that fits, trimming droppable context if needed.

        `droppable` marks messages the caller is willing to lose — retrieved
        project memory, prior attempt output. The spec and acceptance criteria
        are never droppable: a ticket that only fits after discarding its own
        requirements has not been made to fit, it has been made meaningless.
        """
        caps = provider.capabilities()
        budget = caps.input_budget(max_output)
        needed = provider.count_tokens(messages)

        if needed <= budget:
            return messages

        # A budget at or below zero is not a tight fit, it is an arithmetic
        # impossibility: the output reserve alone exceeds the window. No ticket
        # of any size fits, so blaming the one in hand sends the reader off to
        # split tickets that were never the problem. One run reported six
        # tickets of 1-3k tokens as "too large for this model" while the real
        # cause was a context window that had collapsed to a default because
        # the model was missing from the server entirely.
        if budget <= 0:
            raise ContextOverflow(
                f"{provider.name} has no room for a prompt of any size: a "
                f"{format_tokens(caps.context_window)} context window with "
                f"{format_tokens(max_output)} reserved for output leaves "
                f"{budget:,} tokens. This is a configuration or discovery "
                f"failure, not a ticket that is too large — lower "
                f"maxOutputTokens, raise contextWindow, or run `forge doctor` "
                f"to see whether the model is answering at all.",
                needed=needed,
                available=budget,
            )

        if droppable is None:
            raise ContextOverflow(
                f"prompt needs {format_tokens(needed)} tokens but {provider.name} allows "
                f"{format_tokens(budget)} with {format_tokens(max_output)} reserved for output. "
                "Split the ticket or configure a model with a larger context window.",
                needed=needed,
                available=budget,
            )

        # Drop optional context oldest-first — the most recently retrieved
        # context is the most likely to be about the work in hand.
        kept = list(messages)
        for message in messages:
            if provider.count_tokens(kept) <= budget:
                break
            if droppable(message):
                kept.remove(message)

        remaining = provider.count_tokens(kept)
        if remaining > budget:
            raise ContextOverflow(
                f"prompt still needs {format_tokens(remaining)} tokens after dropping optional "
                f"context, but {provider.name} allows {format_tokens(budget)}. "
                "This ticket is too large for this model — split it.",
                needed=remaining,
                available=budget,
            )
        return kept

    def headroom(self, provider: Provider, messages: list[Message], max_output: int) -> float:
        """Fraction of the context window this prompt would consume (0.0-1.0+).

        The dashboard renders this as a bar; a ticket that routinely sits above
        0.8 is a ticket that should have been split.
        """
        caps = provider.capabilities()
        return provider.count_tokens(messages) / max(1, caps.context_window - max_output)


def _clock(timestamp: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(timestamp))
