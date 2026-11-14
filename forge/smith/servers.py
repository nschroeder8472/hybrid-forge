"""The image backend mold needs, started if it is not up.

The same ComfyUI that `forge go` starts for image work, adopted when it is
already running, and owned as `cli` so it outlives the call that needed it:
`forge comfy stop` ends it. llama-server is never started here; none of the
smith's tools uses a text model.
"""

from __future__ import annotations

from typing import Any

from ..run_servers import RunServers
from ..servers import OWNER_CLI


def image_servers(config: Any) -> RunServers | None:
    """Start ComfyUI when the machine profile manages one. Raises ServerError."""
    local = RunServers.for_config(config, images=True, owner=OWNER_CLI, llama=False)
    if local is not None:
        local.start()
    return local
