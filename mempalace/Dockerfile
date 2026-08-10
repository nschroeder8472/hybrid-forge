# MemPalace container.
#
# MemPalace publishes its own image, so there is nothing to build from source:
#
#   docker pull ghcr.io/mempalace/mempalace:latest
#
# This file exists only to pin that image and declare the data volume. Verified
# against MemPalace 3.6.0.
#
# The image's default CMD is `mcp` — the MCP server over **stdio**, which is
# what the daemon drives through `memory.command`. There is no CMD or EXPOSE
# here because that default is already right for the common case: a palace on
# the same machine as the daemon needs no port and no listener.
#
# For a palace on a different machine, MemPalace serves MCP over Streamable
# HTTP itself — `mempalace serve --host <addr> --port 8765`, with bearer-token
# auth and optional TLS. No MCP proxy is involved. See docs/SETUP.md §1.4.

FROM ghcr.io/mempalace/mempalace:latest

# The palace, its config, and the cached embedding model. HOME is /data in this
# image, so a named volume here keeps all three across `docker compose down`.
VOLUME ["/data"]
