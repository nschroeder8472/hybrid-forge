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


def _tagged(name: str) -> str:
    """An Ollama model name with its implicit `:latest` made explicit.

    A digest reference (`model@sha256:…`) carries no tag and is left alone.
    """
    name = name.strip()
    if not name or "@" in name or ":" in name:
        return name
    return f"{name}:latest"


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

    # Sent and silently discarded by Ollama's compatibility shim. vLLM, LM
    # Studio and llama.cpp's server accept them, so they are worth sending —
    # but on Ollama they belong in the Modelfile, and `diagnostics` says so.
    _NOT_ON_OLLAMA = ("top_k", "min_p")

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
        timeout: int = 600,
    ) -> Completion:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
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
            text = message["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderBadResponse(f"unexpected response shape: {str(data)[:400]}") from exc

        finish_reason = choice.get("finish_reason") or "stop"
        if not text.strip():
            # Thinking models served over this shape put their chain of thought
            # in a non-standard sibling field and leave `content` empty until
            # they finish thinking. Spending the whole output budget there
            # yields an empty string, which every caller downstream reports as
            # malformed output — naming the real cause here saves that hunt.
            reasoning = _reasoning_text(message)
            if reasoning and finish_reason in ("length", "max_tokens"):
                raise ProviderBadResponse(
                    f"{self.model} spent its entire {max_tokens:,}-token output budget "
                    "on hidden reasoning and never began its answer. Raise "
                    "`maxOutputTokens` for this model, or turn thinking off with "
                    '`"extraBody": {"reasoning_effort": "none"}`.'
                )

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

        info = self._ollama_show()
        if not info:
            return found
        for key, value in (info.get("model_info") or {}).items():
            if key.endswith(".context_length"):
                try:
                    found["trained"] = int(value)
                except (TypeError, ValueError):
                    pass
                break
        return found

    @staticmethod
    def _same_model(configured: str, reported: str | None) -> bool:
        """Whether an `/api/ps` entry names the model this provider is configured for.

        Ollama reports fully qualified names — `forge-exec:latest` — and config
        almost always omits the tag, because that is how every `ollama run`
        example writes it. Comparing the two exactly therefore never matches,
        and the failure is silent in the worst possible way: the served window
        is discarded, discovery falls back to the architectural maximum, and
        the budget gate plans against 262,144 tokens for a server holding
        32,768. What overflows is then truncated from the *front* — the system
        prompt and the spec — so the model answers a question it was never
        fully asked and reads as merely weak.
        """
        if not reported:
            return False
        return _tagged(configured) == _tagged(reported)

    def _ollama_show(self) -> dict[str, Any]:
        """`/api/show` for this model, or `{}` when it cannot be reached.

        Carries the architectural window under `model_info` and the base
        model's own `PARAMETER` lines under `parameters` — the recipe its
        authors shipped, which a generated Modelfile should preserve rather
        than replace with defaults.
        """
        native = self._native_url()
        if native is None:
            return {}
        try:
            info = post_json(
                f"{native}/api/show",
                {"model": self.model},
                headers=self._headers(),
                timeout=15,
            )
        except Exception:  # noqa: BLE001 - discovery is optional by design
            return {}
        return info if isinstance(info, dict) else {}

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
            if self._same_model(self.model, entry.get("name")) or self._same_model(
                self.model, entry.get("model")
            ):
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

        # Measured, not assumed: against Ollama 0.32, `top_p 0.01` collapses
        # six samples to one and `top_k 1` leaves all six distinct. The shim
        # accepts both and applies only the OpenAI-standard ones, so a knob set
        # here in good faith does nothing and says nothing.
        dropped = [k for k in self._NOT_ON_OLLAMA if k in self.sampling]
        if dropped and self._native_url() and self._ollama_context():
            warnings.append(
                f"{', '.join(dropped)} set, but Ollama's OpenAI endpoint ignores "
                f"{'them' if len(dropped) > 1 else 'it'}. Put "
                f"{'those' if len(dropped) > 1 else 'that'} in the Modelfile "
                f"(`PARAMETER {dropped[0]} …`) instead, or they have no effect."
            )

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


# Every server spells the field differently, and none of them are in the spec:
# Ollama says `reasoning`, DeepSeek and vLLM say `reasoning_content`, and some
# builds nest the text under a dict rather than returning it flat.
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
