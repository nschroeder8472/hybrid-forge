"""Clearing the card for a step that needs it to itself.

On one GPU, a llama checkpoint resident for the next role and a renderer
started by a project command compete for the same memory, and neither reports
the contest: the renderer falls back to system RAM and runs an order of
magnitude slower, or fails to allocate with an error about itself. So a step
the project marks in `loop.exclusiveGpu` is run with every llama checkpoint
unloaded first (docs/CREATIVE-TOOLS.md, *The GPU lease*).

Two measured facts shape it (phase 0, 2026-11-01):

- **Wait on the router's status, not on VRAM.** VRAM drops 0.2 s after
  `/models/unload`, while the router still reports `loaded` for another two
  seconds. A load asked for in that window is refused with `400 model is
  already running`. `LlamaCppProvider._await_unloaded` waits on the status,
  and this module uses it rather than looking at memory.
- **Nothing needs reloading after.** The router loads a checkpoint on the next
  call that names it, and the loop already waits for that, so the release
  half of the lease costs nothing on the llama side. It costs the reload —
  8.8 s for a 22.8 GB checkpoint — on the next model call, which is the price
  of the lease and why grouping leased work is worth doing.
"""

from __future__ import annotations

from typing import Any

from .providers import LlamaCppProvider

# The command kinds a project may lease the card for. `capture` is the one a
# renderer runs under; `image` joins it with the image provider. A kind
# outside this set is refused at `validate()` rather than silently never
# leased.
LEASABLE = ("capture",)


def llama_routers(config: Any) -> list[LlamaCppProvider]:
    """One provider per distinct llama.cpp router the config's models use."""
    seen: dict[str, LlamaCppProvider] = {}
    for name in sorted(config.models):
        try:
            provider = config.provider_for_model(name)
        except Exception:  # noqa: BLE001 - a model that cannot be built holds no VRAM
            continue
        if isinstance(provider, LlamaCppProvider):
            seen.setdefault(provider._router_url(), provider)
    return list(seen.values())


def clear_llama(config: Any) -> tuple[list[str], list[str]]:
    """Unload every resident llama checkpoint. Returns (unloaded, problems).

    Never raises. A router that cannot be reached holds nothing a lease could
    free, and a step that needs the card should still run — slower, or failing
    with its own error — rather than be refused over a server that is down.
    """
    unloaded: list[str] = []
    problems: list[str] = []
    for router in llama_routers(config):
        try:
            catalog = router.catalog(refresh=True)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{router._router_url()}: {exc}")
            continue
        for model, entry in sorted(catalog.items()):
            if router._status(entry) not in ("loaded", "loading"):
                continue
            try:
                router._unload(model)
                router._await_unloaded(model)
                unloaded.append(model)
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{model}: {exc}")
    return unloaded, problems
