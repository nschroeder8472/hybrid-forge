"""FreeToken — an OpenAI-compatible engine that serves one model at a time.

The wire protocol is ordinary OpenAI, so everything about building the request
and reading the answer is inherited. What is not ordinary is which model
answers it.

FreeToken splits into a daemon that owns the GPU and a *serve* that holds one
checkpoint. The daemon's `/engine/switch` unloads what is there and loads
something else; the serve's `/v1/chat/completions` then answers from whatever
that is. The `model` field of the request has no part in it:

    model="Qwen3.8-27B-NVFP4"      -> 200, served-as=Qwen3.8-27B-NVFP4
    model="Gemma-4-26B-A4B-NVFP4"  -> 200, served-as=Gemma-4-26B-A4B-NVFP4
    model="totally-made-up"        -> 200, served-as=totally-made-up

All three were answered by the one loaded engine, which echoed the name back.
That is the whole reason this module exists. Ollama returns 404 for a model it
does not have and loads on demand for one it does, so a config naming three
models gets three models. Point the plain `openai` kind at FreeToken and a
config naming three models gets one model and three labels, with the run
recording the labels — no error anywhere, and every artifact wrong about which
model wrote what.

So this provider asks the daemon what is loaded, switches when it is not the
right thing, waits for the new serve to be genuinely ready, and *verifies* what
came up before sending anything. A shim in front of the port could do the
switching; it could not make forge's own record of which model answered true.

Cost, measured on an RTX 5090 between a 27B dense and a 26B MoE: the switch
call returns in about a second and the new engine serves about twenty seconds
later. That is the price of a role alternation, and it is why `switchSeconds`
exists rather than a hidden constant — a slower disk or a larger checkpoint
moves it, and a run that mysteriously stalls for a minute a cycle is worse than
one that says what it is waiting for.
"""

from __future__ import annotations

import time
from typing import Any

from ._http import get_json, post_json
from .base import (
    DERIVE_TIMEOUT,
    Completion,
    Message,
    ProviderError,
    ProviderUnreachable,
)
from .openai_compat import OpenAICompatProvider

# How long to wait for a switched-to engine to start answering. Twenty seconds
# is typical; the ceiling is for a cold page cache and a checkpoint several
# times the size.
DEFAULT_SWITCH_SECONDS = 300
# Where the daemon puts a serve when nothing says otherwise.
DEFAULT_PORT = 1919
# Between readiness probes. The engine answers `/health` and `/v1/models` while
# it is still loading weights, so the probe that counts is a real completion —
# see `_serving`. Two seconds keeps that cheap.
_POLL_SECONDS = 2.0


# What the daemon says when it could not drain the serve it was about to
# replace: `{"error": "prepare-stop returned HTTP 503",
# "code": "accounting_prepare_failed", "enginePreserved": true}`. Matched on
# these markers rather than on the status code, because a 503 from a daemon
# that is not running means something else entirely and forcing would not help.
_DRAIN_MARKERS = ("accounting_prepare_failed", "prepare-stop", "prepare_stop")


def _drain_failed(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in _DRAIN_MARKERS)


