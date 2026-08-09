# MemPalace container.
#
# VERIFY BEFORE USE: MemPalace ships no official image as far as this project
# knows, and its MCP transport surface has changed across versions. The install
# command and the serve invocation below are the two lines most likely to be
# wrong for your installed version — check the current MemPalace docs and adjust.
#
# Specifically confirm: does your version expose an HTTP/SSE MCP transport, or
# only stdio? If stdio only, this container needs an MCP proxy in front of it
# (add it as a second service in docker-compose.yml) — a stdio server in a
# container is not reachable from your Mac.

FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl \
    && rm -rf /var/lib/apt/lists/*

# VERIFY: package name and install method.
RUN pip install --no-cache-dir mempalace

ENV MEMPALACE_PALACE_PATH=/data/palace \
    MEMPALACE_HOST=127.0.0.1 \
    MEMPALACE_PORT=8787

VOLUME ["/data"]

# VERIFY: subcommand and flags for serving MCP over the network.
CMD ["sh", "-c", "mempalace serve --host ${MEMPALACE_HOST} --port ${MEMPALACE_PORT} --palace ${MEMPALACE_PALACE_PATH}"]
