"""Anthropic Messages API.

Raw HTTP rather than the `anthropic` SDK on purpose: this is one of four
interchangeable adapters behind the `Provider` interface, and the daemon is
stdlib-only so an overnight run cannot fail to start on a missing package.
Swapping this file for the SDK later changes nothing above it.

Three things here differ from the OpenAI-compatible shape and are easy to get
wrong:

1. The system prompt is a top-level field, not a message.
2. `temperature` is **rejected with a 400** on current models (Opus 5, Opus
   4.8/4.7, Sonnet 5, Fable 5). Sending it is a hard failure, not a warning.
3. A safety decline returns HTTP 200 with `stop_reason: "refusal"` and no
   content. Code that reads `content[0]` unconditionally breaks on it.
"""

from __future__ import annotations

import base64
import os
from collections.abc import Sequence
from typing import Any

from ._http import get_json, post_json
from .base import (
    ToolSpec,
    DERIVE_TIMEOUT,
    Capabilities,
    Completion,
    ImagePart,
    Message,
    Provider,
    ProviderAuthError,
    Usage,
    split_system,
)

API_VERSION = "2023-06-01"


def _turn(message: Message) -> dict[str, Any]:
    """One turn in the shape the Messages API takes.

    A plain string stays a plain string: content blocks are equivalent, and
    sending them where a string was sent before would change every recorded
    request body for no reason.
    """
    if isinstance(message.content, str):
        return {"role": message.role, "content": message.content}
    return {
        "role": message.role,
        "content": [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": part.media_type,
                    "data": base64.b64encode(part.data).decode("ascii"),
                },
            }
            if isinstance(part, ImagePart)
            else {"type": "text", "text": part.text}
            for part in message.parts
        ],
    }


# Models that reject temperature/top_p/top_k with a 400. Matched as prefixes so
# a dated snapshot of the same family is covered too.
_NO_SAMPLING_PARAMS = (
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-sonnet-5",
    "claude-fable-5",
    "claude-mythos-5",
)

# Fallback context windows for when the Models API is unreachable. The live
# values come from GET /v1/models/{id}; these only prevent a hard failure.
_FALLBACK_CONTEXT = {
    "claude-haiku-4-5": 200_000,
}
_DEFAULT_CONTEXT = 1_000_000


class AnthropicProvider(Provider):
    kind = "anthropic"

    def __init__(self, name: str, config: dict[str, Any]):
        super().__init__(name, config)
        self.base_url = str(config.get("baseUrl", "https://api.anthropic.com")).rstrip("/")
        key_env = config.get("apiKeyEnv", "ANTHROPIC_API_KEY")
        self.api_key = os.environ.get(key_env, "") or config.get("apiKey", "")
        self.model = config.get("model", "claude-opus-5")
        self._caps: Capabilities | None = None

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise ProviderAuthError(
                f"no API key for provider {self.name!r}; set the environment variable "
                f"named by its apiKeyEnv (default ANTHROPIC_API_KEY)"
            )
        return {"x-api-key": self.api_key, "anthropic-version": API_VERSION}

    def _accepts_temperature(self) -> bool:
        return not self.model.startswith(_NO_SAMPLING_PARAMS)

    def complete(
        self,
        messages: list[Message],
        *,
        max_tokens: int,
        temperature: float = 0.2,
        timeout: int = DERIVE_TIMEOUT,
        # Accepted and ignored: `capabilities().supports_tools` is false here,
        # so the caller has already built a prompt that does not need them.
        # Taking the argument anyway means no call site has to ask which kind
        # of provider a role happens to have.
        tools: Sequence[ToolSpec] = (),
    ) -> Completion:
        # Zero means the caller did not care; the budget decides. An
        # explicit timeout is always truthy and passes through.
        timeout = timeout or self.request_timeout(max_tokens)
        self._require_vision(messages)
        system, turns = split_system(messages)
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [_turn(m) for m in turns],
        }
        if system:
            payload["system"] = system
        if self._accepts_temperature():
            payload["temperature"] = self.temperature(temperature)

        effort = self.config.get("effort")
        if effort:
            payload["output_config"] = {"effort": effort}

        data = post_json(
            f"{self.base_url}/v1/messages",
            payload,
            headers=self._headers(),
            timeout=timeout,
        )

        stop_reason = data.get("stop_reason") or "end_turn"
        blocks = data.get("content") or []

        # A refusal is a successful HTTP response with no usable content. Surface
        # it as text the loop can log rather than crashing on an empty list.
        if stop_reason == "refusal":
            details = data.get("stop_details") or {}
            text = (
                "REFUSED: the model declined this request "
                f"(category={details.get('category')})."
            )
        else:
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")

        raw_usage = data.get("usage") or {}
        # `input_tokens` is the uncached remainder only; with a cached prefix
        # the cache counters hold nearly all of the real input. Counting them
        # separately keeps the ledger honest about what the call consumed.
        usage = Usage(
            prompt_tokens=int(raw_usage.get("input_tokens", 0)),
            completion_tokens=int(raw_usage.get("output_tokens", 0)),
            cache_creation_tokens=int(raw_usage.get("cache_creation_input_tokens", 0)),
            cache_read_tokens=int(raw_usage.get("cache_read_input_tokens", 0)),
            estimated=not raw_usage,
        )

        return Completion(
            text=text,
            usage=usage,
            finish_reason=stop_reason,
            model=data.get("model", self.model),
            raw=data,
        )

    def count_tokens(self, messages: list[Message]) -> int:
        """Exact count via the token-counting endpoint, with a local fallback.

        Worth the extra call before a large delegation: the budget gate's whole
        job is deciding whether a prompt fits, and a heuristic that is 20% low
        turns that decision into a wasted request.
        """
        system, turns = split_system(messages)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [_turn(m) for m in turns],
        }
        if system:
            payload["system"] = system
        try:
            data = post_json(
                f"{self.base_url}/v1/messages/count_tokens",
                payload,
                headers=self._headers(),
                timeout=30,
            )
            return int(data["input_tokens"])
        except Exception:  # noqa: BLE001 - fall back to the estimate
            return super().count_tokens(messages)

    def capabilities(self) -> Capabilities:
        if self._caps is not None:
            return self._caps

        context_window = int(self.config.get("contextWindow", 0) or 0)
        max_output = int(self.config.get("maxOutputTokens", 0) or 0)

        if not context_window or not max_output:
            live = self._discover()
            context_window = context_window or live[0]
            max_output = max_output or live[1]

        self._caps = Capabilities(
            context_window=context_window,
            max_output_tokens=max_output,
            supports_temperature=self._accepts_temperature(),
            # Every Claude model this adapter reaches takes images. `vision:
            # false` on the model block is for a future one that does not,
            # and for an operator who wants the refusal rather than the bill.
            supports_images=bool(self.config.get("vision", True)),
        )
        return self._caps

    def _discover(self) -> tuple[int, int]:
        """Read the live context window from the Models API.

        `max_input_tokens` is the context window; there is no `context_window`
        field. Failure here is non-fatal — config or the defaults take over.
        """
        try:
            data = get_json(
                f"{self.base_url}/v1/models/{self.model}",
                headers=self._headers(),
                timeout=15,
            )
            return (
                int(data.get("max_input_tokens") or _DEFAULT_CONTEXT),
                int(data.get("max_tokens") or 8192),
            )
        except Exception:  # noqa: BLE001 - discovery is optional
            for prefix, window in _FALLBACK_CONTEXT.items():
                if self.model.startswith(prefix):
                    return window, 8192
            return _DEFAULT_CONTEXT, 8192
