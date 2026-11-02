"""Image models: a provider family that returns pixels, not tokens.

A sibling of `Provider`, not a widening of it (docs/IMAGE-LOOP.md, problem 2,
decision 2). The budget gate reasons in tokens throughout, and an image call
folded into that accounting is one it treats as free. So a rendering carries
its own usage — images and seconds — and is recorded beside token usage rather
than inside it.

The error hierarchy is shared with `Provider`: `RateLimited` carrying
`reset_at` matters exactly as much here, and it is why `waiting_budget` needs
no changes to park a run on an image backend.

**The daemon moves image bytes and never looks inside them** (IMAGE-LOOP,
"No decoding in the daemon"). The one exception is a PNG's width and height,
which sit at fixed offsets in its header and are read with `struct`: the
budget gate prices an image by area, and reading two integers is not decoding.
"""

from __future__ import annotations

import struct
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@dataclass
class ImageUsage:
    """What one image call cost. `seconds` is the local backend's real budget."""

    images: int = 0
    seconds: float = 0.0
    cost_usd: float = 0.0
    estimated: bool = False


@dataclass
class Rendering:
    """One image an image model produced, and how it was produced.

    `seed` is the seed actually used, never the one asked for when they differ,
    and never zero when unknown — `None` is unknown. `revised_prompt` is set by
    a backend that rewrites prompts, so a rejected rendering can be traced to
    what was really sent.
    """

    data: bytes
    media_type: str
    width: int
    height: int
    seed: int | None
    revised_prompt: str = ""
    usage: ImageUsage = field(default_factory=ImageUsage)


@dataclass
class ImageCapabilities:
    """What an image model can do, declared by its config rather than probed."""

    supports_edit: bool = False
    supports_mask: bool = False
    supports_seed: bool = False
    sizes: list[tuple[int, int]] = field(default_factory=list)
    max_batch: int = 1
    cost_per_image_usd: float = 0.0


def png_size(data: bytes) -> tuple[int, int]:
    """A PNG's (width, height), read from its IHDR chunk; (0, 0) if not a PNG."""
    if len(data) < 24 or not data.startswith(_PNG_SIGNATURE) or data[12:16] != b"IHDR":
        return 0, 0
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


class ImageProvider(ABC):
    """An image model. Registered by `kind`, built from a config block."""

    kind: str = "abstract"

    def __init__(self, name: str, config: dict[str, Any]):
        self.name = name
        self.config = config

    @abstractmethod
    def generate(
        self,
        prompt: str,
        *,
        width: int = 0,
        height: int = 0,
        seed: int | None = None,
        negative: str = "",
    ) -> Rendering:
        """Render `prompt`. Width and height of 0 mean the workflow's own."""

    def edit(
        self,
        prompt: str,
        *,
        image: bytes,
        mask: bytes | None = None,
        seed: int | None = None,
    ) -> Rendering:
        """Change `image` as `prompt` instructs. Declared by `supports_edit`."""
        raise NotImplementedError(f"{self.kind} model {self.name!r} cannot edit")

    @abstractmethod
    def capabilities(self) -> ImageCapabilities:
        """What this model can do, from its config. Never contacts the backend."""

    @abstractmethod
    def health(self) -> str:
        """One line on whether the backend answers. Raises `ProviderError` if not."""

    def free(self) -> None:
        """Release whatever the backend holds on the GPU. A no-op by default."""

    def interrupt(self) -> None:
        """Abandon a render in flight, when a run is stopped. A no-op by default."""
