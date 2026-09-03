"""Provider abstraction: one interface, any model the user brings.

Every model the pipeline talks to — a local checkpoint on llama.cpp's router,
the Anthropic API, Gemini, OpenAI, or the Claude Code CLI itself — is reached
through a `Provider`. Local means llama.cpp and only llama.cpp, so what a
diagnostic can say about a local model is specific rather than the intersection
of four backends; cloud stays plural. Adding one is a module here and a line in
the registry; nothing in the loop, the budget gate, or the UI changes.

Two things every provider must report honestly, because the loop depends on
them:

1. **Capabilities** — context window and max output tokens. The budget gate uses
   these to decide whether a request fits before spending anything on it.
2. **Normalized errors** — in particular `RateLimited`, which must carry a
   reset time when the backend supplies one. That is what lets the loop park
   itself and wake up when the window reopens instead of failing the run.
"""

from __future__ import annotations

import hashlib
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["planner", "executor", "tester", "reviewer"]


# --------------------------------------------------------------------------
# Wire types
# --------------------------------------------------------------------------


@dataclass
class TextPart:
    """Ordinary prompt text, as one part of a message."""

    text: str


@dataclass
class ImagePart:
    """An image inlined into a prompt.

    Carried as bytes and base64-encoded at the wire edge, per provider, so the
    loop never holds a provider's encoding of it.

    `width` and `height` are metadata supplied by whoever built the part, not
    something read out of the pixels. The daemon does not decode images — it
    has no image library and an image is arbitrary bytes from a model — but
    the budget gate still has to price the message, and every provider that
    bills for images bills by area. A part with no dimensions is priced at the
    worst case rather than at zero: an under-priced prompt overflows a context
    window after it has been paid for.
    """

    media_type: str
    data: bytes
    width: int = 0
    height: int = 0
    # Where the bytes live on disk, when they came from a file. Recorded in
    # artifacts instead of the bytes themselves.
    path: str = ""

    @property
    def digest(self) -> str:
        """A stable identity for these bytes.

        What a record keeps instead of the image, and what a prompt
        fingerprint hashes: two prompts differing only in the image they carry
        are two different prompts.
        """
        return hashlib.sha256(self.data).hexdigest()

    def summary(self) -> dict[str, Any]:
        """The part as something a JSON record can hold."""
        return {
            "media_type": self.media_type,
            "bytes": len(self.data),
            "digest": self.digest[:16],
            "width": self.width,
            "height": self.height,
            "path": self.path,
        }


Part = TextPart | ImagePart


@dataclass
class Message:
    """One turn of a prompt.

    `content` stays a `str` and means exactly what it used to. Every prompt in
    the loop builds one that way and none of them changed: a string is read as
    a single `TextPart`, and the parts list exists for the one thing a string
    cannot carry. A reviewer that cannot see the image is not a reviewer —
    which is true of a screenshot attached to an ordinary code ticket, with no
    image generation anywhere in the picture.
    """

    role: Literal["system", "user", "assistant"]
    content: str | list[Part]

    @property
    def parts(self) -> list[Part]:
        """The content as parts, whichever way it was given."""
        if isinstance(self.content, str):
            return [TextPart(self.content)]
        return list(self.content)

    @property
    def text(self) -> str:
        """Every text part, joined.

        What a caller that reasons about prompt *prose* wants — the droppable
        check, the system-message split, a log line. An image contributes
        nothing to it and must not contribute a placeholder either: a heading
        check reading `[image]` is a check reading something no prompt builder
        wrote.
        """
        if isinstance(self.content, str):
            return self.content
        return "".join(
            part.text for part in self.content if isinstance(part, TextPart)
        )

    @property
    def images(self) -> list[ImagePart]:
        if isinstance(self.content, str):
            return []
        return [part for part in self.content if isinstance(part, ImagePart)]


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
    # Whether this model can be shown an image. Off by default, and declared
    # rather than assumed: on llamacpp it is a property of the checkpoint
    # rather than of the adapter — a GGUF with a projector beside it can see
    # and one without cannot, and forge turns the projector off by default
    # because it costs VRAM no text-only role uses. A blind model handed an
    # image is not a slower reviewer, it is a reviewer ruling on a filename.
    supports_images: bool = False
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


class ProviderCannotSee(ProviderError):
    """A model without vision was handed an image.

    Never retried: the checkpoint does not grow a projector between attempts.
    Raised rather than silently dropping the image, because a prompt that says
    "does this match the criteria" with the image removed is a question the
    model will answer anyway.
    """

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

    def __init__(
        self,
        message: str,
        *,
        reset_at: float | None = None,
        retry_after: float | None = None,
    ):
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


