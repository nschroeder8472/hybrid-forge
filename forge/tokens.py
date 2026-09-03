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
    from .providers.base import ImagePart, Message

# Bytes per token. English prose runs ~4; code and structured text run denser,
# and specs are mostly code, so 3.3 keeps the estimate on the safe side.
_BYTES_PER_TOKEN = 3.3

# Per-message framing (role markers, separators) charged by most chat APIs.
_MESSAGE_OVERHEAD = 4

# Pixels per token. Anthropic and Gemini both bill image input in tokens, and
# Anthropic documents roughly `(width x height) / 750`.
_PIXELS_PER_TOKEN = 750

# What an image with no stated dimensions costs. Providers resize a large image
# down to a longest edge around 1568px before billing it, so an image cannot
# exceed about 1568^2 / 750 tokens however big the file is. Charging that for
# an unmeasured image keeps the gate an over-estimate, which is the direction
# it is allowed to be wrong in: a review prompt carrying an image priced at
# zero is several thousand tokens the gate believes it has room for.
_UNKNOWN_IMAGE_TOKENS = int(1568 * 1568 / _PIXELS_PER_TOKEN)


def estimate_text(text: str) -> int:
    if not text:
        return 0
    return int(len(text) / _BYTES_PER_TOKEN) + 1


def estimate_image(part: "ImagePart") -> int:
    """What one image costs the prompt it is attached to.

    From the dimensions the part carries, because the daemon does not decode
    images. An unmeasured image is charged the worst case rather than nothing —
    see `_UNKNOWN_IMAGE_TOKENS`.
    """
    if part.width > 0 and part.height > 0:
        return int(part.width * part.height / _PIXELS_PER_TOKEN) + 1
    return _UNKNOWN_IMAGE_TOKENS


def estimate_message(message: "Message") -> int:
    from .providers.base import ImagePart

    total = _MESSAGE_OVERHEAD
    for part in message.parts:
        total += (
            estimate_image(part)
            if isinstance(part, ImagePart)
            else estimate_text(part.text)
        )
    return total


def estimate_messages(messages: list["Message"]) -> int:
    return sum(estimate_message(message) for message in messages)


def format_tokens(count: int) -> str:
    """Human-readable count for logs and the dashboard."""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}k"
    return str(count)
