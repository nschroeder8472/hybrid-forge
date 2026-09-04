"""Google Gemini generateContent API.

Shape differs from both OpenAI and Anthropic: turns are `contents` with
`parts`, the assistant role is spelled `model`, the system prompt is
`systemInstruction`, and generation settings live under `generationConfig`.

The API key goes in the `x-goog-api-key` header rather than the query string,
so it does not end up in server access logs.
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
    ProviderAuthError,
    ProviderBadResponse,
    Usage,
    split_system,
)

# Known context windows, used when config does not supply one. Gemini has no
# per-model metadata endpoint on the generateContent surface.
_CONTEXT_WINDOWS = {
    "gemini-2.5-pro": 1_048_576,
    "gemini-2.5-flash": 1_048_576,
    "gemini-2.0-flash": 1_048_576,
}
_DEFAULT_CONTEXT = 32_768


def _parts(message: Message) -> list[dict[str, Any]]:
    """One turn's `parts`. An image travels as `inline_data`."""
    return [
        {
            "inline_data": {
                "mime_type": part.media_type,
                "data": base64.b64encode(part.data).decode("ascii"),
            }
        }
        if isinstance(part, ImagePart)
        else {"text": part.text}
        for part in message.parts
    ]


class GeminiProvider(Provider):
    kind = "gemini"

    def __init__(self, name: str, config: dict[str, Any]):
        super().__init__(name, config)
        self.base_url = str(
            config.get("baseUrl", "https://generativelanguage.googleapis.com/v1beta")
        ).rstrip("/")
        key_env = config.get("apiKeyEnv", "GEMINI_API_KEY")
        self.api_key = os.environ.get(key_env, "") or config.get("apiKey", "")

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise ProviderAuthError(
                f"no API key for provider {self.name!r}; set the environment variable "
                f"named by its apiKeyEnv (default GEMINI_API_KEY)"
            )
        return {"x-goog-api-key": self.api_key}

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
        system, turns = split_system(messages)
        payload: dict[str, Any] = {
            "contents": [
                {
                    # Gemini calls the assistant role "model".
                    "role": "model" if m.role == "assistant" else "user",
                    "parts": _parts(m),
                }
                for m in turns
            ],
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature": self.temperature(temperature),
            },
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}

        data = post_json(
            f"{self.base_url}/models/{self.model}:generateContent",
            payload,
            headers=self._headers(),
            timeout=timeout,
        )

        try:
            candidate = data["candidates"][0]
        except (KeyError, IndexError, TypeError) as exc:
            # A prompt blocked by safety filters comes back with no candidates
            # and a promptFeedback block explaining why.
            feedback = data.get("promptFeedback") or {}
            if feedback:
                return Completion(
                    text=f"REFUSED: prompt blocked ({feedback.get('blockReason')}).",
                    usage=Usage(estimated=True),
                    finish_reason="refusal",
                    model=self.model,
                    raw=data,
                )
            raise ProviderBadResponse(f"unexpected response shape: {str(data)[:400]}") from exc

        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts)

        raw_usage = data.get("usageMetadata") or {}
        usage = Usage(
            prompt_tokens=int(raw_usage.get("promptTokenCount", 0)),
            completion_tokens=int(raw_usage.get("candidatesTokenCount", 0)),
            estimated=not raw_usage,
        )

        return Completion(
            text=text,
            usage=usage,
            finish_reason=candidate.get("finishReason", "STOP"),
            model=self.model,
            raw=data,
        )

    def capabilities(self) -> Capabilities:
        context_window = int(self.config.get("contextWindow", 0) or 0)
        if not context_window:
            context_window = next(
                (w for prefix, w in _CONTEXT_WINDOWS.items() if self.model.startswith(prefix)),
                _DEFAULT_CONTEXT,
            )
        return Capabilities(
            context_window=context_window,
            max_output_tokens=int(self.config.get("maxOutputTokens", 8192)),
            # Every Gemini model on this surface takes images; `vision: false`
            # is the operator's off switch, as on the Anthropic adapter.
            supports_images=bool(self.config.get("vision", True)),
        )
