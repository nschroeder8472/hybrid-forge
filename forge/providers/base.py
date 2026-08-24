"""Provider abstraction: one interface, any model the user brings.

Every model the pipeline talks to — local Ollama, a vLLM box, the Anthropic API,
Gemini, a bare llama.cpp binary, or the Claude Code CLI itself — is reached
through a `Provider`. Adding a new backend means adding one module here and
registering it; nothing in the loop, the budget gate, or the UI changes.

Two things every provider must report honestly, because the loop depends on
them:

1. **Capabilities** — context window and max output tokens. The budget gate uses
   these to decide whether a request fits before spending anything on it.
2. **Normalized errors** — in particular `RateLimited`, which must carry a
   reset time when the backend supplies one. That is what lets the loop park
   itself and wake up when the window reopens instead of failing the run.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["planner", "executor", "tester", "reviewer"]


# --------------------------------------------------------------------------
# Wire types
# --------------------------------------------------------------------------


@dataclass
class Message:
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass
class Usage:
    """Token accounting for a single call.

    Providers that do not report usage leave these at 0 and set `estimated`;
    the budget gate then falls back to its own estimate rather than silently
    treating the call as free.

    `prompt_tokens` is fresh (uncached) input only, matching the provider field
    of the same name. On a cached prefix that number is tiny and says nothing
    about what the call actually consumed, so the cache counters are tracked
    separately and folded into `total_tokens` — reading `prompt_tokens` alone
    undercounts a cache-heavy call by orders of magnitude.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    # Provider-reported cost for this call, when it reports one. Authoritative
    # where present: it already accounts for the model, tier, and cache rates,
    # none of which we can reconstruct from token counts alone.
    cost_usd: float = 0.0
    estimated: bool = False

    @property
    def input_tokens(self) -> int:
        """Everything the model read: fresh input plus both cache paths."""
        return self.prompt_tokens + self.cache_creation_tokens + self.cache_read_tokens

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.completion_tokens


@dataclass
class Completion:
    text: str
    usage: Usage
    finish_reason: str = "stop"
    model: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    # What a provider had to do to get an answer at all. Empty on the ordinary
    # path. Recorded and logged by the caller rather than raised, because the
    # answer arrived — but a run whose planner is being silently downgraded is
    # a run whose operator needs to know.
    recovered: str = ""

    @property
    def truncated(self) -> bool:
        """True when the model ran out of output budget mid-answer.

        Worth checking explicitly: a truncated implementation looks like a
        complete one until you try to apply it.
        """
        return self.finish_reason in ("length", "max_tokens", "MAX_TOKENS")


@dataclass
class Capabilities:
    """What this model can hold and produce.

    `context_window` is the total prompt+output budget. When a provider cannot
    introspect it, config supplies it; when neither does, the conservative
    default below applies, which errs toward splitting work rather than toward
    a mid-run overflow.
    """

    context_window: int = 8192
    max_output_tokens: int = 4096
    supports_system_role: bool = True
    supports_temperature: bool = True
    # Reserved headroom so a slightly-off token estimate does not overflow.
    safety_margin_tokens: int = 512

    def input_budget(self, requested_output: int) -> int:
        """Largest prompt that still leaves room for the requested output."""
        return self.context_window - requested_output - self.safety_margin_tokens


# --------------------------------------------------------------------------
# Normalized errors
# --------------------------------------------------------------------------


class ProviderError(Exception):
    """Base for every provider failure the loop knows how to react to."""

    retryable = False


class ProviderUnreachable(ProviderError):
    """Network-level failure. Retryable with backoff — the box may be booting."""

    retryable = True


class ProviderBadResponse(ProviderError):
    """Reached the backend, got something unparseable."""

    retryable = True


class ProviderAuthError(ProviderError):
    """Bad or missing credentials. Never retried — it will not fix itself."""

    retryable = False


class ContextOverflow(ProviderError):
    """The request does not fit in the model's context window.

    Raised before the call when the budget gate can prove it, and after the call
    when only the backend knew. Either way the loop responds by trimming
    context or splitting the ticket, never by retrying unchanged.
    """

    def __init__(self, message: str, *, needed: int = 0, available: int = 0):
        super().__init__(message)
        self.needed = needed
        self.available = available


