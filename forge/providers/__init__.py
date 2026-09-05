"""Provider registry — one local backend, three ways to reach a cloud one.

Local models are llama.cpp and only llama.cpp. That is a deliberate narrowing:
the adapters this replaced each had their own way of being asked what they were
serving, their own way of being told to load something else, and their own
silent failures — an Ollama name that never matched because config omitted
`:latest`, an engine that answered to any model id and echoed it back. Every
one of those cost a run before it was found, and the cost of carrying them was
paid in diagnostics that could only say what all of them had in common.

One local backend means the opposite: errors and capabilities can name presets,
`--models-max` slots, reasoning budgets, and the argv a child server will be
spawned with, because there is exactly one thing on the other end.

Cloud is unchanged and stays plural — `openai` for OpenAI and the gateways that
speak its wire, `anthropic`, `gemini`, and `claude-cli` for a subscription
rather than a key. Adding a backend is still one module and one line here.
"""

from __future__ import annotations

from typing import Any

from .anthropic_api import AnthropicProvider
from .base import (
    Capabilities,
    Completion,
    ContextOverflow,
    ImagePart,
    Message,
    ToolCall,
    ToolResult,
    ToolSpec,
    Provider,
    ProviderAuthError,
    ProviderBadResponse,
    ProviderError,
    ProviderCannotSee,
    ProviderUnreachable,
    RateLimited,
    Role,
    TextPart,
    Usage,
)
from .claude_cli import ClaudeCLIProvider
from .gemini import GeminiProvider
from .llamacpp import LlamaCppProvider
from .openai_compat import OpenAICompatProvider

_REGISTRY: dict[str, type[Provider]] = {
    LlamaCppProvider.kind: LlamaCppProvider,
    OpenAICompatProvider.kind: OpenAICompatProvider,
    AnthropicProvider.kind: AnthropicProvider,
    GeminiProvider.kind: GeminiProvider,
    ClaudeCLIProvider.kind: ClaudeCLIProvider,
}

# The local backend, under every spelling of its name.
_ALIASES = {
    "llama.cpp": "llamacpp",
    "llama-cpp": "llamacpp",
    "llama-server": "llamacpp",
    "llama": "llamacpp",
    "local": "llamacpp",
    # Cloud gateways that speak OpenAI's wire.
    "openai-compatible": "openai",
    "openrouter": "openai",
    "litellm": "openai",
    "together": "openai",
    "deepseek": "openai",
    "claude": "anthropic",
    "google": "gemini",
    "claude-code": "claude-cli",
}

# Local backends forge used to carry, and what to do instead. Named rather
# than merely absent: a config written against one of these is not a typo, and
# "unknown kind, valid kinds are …" would send its author looking for a
# spelling mistake that is not there.
_RETIRED = {
    "ollama": (
        "Ollama is no longer a forge backend. Point `llama-server` at the same "
        "GGUF and use `\"kind\": \"llamacpp\"` — forge writes the preset for "
        "you with `forge init`, and gets the context window from the argv the "
        "router will spawn rather than guessing between /api/ps and /api/show."
    ),
    "vllm": (
        "vLLM is no longer a forge backend. If it is serving a local model, "
        "use `\"kind\": \"llamacpp\"`; if it is a gateway in front of a hosted "
        "one, `\"kind\": \"openai\"` still speaks to it."
    ),
    "lmstudio": (
        "LM Studio is no longer a forge backend. It serves GGUF, so point "
        "`llama-server` at the same file and use `\"kind\": \"llamacpp\"`."
    ),
    "freetoken": (
        "FreeToken is no longer a forge backend. Its one job was swapping "
        "checkpoints on a single endpoint, which `llama-server --models-dir` "
        "does natively and without answering to a model id it is not serving. "
        "Use `\"kind\": \"llamacpp\"`."
    ),
    "ft": "freetoken",
    "command": (
        "The `command` backend is gone. It existed to wrap a local model with "
        "no HTTP server — `llama-cli`, `mlx_lm.generate`, a script of your "
        "own. Serve the GGUF with `llama-server` and use "
        "`\"kind\": \"llamacpp\"` instead."
    ),
    "cli": "command",
    "subprocess": "command",
}


def available_kinds() -> list[str]:
    return sorted(_REGISTRY)


def retired_kind(raw_kind: str) -> str:
    """Why a backend forge used to carry is not there, or empty if it never was.

    Followed through one level of aliasing, so `ft` and `subprocess` get the
    same sentence as the names they were short for.
    """
    note = _RETIRED.get(raw_kind.lower(), "")
    return _RETIRED.get(note, note) if note in _RETIRED else note


def build_provider(name: str, config: dict[str, Any]) -> Provider:
    """Construct a provider from its config block.

    `name` is the key it was declared under, used in errors and the dashboard
    so a failure names the model the user recognizes rather than a class.

    A block with no `kind` is llama.cpp. That is the local backend and the one
    a config written for this project overwhelmingly means; every cloud kind
    needs a credential named alongside it anyway, so none of them are reached
    by omission.
    """
    raw_kind = str(config.get("kind", LlamaCppProvider.kind)).lower()
    kind = _ALIASES.get(raw_kind, raw_kind)
    try:
        provider_class = _REGISTRY[kind]
    except KeyError:
        retired = retired_kind(raw_kind)
        if retired:
            raise ValueError(f"model {name!r}: {retired}") from None
        raise ValueError(
            f"model {name!r} has unknown kind {raw_kind!r}; "
            f"available kinds: {', '.join(available_kinds())}"
        ) from None
    return provider_class(name, config)


__all__ = [
    "Capabilities",
    "Completion",
    "ContextOverflow",
    "ImagePart",
    "Message",
    "ToolCall",
    "ToolResult",
    "ToolSpec",
    "Provider",
    "ProviderAuthError",
    "ProviderBadResponse",
    "ProviderError",
    "ProviderCannotSee",
    "ProviderUnreachable",
    "RateLimited",
    "Role",
    "TextPart",
    "Usage",
    "available_kinds",
    "build_provider",
]
