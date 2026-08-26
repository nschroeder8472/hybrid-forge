"""Provider registry — the seam that keeps "any model the user brings" true.

Adding a backend means writing one module and adding one line to `_REGISTRY`.
Nothing in the loop, the budget gate, the dashboard, or the config schema needs
to know the new kind exists.
"""

from __future__ import annotations

from typing import Any

from .anthropic_api import AnthropicProvider
from .base import (
    Capabilities,
    Completion,
    ContextOverflow,
    Message,
    Provider,
    ProviderAuthError,
    ProviderBadResponse,
    ProviderError,
    ProviderUnreachable,
    RateLimited,
    Role,
    Usage,
)
from .claude_cli import ClaudeCLIProvider
from .freetoken import FreeTokenProvider
from .gemini import GeminiProvider
from .openai_compat import OpenAICompatProvider
from .subprocess_cli import SubprocessProvider

_REGISTRY: dict[str, type[Provider]] = {
    OpenAICompatProvider.kind: OpenAICompatProvider,
    AnthropicProvider.kind: AnthropicProvider,
    GeminiProvider.kind: GeminiProvider,
    SubprocessProvider.kind: SubprocessProvider,
    ClaudeCLIProvider.kind: ClaudeCLIProvider,
    FreeTokenProvider.kind: FreeTokenProvider,
}

# Aliases for the names people actually type in config.
_ALIASES = {
    "openai-compatible": "openai",
    "ollama": "openai",
    "vllm": "openai",
    "lmstudio": "openai",
    "openrouter": "openai",
    "litellm": "openai",
    "claude": "anthropic",
    "google": "gemini",
    "cli": "command",
    "subprocess": "command",
    "claude-code": "claude-cli",
    "ft": "freetoken",
}


def available_kinds() -> list[str]:
    return sorted(_REGISTRY)


def build_provider(name: str, config: dict[str, Any]) -> Provider:
    """Construct a provider from its config block.

    `name` is the key it was declared under, used in errors and the dashboard
    so a failure names the model the user recognizes rather than a class.
    """
    raw_kind = str(config.get("kind", "openai")).lower()
    kind = _ALIASES.get(raw_kind, raw_kind)
    try:
        provider_class = _REGISTRY[kind]
    except KeyError:
        raise ValueError(
            f"model {name!r} has unknown kind {raw_kind!r}; "
            f"available kinds: {', '.join(available_kinds())}"
        ) from None
    return provider_class(name, config)


__all__ = [
    "Capabilities",
    "Completion",
    "ContextOverflow",
    "Message",
    "Provider",
    "ProviderAuthError",
    "ProviderBadResponse",
    "ProviderError",
    "ProviderUnreachable",
    "RateLimited",
    "Role",
    "Usage",
    "available_kinds",
    "build_provider",
]