class RateLimited(ProviderError):
    """Quota exhausted.

    `reset_at` is a unix timestamp when known. This is the signal the loop uses
    to park in WAITING_BUDGET and resume on its own, so providers should work
    hard to populate it: prefer an explicit reset header, then retry-after,
    then a conservative guess.
    """

    retryable = True

    def __init__(self, message: str, *, reset_at: float | None = None, retry_after: float | None = None):
        super().__init__(message)
        if reset_at is None and retry_after is not None:
            reset_at = time.time() + retry_after
        self.reset_at = reset_at

    @property
    def seconds_remaining(self) -> float:
        if self.reset_at is None:
            return 60.0
        return max(0.0, self.reset_at - time.time())


# --------------------------------------------------------------------------
# Provider interface
# --------------------------------------------------------------------------


class Provider(ABC):
    """A model endpoint the pipeline can send a chat completion to.

    Implementations must be stateless and safe to call from the daemon thread.
    Any per-call retry belongs to the loop, not here — the loop is the thing
    that knows whether a retry is worth its budget.
    """

    kind: str = "abstract"

    def __init__(self, name: str, config: dict[str, Any]):
        self.name = name
        self.config = config
        self.model = config.get("model", "")

    @abstractmethod
    def complete(
        self,
        messages: list[Message],
        *,
        max_tokens: int,
        temperature: float = 0.2,
        timeout: int = 600,
    ) -> Completion:
        """Send a completion request. Raises a ProviderError subclass on failure."""

    def temperature(self, requested: float) -> float:
        """The sampling temperature to actually send.

        The loop asks for a temperature per role — low, because determinism is
        usually what a pipeline wants. Model families disagree with that: some
        reasoning models degenerate into repetition well above zero, and ship
        an official sampling recipe you are meant to follow rather than
        override. Config wins, so `"temperature": 0.6` on a model block lets a
        model be run the way its authors intended without the loop having to
        know which family it belongs to.
        """
        configured = self.config.get("temperature")
        return requested if configured is None else float(configured)

    @abstractmethod
    def capabilities(self) -> Capabilities:
        """Report context window and output limits for the configured model."""

    def count_tokens(self, messages: list[Message]) -> int:
        """Token count for a message list.

        The default is a deliberate over-estimate. Providers with a real
        tokenizer or a counting endpoint should override — the budget gate is
        only as good as this number.
        """
        from ..tokens import estimate_messages

        return estimate_messages(messages)

    # Enough for a reasoning model to think before it answers. The probe used
    # to ask for 16, which a reasoning model spends entirely on its preamble:
    # it returned an empty string with finish_reason "length" and the endpoint
    # was reported `ok ... reply=''` — a pass recorded for a model that had not
    # said anything. gpt-oss:20b needs 51 tokens to reply "OK".
    _HEALTH_OUTPUT_TOKENS = 512

    def health(self) -> str:
        """One-line liveness probe used by `forge doctor` and the dashboard."""
        try:
            reply = self.complete(
                [Message(role="user", content="Reply with exactly: OK")],
                max_tokens=min(self._HEALTH_OUTPUT_TOKENS, self.capabilities().max_output_tokens),
                temperature=0.0,
                timeout=60,
            )
        except ProviderError as exc:
            return f"FAIL name={self.name} kind={self.kind} model={self.model} error={type(exc).__name__}: {exc}"

        text = reply.text.strip()
        if not text:
            # Answering with nothing is not answering. Reported as a failure so
            # it is fixed before a run rather than diagnosed as a bad executor.
            why = (
                "hit its output limit before emitting anything"
                if reply.truncated
                else "returned an empty response"
            )
            return f"FAIL name={self.name} kind={self.kind} model={self.model} error={why}"
        return f"ok name={self.name} kind={self.kind} model={self.model} reply={text[:40]!r}"

    def diagnostics(self) -> list[str]:
        """Configuration problems that will not fail a probe but will cost a run.

        `health()` answers "does this endpoint reply". These are the things that
        answer yes and are still wrong: a context window larger than the server
        is serving, an output reserve that leaves nowhere to put the prompt, a
        model half-resident in VRAM. None of them raise, none of them show up
        until a ticket behaves strangely at 2am, and all of them are visible for
        the price of a request `forge doctor` is already making.

        Best-effort and provider-specific; the default is that there is nothing
        to say.
        """
        return []


def split_system(messages: list[Message]) -> tuple[str, list[Message]]:
    """Pull system messages out for backends that take them as a separate field.

    Anthropic and Gemini both want the system prompt outside the turn list.
    Multiple system messages are concatenated rather than dropped.
    """
    system_parts = [m.content for m in messages if m.role == "system"]
    rest = [m for m in messages if m.role != "system"]
    return "\n\n".join(system_parts), rest
