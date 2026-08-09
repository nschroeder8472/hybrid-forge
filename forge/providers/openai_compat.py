"""OpenAI-compatible chat completions.

One adapter, most of the ecosystem: Ollama, vLLM, LM Studio, llama.cpp's
server, LiteLLM, OpenRouter, Together, DeepSeek, and OpenAI itself all speak
this shape. Point `baseUrl` at whichever one you run.

Context window discovery is best-effort. Ollama exposes it through `/api/show`
on its native port; most other servers do not expose it at all. Config always
wins over discovery, and discovery only fills a gap.
"""

from __future__ import annotations

import os
from typing import Any

from ._http import get_json, post_json
from .base import Capabilities, Completion, Message, Provider, ProviderBadResponse, Usage


class OpenAICompatProvider(Provider):
    kind = "openai"

    def __init__(self, name: str, config: dict[str, Any]):
        super().__init__(name, config)
        self.base_url = str(config.get("baseUrl", "http://localhost:11434/v1")).rstrip("/")
        key_env = config.get("apiKeyEnv")
        self.api_key = os.environ.get(key_env, "") if key_env else config.get("apiKey", "")
        self._caps: Capabilities | None = None
        # Extra body fields for backends with useful non-standard knobs
        # (vLLM's `top_k`, Ollama's `options`, OpenRouter's routing prefs).
        self.extra_body: dict[str, Any] = config.get("extraBody", {}) or {}

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
        timeout: int = 600,
    ) -> Completion:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "max_tokens": max_tokens,
            **self.extra_body,
        }
        if self.capabilities().supports_temperature:
            payload["temperature"] = temperature

        data = post_json(
            f"{self.base_url}/chat/completions",
            payload,
            headers=self._headers(),
            timeout=timeout,
        )

        try:
            choice = data["choices"][0]
            text = choice["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderBadResponse(f"unexpected response shape: {str(data)[:400]}") from exc

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
            finish_reason=choice.get("finish_reason") or "stop",
            model=data.get("model", self.model),
            raw=data,
        )

    def capabilities(self) -> Capabilities:
        if self._caps is not None:
            return self._caps

        caps = Capabilities(
            context_window=int(self.config.get("contextWindow", 0) or 0),
            max_output_tokens=int(self.config.get("maxOutputTokens", 4096)),
            supports_temperature=bool(self.config.get("supportsTemperature", True)),
        )
        if not caps.context_window:
            caps.context_window = self._discover_context_window() or 8192
        self._caps = caps
        return caps

    def _discover_context_window(self) -> int | None:
        """Ask Ollama for the model's context length, if this is Ollama.

        `/v1` is the compatibility prefix; the native API sits one level up.
        Anything that is not Ollama simply fails this call and we fall back to
        the default, which is why the failure is swallowed rather than raised.
        """
        if not self.base_url.endswith("/v1"):
            return None
        native = self.base_url[: -len("/v1")]
        try:
            data = get_json(f"{native}/api/tags", headers=self._headers(), timeout=10)
        except Exception:  # noqa: BLE001 - discovery is optional by design
            return None
        if not isinstance(data, dict) or "models" not in data:
            return None
        try:
            info = post_json(
                f"{native}/api/show",
                {"model": self.model},
                headers=self._headers(),
                timeout=15,
            )
        except Exception:  # noqa: BLE001
            return None
        details = info.get("model_info") or {}
        for key, value in details.items():
            if key.endswith(".context_length"):
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return None
        return None
