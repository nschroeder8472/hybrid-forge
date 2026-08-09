"""Token estimation.

A deliberate over-estimate. The budget gate uses this to decide whether a
request fits before spending anything on it, and the two failure modes are not
symmetric: over-estimating costs an unnecessary trim, under-estimating costs a
mid-run context overflow after the prompt has already been paid for.

Providers with a real tokenizer or a counting endpoint override
`Provider.count_tokens` — the Anthropic adapter does. This is the floor.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .providers.base import Message

# Bytes per token. English prose runs ~4; code and structured text run denser,
# and specs are mostly code, so 3.3 keeps the estimate on the safe side.
_BYTES_PER_TOKEN = 3.3

# Per-message framing (role markers, separators) charged by most chat APIs.
_MESSAGE_OVERHEAD = 4


def estimate_text(text: str) -> int:
    if not text:
        return 0
    return int(len(text) / _BYTES_PER_TOKEN) + 1


def estimate_messages(messages: list["Message"]) -> int:
    return sum(estimate_text(m.content) + _MESSAGE_OVERHEAD for m in messages)


def format_tokens(count: int) -> str:
    """Human-readable count for logs and the dashboard."""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}k"
    return str(count)