# What a call's timeout has to cover.
#
# Generating `maxOutputTokens` takes as long as it takes, and a timeout shorter
# than that makes the budget a number the model can never reach. The failure is
# worse than the waste: the socket dies mid-generation, and what is reported is
# `timed out ... reaching <url>` — the endpoint, which is the one thing that was
# not wrong. On one run a reviewer configured for 65,536 tokens against a
# hardcoded 600s died that way three times in a row while the model behind it
# was answering normally, and the real cause (a model reasoning past its budget)
# was never diagnosed because the handler for it is downstream of the response.
#
# 30 tok/s is a floor, not an estimate of any particular box. A 26B MoE on an
# RTX 5090 measured 113.8 tok/s; a dense model on consumer hardware runs a
# fraction of that. Deriving from the floor is generous where the hardware is
# fast and still correct where it is slow. `tokensPerSecond` sets the real
# figure for a run that wants a tighter guard.
DEFAULT_TOKENS_PER_SECOND = 30.0
# Prefill, queueing behind another request, and loading a checkpoint that was
# not resident. Flat, because none of it scales with the output budget.
TIMEOUT_OVERHEAD_SECONDS = 120
# Never shorter than the timeout that used to be hardcoded here, so deriving it
# cannot make any existing configuration less patient than it already was.
MIN_TIMEOUT_SECONDS = 600
# Passed as `timeout` to mean "derive one from the budget". A real timeout is
# always truthy, so an explicit caller — `health()` asks for 60 — still wins.
DERIVE_TIMEOUT = 0


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
        # An explicit ceiling in seconds. Wins over the derivation below, for
        # the run that would rather cut a call off than wait for it.
        self.timeout_seconds = int(config.get("timeoutSeconds", 0) or 0)
        # What this endpoint actually generates at. Set it and the derived
        # timeout tracks `maxOutputTokens` on its own, which is the point: an
        # absolute `timeoutSeconds` has to be re-tuned by hand every time the
        # budget moves, and forgetting to is exactly how the budget stops
        # being reachable.
        self.tokens_per_second = float(config.get("tokensPerSecond", 0) or 0)

    @abstractmethod
    def complete(
        self,
        messages: list[Message],
        *,
        max_tokens: int,
        temperature: float = 0.2,
        timeout: int = DERIVE_TIMEOUT,
    ) -> Completion:
        """Send a completion request. Raises a ProviderError subclass on failure.

        `timeout` defaults to whatever `request_timeout` derives from
        `max_tokens`; pass one only to be less patient than that.
        """

    def request_timeout(self, max_tokens: int) -> int:
        """Seconds to allow a call that may generate `max_tokens`.

        The loop never passes a timeout, and it should not have to: the only
        thing that decides how long a call can legitimately take is the budget
        it was given, and the provider is the one holding both.
        """
        if self.timeout_seconds:
            return self.timeout_seconds
        rate = self.tokens_per_second or DEFAULT_TOKENS_PER_SECOND
        return max(
            MIN_TIMEOUT_SECONDS,
            int(TIMEOUT_OVERHEAD_SECONDS + max_tokens / rate),
        )

    def timeout_notes(self) -> list[str]:
        """Whether a configured `timeoutSeconds` can cover the output budget.

        Only reachable when someone set one: a derived timeout covers the
        budget by construction. Reported rather than corrected, because a
        deliberate ceiling is a legitimate thing to want — what is not
        legitimate is finding out about it at 2am, wearing the name of a
        network fault.
        """
        if not self.timeout_seconds:
            return []
        budget = self.capabilities().max_output_tokens
        rate = self.tokens_per_second or DEFAULT_TOKENS_PER_SECOND
        needed = int(TIMEOUT_OVERHEAD_SECONDS + budget / rate)
        if self.timeout_seconds >= needed:
            return []
        reachable = int((self.timeout_seconds - TIMEOUT_OVERHEAD_SECONDS) * rate)
        return [
            f"timeoutSeconds is {self.timeout_seconds:,} but generating "
            f"maxOutputTokens ({budget:,}) needs about {needed:,}s at "
            f"{rate:g} tok/s. A call that uses more than roughly "
            f"{max(reachable, 0):,} tokens will be cut off mid-generation and "
            f"reported as the endpoint timing out. Raise timeoutSeconds to "
            f"{needed:,}, lower maxOutputTokens, or set tokensPerSecond if "
            f"{rate:g} tok/s is wrong for this endpoint."
        ]

    def temperature(self, requested: float) -> float:
        """The sampling temperature to actually send.

        The loop asks for a temperature per role — low, because determinism is
        usually what a pipeline wants. Model families disagree with that: some
        reasoning models degenerate into repetition well above zero, and ship
        an official sampling recipe you are meant to follow rather than
        override. Config wins, so `"temperature": 0.6` on a model block lets a
        model be run the way its authors intended without the loop having to
        know which family it belongs to.

        Winning everywhere is the problem. The loop does not ask for one
        temperature: it asks 0.0 where it needs the same answer twice — the
        sign-off votes, the verdict parse, the respec — and 0.1 or 0.2 where it
        wants the model to reach. A scalar overrides both, so following a
        vendor's recipe silently costs reproducible ratification, and the same
        backlog run twice parks different tickets. Measured: two runs of one
        nine-ticket spec under identical configuration, and two tickets swapped
        verdicts.

        So the requested value is read as the intent it is. A map says what to
        do with each:

            "temperature": 0.6                             # as before
            "temperature": {"default": 0.6, "deterministic": 0.0}

        `deterministic` is used when the loop asked for exactly zero, `default`
        for everything else. Either key may be omitted, and an omitted one
        leaves the loop's own number alone — which makes `{"default": 0.6}` the
        honest spelling of "follow the recipe, but let determinism through".
        """
        configured = self.config.get("temperature")
        if configured is None:
            return requested
        if isinstance(configured, dict):
            key = "deterministic" if requested == 0.0 else "default"
            chosen = configured.get(key)
            return requested if chosen is None else float(chosen)
        return float(configured)

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

    def _require_vision(self, messages: list[Message]) -> None:
        """Refuse a prompt carrying an image this model cannot see.

        Every adapter calls this before it formats a request. Checked here
        rather than at `config.validate()` alone because a role's capability is
        a property of the model it is pointed at, and a prompt only sometimes
        carries an image.
        """
        if not any(message.images for message in messages):
            return
        if self.capabilities().supports_images:
            return
        raise ProviderCannotSee(
            f"{self.name} ({self.kind}, model={self.model}) was given a prompt "
            f"carrying an image and cannot see one. Point this role at a "
            f"multimodal model, or do not send it images."
        )

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
            return (
                f"FAIL name={self.name} kind={self.kind} model={self.model} "
                f"error={type(exc).__name__}: {exc}"
            )

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

        Best-effort and provider-specific; the default is the one check that
        is not provider-specific at all — whether a timeout set by hand leaves
        room to generate the budget set by hand.
        """
        return self.timeout_notes()


_REASONING_CLOSE = re.compile(r"</(?:think|thinking|reasoning)\s*>", re.IGNORECASE)
_REASONING_OPEN = re.compile(r"<(?:think|thinking|reasoning)\s*>", re.IGNORECASE)


def strip_reasoning(text: str) -> str:
    """The answer out of a reply that carries its chain of thought inline.

    A thinking model is supposed to return its reasoning in a sibling field, and
    most servers do. llama.cpp does not always: depending on the chat template
    and how the server was started, the whole `<think>…</think>` block arrives
    in `content` instead, and every parser downstream then reads the model's
    deliberation as its answer.

    That is not a cosmetic problem. One sign-off pass rehearsed the required
    reply format inside its own reasoning, and the ratification parser — which
    scans for a `BLOCKING:` heading and takes what follows — collected the
    prompt's own placeholder lines as blocking objections. The ticket was parked
    partly on the strings `...` and `(one line each, or NONE)`.

    Everything up to and including the last closing tag goes. An opening tag
    with no closing one means the model never finished thinking and there is no
    answer to find; returning the deliberation would hand a parser prose that
    argues with itself, so the empty string is the honest result and callers
    already treat it as unreadable output.
    """
    if not text:
        return text
    closes = list(_REASONING_CLOSE.finditer(text))
    if closes:
        return text[closes[-1].end() :].strip()
    if _REASONING_OPEN.search(text):
        return ""
    return text


def split_system(messages: list[Message]) -> tuple[str, list[Message]]:
    """Pull system messages out for backends that take them as a separate field.

    Anthropic and Gemini both want the system prompt outside the turn list.
    Multiple system messages are concatenated rather than dropped.
    """
    system_parts = [m.text for m in messages if m.role == "system"]
    rest = [m for m in messages if m.role != "system"]
    return "\n\n".join(system_parts), rest
