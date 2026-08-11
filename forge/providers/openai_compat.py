"""OpenAI-compatible chat completions.

One adapter, most of the ecosystem: Ollama, vLLM, LM Studio, llama.cpp's
server, LiteLLM, OpenRouter, Together, DeepSeek, and OpenAI itself all speak
this shape. Point `baseUrl` at whichever one you run.

Context window discovery is best-effort. Ollama exposes it on its native port;
most other servers do not expose it at all. Config always wins over discovery,
and discovery only fills a gap.

When Ollama is asked, it is asked what it is *serving* (`/api/ps`) before what
the model *could* do (`/api/show`). Those disagree routinely — 32768 against
131072 on a real box — and planning against the larger one hands the budget
gate a ceiling four times too high, so it approves prompts the server then
truncates from the front.
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
            payload["temperature"] = self.temperature(temperature)

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

    def _native_url(self) -> str | None:
        """Ollama's native API root, if this endpoint looks like Ollama.

        `/v1` is the compatibility prefix; the native API sits one level up.
        Anything that is not Ollama simply fails the calls below, which is why
        their failures are swallowed rather than raised.
        """
        if not self.base_url.endswith("/v1"):
            return None
        return self.base_url[: -len("/v1")]

    def _ollama_context(self) -> dict[str, int]:
        """What each Ollama source says this model's context is.

        Two different numbers, and the difference is the whole point:

        `served` is `num_ctx` of the instance actually loaded — the size of the
        KV cache Ollama allocated, and the real ceiling on a request.

        `trained` is the architectural maximum out of the GGUF metadata. It
        describes the model, not the server, and Ollama will happily serve a
        fraction of it: on one box `/api/show` reported 131072 while `/api/ps`
        reported 32768, a 4x gap.

        Believing `trained` defeats the budget gate. The gate exists to prove a
        prompt fits before anything is spent, and it would have passed a 90k
        prompt to a 32k server — where the front of it, meaning the system
        prompt and the spec, is silently dropped. What comes back then looks
        like a weak model rather than a truncated request.
        """
        native = self._native_url()
        if native is None:
            return {}

        found: dict[str, int] = {}
        entry = self._ollama_loaded()
        if entry:
            served = entry.get("context_length")
            if isinstance(served, int) and served > 0:
                found["served"] = served

        try:
            info = post_json(
                f"{native}/api/show",
                {"model": self.model},
                headers=self._headers(),
                timeout=15,
            )
        except Exception:  # noqa: BLE001 - discovery is optional by design
            return found
        for key, value in (info.get("model_info") or {}).items():
            if key.endswith(".context_length"):
                try:
                    found["trained"] = int(value)
                except (TypeError, ValueError):
                    pass
                break
        return found

    def _ollama_loaded(self) -> dict[str, Any]:
        """This model's entry in `/api/ps`, or `{}` if it is not resident."""
        native = self._native_url()
        if native is None:
            return {}
        try:
            data = get_json(f"{native}/api/ps", headers=self._headers(), timeout=10)
        except Exception:  # noqa: BLE001
            return {}
        if not isinstance(data, dict):
            return {}
        for entry in data.get("models") or []:
            if not isinstance(entry, dict):
                continue
            if self.model in (entry.get("name"), entry.get("model")):
                return entry
        return {}

    def _discover_context_window(self) -> int | None:
        """The window to plan against: what is served, not what is possible.

        Falls back to the architectural figure only when the model is not
        loaded and there is nothing better to go on — a guess that is too large
        is still better than the 8192 default for a model that can do far more,
        and `forge doctor` says so out loud when the two disagree.
        """
        found = self._ollama_context()
        return found.get("served") or found.get("trained") or None

    def diagnostics(self) -> list[str]:
        caps = self.capabilities()
        found = self._ollama_context()
        warnings: list[str] = []

        served = found.get("served")
        if served and caps.context_window > served:
            warnings.append(
                f"context window is set to {caps.context_window:,} but the server "
                f"is serving {served:,}. Prompts between the two will be accepted "
                f"here and silently truncated there, dropping the start of the "
                f"prompt — the system message and the spec. Set contextWindow to "
                f"{served:,}, or raise num_ctx on the server."
            )

        trained = found.get("trained")
        if served and trained and trained > served:
            warnings.append(
                f"note: this model was trained for {trained:,} but Ollama loaded "
                f"it with num_ctx={served:,}. Raise OLLAMA_CONTEXT_LENGTH or the "
                f"Modelfile to use the rest."
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

        entry = self._ollama_loaded()
        total, in_vram = entry.get("size") or 0, entry.get("size_vram") or 0
        if total and in_vram < total:
            share = 100 * in_vram / total
            warnings.append(
                f"only {share:.0f}% of {total / 2**30:.1f} GiB is in VRAM; the rest "
                f"runs on CPU. It will work, several times slower — a smaller "
                f"quantization or a lower num_ctx would keep it resident."
            )
        return warnings
