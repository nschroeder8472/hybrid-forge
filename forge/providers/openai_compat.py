"""OpenAI-compatible chat completions, for models forge does not host.

OpenAI itself and the gateways that speak its wire: OpenRouter, LiteLLM,
Together, DeepSeek. Point `baseUrl` at whichever one you use, or leave it and
get OpenAI.

This is the *cloud* half of forge's model support. Local models are llama.cpp
and nothing else, which is why the discovery this adapter used to carry is
gone: it existed to reconcile Ollama's two disagreeing answers about what
context window was actually loaded, and there is no longer an Ollama here to
ask. A hosted endpoint publishes no such thing, so `contextWindow` is
configuration, and a missing one is a documented default rather than a guess
dressed up as a measurement.

`llamacpp` extends this class for the wire format, and only for that — it gets
its window from the argv the router will spawn its child server with, which is
a fact rather than a best effort.
"""

from __future__ import annotations

import base64
import os
from typing import Any

from ._http import post_json
from .base import (
    DERIVE_TIMEOUT,
    Capabilities,
    Completion,
    ImagePart,
    Message,
    Provider,
    ProviderBadResponse,
    ProviderError,
    Usage,
    strip_reasoning,
)

# What a hosted endpoint gets when nothing says otherwise. Every model worth
# pointing forge at has more, so this is a floor that makes the budget gate
# work rather than an estimate of anything — `forge doctor` says to set it.
DEFAULT_CONTEXT_WINDOW = 8192


def _turn(message: Message) -> dict[str, Any]:
    """One turn in the chat-completions shape.

    A string stays a string. The parts form is the multi-part `content` array
    every OpenAI-compatible server that accepts images accepts, with the image
    inlined as a `data:` URL rather than fetched from one — the server would
    be reaching back out over the network for a file only the daemon has.
    """
    if isinstance(message.content, str):
        return {"role": message.role, "content": message.content}
    return {
        "role": message.role,
        "content": [
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{part.media_type};base64,"
                    + base64.b64encode(part.data).decode("ascii")
                },
            }
            if isinstance(part, ImagePart)
            else {"type": "text", "text": part.text}
            for part in message.parts
        ],
    }