class FreeTokenProvider(OpenAICompatProvider):
    kind = "freetoken"

    def __init__(self, name: str, config: dict[str, Any]):
        super().__init__(name, config)
        self.daemon_url = str(
            config.get("daemonUrl", "http://127.0.0.1:1900")
        ).rstrip("/")
        # Port and `baseUrl` are two spellings of one thing, and a config may
        # give either. Read from the declared url rather than from
        # `self.base_url`, which the OpenAI base has already defaulted to
        # Ollama's 11434 — a FreeToken block that named neither would otherwise
        # inherit a port belonging to a different server entirely.
        declared = str(config.get("baseUrl") or "")
        self.port = int(config.get("port", 0) or 0)
        if not self.port:
            tail = declared.rsplit(":", 1)[-1].split("/")[0] if declared else ""
            self.port = int(tail) if tail.isdigit() else DEFAULT_PORT
        if not declared:
            self.base_url = f"http://127.0.0.1:{self.port}/v1"
        # What the daemon needs to load this checkpoint, which is NOT what
        # `/v1/models` calls it. The daemon hands its `model` to
        # `AutoConfig.from_pretrained`, so a bare name is looked up on
        # HuggingFace and the engine exits 1 before it ever binds a port —
        # while `/engine/status` still reports `running: true` for a moment,
        # which is how it reads as a working engine that refuses connections.
        self.model_path = str(config.get("modelPath", "") or self.model)
        # Passed through to the serve on switch: `--moe-backend offload`,
        # `--memory-ratio`, `--max-seq-len-override`. Per model, because the
        # two halves of a pair are rarely given the same budget.
        self.engine_args = list(config.get("engineArgs", []) or [])
        self.switch_seconds = int(
            config.get("switchSeconds", DEFAULT_SWITCH_SECONDS)
        )

    # -- the daemon ----------------------------------------------------

    def _status(self) -> dict[str, Any]:
        try:
            return get_json(
                f"{self.daemon_url}/engine/status", headers={}, timeout=15
            )
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 - reported as unreachable below
            raise ProviderUnreachable(
                f"FreeToken daemon at {self.daemon_url} did not answer: {exc}"
            ) from exc

    @staticmethod
    def _loaded_name(status: dict[str, Any]) -> str:
        """The basename of what the daemon says is loaded.

        `/engine/status` reports the path it was given and `/v1/models` reports
        the basename of it, so one of the two has to be reduced before they can
        be compared at all.
        """
        raw = str(status.get("model") or "")
        for separator in ("\\", "/"):
            raw = raw.rsplit(separator, 1)[-1]
        return raw

    def _serving(self) -> str:
        """The model actually answering, or "" if the engine is not up yet.

        A completion, not a status flag. `running: true`, `/health` and
        `/v1/models` all answer while the weights are still loading, and
        `/v1/chat/completions` returns 503 for about twenty seconds after them
        — so every cheaper probe reports ready before anything can be sent.
        """
        try:
            served = get_json(
                f"http://127.0.0.1:{self.port}/v1/models", headers={}, timeout=10
            )["data"][0]["id"]
        except Exception:  # noqa: BLE001 - not up yet is the ordinary case
            return ""
        try:
            post_json(
                f"http://127.0.0.1:{self.port}/v1/chat/completions",
                {
                    "model": served,
                    "messages": [{"role": "user", "content": "ok"}],
                    "max_tokens": 1,
                    "temperature": 0.0,
                },
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
        except Exception:  # noqa: BLE001 - 503 while loading
            return ""
        return served

    def _ask(self, *, force: bool) -> None:
        body: dict[str, Any] = {"model": self.model_path, "port": self.port}
        if self.engine_args:
            body["args"] = self.engine_args
        if force:
            body["force"] = True
        try:
            post_json(
                f"{self.daemon_url}/engine/switch",
                body,
                headers={"Content-Type": "application/json"},
                timeout=120,
            )
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ProviderUnreachable(
                f"FreeToken daemon refused to load {self.model_path!r}: {exc}"
            ) from exc

    def _switch(self) -> None:
        try:
            self._ask(force=False)
        except ProviderError as exc:
            if not _drain_failed(str(exc)):
                raise
            # The daemon drains the running serve before replacing it, and a
            # serve that is mid-generation will not drain. That is the ordinary
            # case here rather than an exotic one: the engine this is replacing
            # was answering a moment ago, and a model that reasons past its
            # output budget is still answering for a long time afterwards. One
            # run lost ten builds and two records to it, all of them reported
            # as the build step failing.
            #
            # Forcing is safe precisely because the drain failed: the daemon
            # says `enginePreserved`, so nothing has been torn down and the
            # only thing lost is the usage receipt for the model being
            # replaced. A receipt is worth less than the attempt.
            self._ask(force=True)

        deadline = time.time() + self.switch_seconds
        while time.time() < deadline:
            serving = self._serving()
            if serving == self.model:
                return
            if serving:
                # Something else came up. Reported rather than used: this
                # provider exists because the endpoint will happily answer as
                # whatever name it is sent, and answering from the wrong
                # checkpoint under the right label is the failure it is here to
                # prevent.
                raise ProviderError(
                    f"asked FreeToken for {self.model!r} and "
                    f"{serving!r} is serving on port {self.port}. The engine "
                    f"echoes whatever model name it is sent, so this would "
                    f"otherwise have been recorded as {self.model!r}."
                )
            status = self._status()
            if not status.get("running") and status.get("lastExitCode"):
                raise ProviderUnreachable(
                    f"FreeToken engine for {self.model!r} exited "
                    f"({status.get('lastExitReason') or 'no reason given'}); "
                    f"see the daemon's serve log. A `modelPath` that is not a "
                    f"directory on disk fails exactly this way."
                )
            time.sleep(_POLL_SECONDS)
        raise ProviderUnreachable(
            f"FreeToken did not serve {self.model!r} within "
            f"{self.switch_seconds}s of being asked to load it."
        )

    def _ensure_loaded(self) -> None:
        status = self._status()
        if status.get("running") and self._loaded_name(status) == self.model:
            return
        self._switch()

    # -- the call ------------------------------------------------------

    def complete(
        self,
        messages: list[Message],
        *,
        max_tokens: int,
        temperature: float = 0.2,
        timeout: int = DERIVE_TIMEOUT,
    ) -> Completion:
        # Before every call, not once at startup. The loop alternates roles and
        # each role is its own provider instance, so what is loaded is decided
        # by whichever one went last — and on a resumed run, by whatever the
        # last run left behind.
        self._ensure_loaded()
        return super().complete(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
        )
