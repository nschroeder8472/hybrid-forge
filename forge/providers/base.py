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
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated: bool = False

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class Completion:
    text: str
    usage: Usage
    finish_reason: str = "stop"
    model: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

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

    def health(self) -> str:
        """One-line liveness probe used by `forge doctor` and the dashboard."""
        try:
            reply = self.complete(
                [Message(role="user", content="Reply with exactly: OK")],
                max_tokens=16,
                temperature=0.0,
                timeout=60,
            )
            return f"ok name={self.name} kind={self.kind} model={self.model} reply={reply.text.strip()[:40]!r}"
        except ProviderError as exc:
            return f"FAIL name={self.name} kind={self.kind} model={self.model} error={type(exc).__name__}: {exc}"


def split_system(messages: list[Message]) -> tuple[str, list[Message]]:
    """Pull system messages out for backends that take them as a separate field.

    Anthropic and Gemini both want the system prompt outside the turn list.
    Multiple system messages are concatenated rather than dropped.
    """
    system_parts = [m.content for m in messages if m.role == "system"]
    rest = [m for m in messages if m.role != "system"]
    return "\n\n".join(system_parts), rest