class OpenAICompatProvider(Provider):
    kind = "openai"

    def __init__(self, name: str, config: dict[str, Any]):
        super().__init__(name, config)
        self.base_url = str(
            config.get("baseUrl", "https://api.openai.com/v1")
        ).rstrip("/")
        key_env = config.get("apiKeyEnv")
        self.api_key = os.environ.get(key_env, "") if key_env else config.get("apiKey", "")
        self._caps: Capabilities | None = None
        # Extra body fields for endpoints with useful non-standard knobs
        # (OpenRouter's routing preferences, a gateway's own extensions).
        self.extra_body: dict[str, Any] = config.get("extraBody", {}) or {}
        self.sampling = self._sampling_from(config)

    # Config key -> OpenAI wire name. `temperature` is absent on purpose: the
    # loop asks for one per role and `Provider.temperature` decides whether
    # config overrides it, which is a different rule from these.
    _SAMPLING: tuple[tuple[str, str, type], ...] = (
        ("topP", "top_p", float),
        ("topK", "top_k", int),
        ("minP", "min_p", float),
        ("presencePenalty", "presence_penalty", float),
        ("frequencyPenalty", "frequency_penalty", float),
    )

    @classmethod
    def _sampling_from(cls, config: dict[str, Any]) -> dict[str, Any]:
        """Sampling knobs to put on every request, in their wire spelling.

        Only what is configured. An unset knob is left off the payload rather
        than defaulted, so a model's own shipped recipe still applies —
        qwen3-coder ships `top_p 0.8` and sending 1.0 because nobody chose one
        would quietly overrule it.
        """
        found: dict[str, Any] = {}
        for key, wire, cast in cls._SAMPLING:
            value = config.get(key)
            if value is not None:
                found[wire] = cast(value)
        return found

    def _headers(self) -> dict[str, str]:
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        headers.update(self.config.get("headers", {}) or {})
        return headers

    def complete(
        self,
        messages: list[Message],
        *,
        max_tokens: int,
        temperature: float = 0.2,
        timeout: int = DERIVE_TIMEOUT,
    ) -> Completion:
        # Zero means the caller did not care; the budget decides. An
        # explicit timeout is always truthy and passes through.
        timeout = timeout or self.request_timeout(max_tokens)
        self._require_vision(messages)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [_turn(m) for m in messages],
            "max_tokens": max_tokens,
            **self.sampling,
            # Last, so a hand-written body can still override anything above it.
            **self.extra_body,
        }
        if self.capabilities().supports_temperature:
            payload["temperature"] = self.temperature(temperature)

        data = post_json(
            f"{self.base_url}/chat/completions",
            payload,
            headers=self._headers(),
            timeout=timeout,
        )

        try:
            choice = data["choices"][0]
            message = choice["message"]
            # Here rather than in each parser: a reply that carries its own
            # chain of thought in `content` is one reply shape, and every
            # caller downstream wants the answer out of it.
            text = strip_reasoning(message["content"] or "")
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderBadResponse(f"unexpected response shape: {str(data)[:400]}") from exc

        recovered = ""
        finish_reason = choice.get("finish_reason") or "stop"
        if not text.strip():
            # Thinking models served over this shape put their chain of thought
            # in a non-standard sibling field and leave `content` empty until
            # they finish thinking. Spending the whole output budget there
            # yields an empty string, which every caller downstream reports as
            # malformed output — naming the real cause here saves that hunt.
            reasoning = _reasoning_text(message)
            if reasoning and finish_reason in ("length", "max_tokens"):
                data, recovered = self._without_thinking(
                    payload, max_tokens, timeout
                )
                choice = data["choices"][0]
                message = choice.get("message") or {}
                text = strip_reasoning(message.get("content") or "")
                finish_reason = choice.get("finish_reason") or "stop"

        raw_usage = data.get("usage") or {}
        usage = Usage(
            prompt_tokens=int(raw_usage.get("prompt_tokens", 0)),
            completion_tokens=int(raw_usage.get("completion_tokens", 0)),
            estimated=not raw_usage,
        )
        if usage.estimated:
            usage.prompt_tokens = self.count_tokens(messages)
            usage.completion_tokens = self.count_tokens([Message(role="assistant", content=text)])

        return Completion(
            text=text,
            usage=usage,
            finish_reason=finish_reason,
            model=data.get("model", self.model),
            raw=data,
            recovered=recovered,
        )

    def _without_thinking(
        self, payload: dict[str, Any], max_tokens: int, timeout: int
    ) -> tuple[dict[str, Any], str]:
        """One more attempt with thinking turned off. Raises if that is not on.

        The budget is not the problem and raising it does not fix this: a model
        that reasons until it is cut off will reason until it is cut off at any
        ceiling. The only thing that changes the outcome is asking it not to,
        which is the remedy this used to print and leave to a person.

        Worth doing automatically because the alternative is losing the call
        outright. On one run this cost five calls — three of them the memory
        step, whose whole answer is `NOTHING` or three sentences, and two of
        them a tester that then had to be retried from scratch.

        Never against an operator's own setting. Someone who has written
        `reasoning_effort` into `extraBody` has chosen how this model thinks,
        and quietly overruling it would make the configuration a suggestion.
        Reported rather than done in silence for the same reason: an answer
        produced by a model the operator did not configure is still worth
        knowing about.
        """
        chosen = {key.lower() for key in self.extra_body}
        if "reasoning_effort" in chosen or "reasoning" in chosen:
            raise ProviderBadResponse(
                f"{self.model} spent its entire {max_tokens:,}-token output budget "
                "on hidden reasoning and never began its answer, and `extraBody` "
                "already sets how it reasons, so this was not overruled. Raise "
                "`maxOutputTokens` for this model, or turn thinking off with "
                '`"extraBody": {"reasoning_effort": "none"}`.'
            )

        retry = {**payload, "reasoning_effort": "none"}
        try:
            data = post_json(
                f"{self.base_url}/chat/completions",
                retry,
                headers=self._headers(),
                timeout=timeout,
            )
            text = ((data["choices"][0].get("message") or {}).get("content")) or ""
        except (ProviderError, KeyError, IndexError, TypeError) as exc:
            raise ProviderBadResponse(
                f"{self.model} spent its entire {max_tokens:,}-token output budget "
                f"on hidden reasoning and never began its answer, and asking it "
                f"again with `reasoning_effort: none` did not help ({exc}). Raise "
                "`maxOutputTokens` for this model, or turn thinking off with "
                '`"extraBody": {"reasoning_effort": "none"}`.'
            ) from exc

        if not text.strip():
            raise ProviderBadResponse(
                f"{self.model} spent its entire {max_tokens:,}-token output budget "
                "on hidden reasoning and never began its answer, and answered "
                "with nothing when asked again without thinking. Raise "
                "`maxOutputTokens` for this model, or turn thinking off with "
                '`"extraBody": {"reasoning_effort": "none"}`.'
            )
        return data, (
            f"{self.model} spent its entire {max_tokens:,}-token output budget on "
            f"hidden reasoning and never began its answer; asked again with "
            f"`reasoning_effort: none`, which worked. Set that in `extraBody` for "
            f"this model, or raise `maxOutputTokens`, so the run is not paying "
            f"for a wasted call each time."
        )

    def capabilities(self) -> Capabilities:
        """What this endpoint can do, from configuration.

        No discovery. A hosted endpoint does not publish the window it will
        serve, and the adapter that used to ask — Ollama's — is not a forge
        backend any more. `contextWindow` is therefore the operator's number,
        and `diagnostics` says so when it has been left at the default.
        """
        if self._caps is not None:
            return self._caps

        caps = Capabilities(
            context_window=int(self.config.get("contextWindow", 0) or 0),
            max_output_tokens=int(self.config.get("maxOutputTokens", 4096)),
            supports_temperature=bool(self.config.get("supportsTemperature", True)),
            # Off unless the operator says otherwise. "OpenAI-compatible" is a
            # claim about the request shape, not about the model behind it, and
            # nothing here can ask an arbitrary endpoint whether it can see.
            supports_images=bool(self.config.get("multimodal", False)),
        )
        if not caps.context_window:
            caps.context_window = DEFAULT_CONTEXT_WINDOW
        self._caps = caps
        return caps

    def diagnostics(self) -> list[str]:
        caps = self.capabilities()
        warnings: list[str] = self.timeout_notes()

        if not int(self.config.get("contextWindow", 0) or 0):
            warnings.append(
                f"contextWindow is not set, so the budget gate is planning "
                f"against {DEFAULT_CONTEXT_WINDOW:,} tokens. Nothing here can "
                f"ask the endpoint what it will actually serve, and every "
                f"model worth pointing forge at has more than this — set it "
                f"from the model's documented window."
            )

        # Output reserve comes straight off the input budget, so what matters
        # is not the ratio but what is left to put a prompt in. Judged as a
        # fraction of the window rather than a fixed count, since a third of a
        # small window and a third of a large one are both survivable; a whole
        # window reserved for output is not, at any size.
        budget = caps.input_budget(caps.max_output_tokens)
        if budget < caps.context_window // 3:
            warnings.append(
                f"maxOutputTokens is {caps.max_output_tokens:,} of a "
                f"{caps.context_window:,} window, leaving {budget:,} tokens for "
                f"the prompt"
                + (
                    ". Every ticket overflows before it starts — lower it."
                    if budget <= 0
                    else ". A ticket with more than one reference file will not fit."
                )
            )
        return warnings


# Every server spells the field differently, and none of them are in the spec:
# llama.cpp and DeepSeek say `reasoning_content`, others say `reasoning`, and
# some builds nest the text under a dict rather than returning it flat. Kept in
# the shared class because `llamacpp` needs it too — a thinking model that
# never begins its answer is the failure this backend sees most.
_REASONING_KEYS = ("reasoning", "reasoning_content")


def _reasoning_text(message: dict[str, Any]) -> str:
    """The hidden chain of thought a thinking model returned, if any."""
    for key in _REASONING_KEYS:
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, dict):
            nested = value.get("content") or value.get("text")
            if isinstance(nested, str) and nested.strip():
                return nested
    return ""
