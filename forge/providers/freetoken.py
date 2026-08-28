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


# The pools a `cache` block may name, mapped to the rebuild endpoint's own
# field and to the limits key that says whether this model has the pool at all.
#
# Config counts tokens and slots, because that is what the operator is shown
# and what a model's documentation talks about. The endpoint counts *pages* for
# the two token pools, so those are converted here against the page sizes the
# engine reports — never assumed to be 1, which they are on radix-SWA and are
# not on every backend.
#
# Nothing here is per-model. Which pools exist, and how large each may be, are
# read off `/v1/cache/status`, so a checkpoint with a mamba pool and no window
# pool configures exactly as well as one with the reverse. That is the whole
# point: the operator brings the model, and the engine says what it has.
_POOLS = {
    # config key -> (rebuild field, limits key, page-size field or "")
    "kv": ("num_pages", "kv_tokens", "page_size"),
    "swa": ("num_swa_pages", "swa_tokens", "swa_page_size"),
    "moe": ("moe_cache_size", "moe_experts", ""),
    "mamba": ("num_mamba_slots", "mamba_slots", ""),
}
# A rebuild stops serving while it runs, and a large one is not instant.
DEFAULT_REBUILD_SECONDS = 300


def _pages(tokens: int, page_size: int) -> int:
    """Tokens up to whole pages. A partial page still has to be allocated."""
    size = max(1, int(page_size or 1))
    return -(-int(tokens) // size)


def _merge_body(gear: dict[str, Any], declared: dict[str, Any]) -> dict[str, Any]:
    """The gear's fields under the operator's, one level into the template kwargs.

    A plain `{**gear, **declared}` would let an operator who sets one template
    variable drop every variable the gear selects, which is the opposite of
    what writing one of them means.
    """
    merged: dict[str, Any] = {**gear, **declared}
    key = "chat_template_kwargs"
    if isinstance(gear.get(key), dict) and isinstance(declared.get(key), dict):
        merged[key] = {**gear[key], **declared[key]}
    return merged


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
        # Cache geometry for this role, in tokens and slots. Per model block
        # rather than per engine, because the two are not the same thing: a
        # reviewer reading one diff and an executor holding six reference files
        # want different budgets out of the same checkpoint, and they are
        # separate blocks precisely so they can differ.
        #
        # This is not cosmetic. A serve started by `/engine/switch` comes up at
        # the engine's own default sizing; anything applied by hand belongs to
        # the process it was applied to and dies with it. So a run that swaps
        # models lands on defaults every swap, whatever the desktop app was
        # last told. On one checkpoint the difference was 115,729 KV tokens
        # against 759,808 — and the small one produced a model that reasoned
        # past a 65,536-token budget without ever answering.
        self.cache = {
            key: int(value)
            for key, value in (config.get("cache", {}) or {}).items()
            if value is not None
        }
        self.rebuild_seconds = int(
            config.get("rebuildSeconds", DEFAULT_REBUILD_SECONDS)
        )
        # Which way of thinking to ask for, named the way this checkpoint names
        # it. `/v1/cache/status` reports the gears it actually has, so this is
        # checked against them rather than passed through and hoped for — see
        # `_reasoning_body`.
        self.reasoning_gear = str(config.get("reasoning", "") or "")
        # The hand-written body, kept apart from the gear resolved off the
        # loaded checkpoint: the gear is recomputed per call and must not
        # accumulate onto what the operator wrote.
        self._declared_extra_body = dict(self.extra_body)
        self._geometry: dict[str, Any] | None = None

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

    # -- what this checkpoint has --------------------------------------

    def geometry(self, *, refresh: bool = False) -> dict[str, Any]:
        """The serve's cache geometry, or an empty dict when it will not say.

        Carries which pools this model has, their limits, the page sizes its
        token counts convert against, and the reasoning gears it understands.
        Everything model-specific this provider needs comes from here, which is
        why none of it is written down per model anywhere in forge.

        Cached per switch: the geometry belongs to the loaded checkpoint and
        changes when that does.
        """
        if self._geometry is not None and not refresh:
            return self._geometry
        try:
            doc = get_json(
                f"http://127.0.0.1:{self.port}/v1/cache/status",
                headers={},
                timeout=20,
            )
        except Exception:  # noqa: BLE001 - an older serve may not have it
            self._geometry = {}
            return self._geometry
        self._geometry = dict((doc or {}).get("geometry") or {})
        return self._geometry

    def _reasoning_body(self) -> dict[str, Any]:
        """The request fields that select `reasoning` on this checkpoint.

        `reasoning_effort` is a poor instrument for this and the failure is
        silent: on one checkpoint the engine logged `reasoning_effort medium is
        not supported by this checkpoint; using the template default` and then
        answered anyway, so a run spent two roles' worth of calls believing it
        had asked for something. That checkpoint has two gears, off and on, and
        reports them here along with the fields that select each.

        So the gear is named and the engine's own kwargs are sent. A name the
        model does not have is refused rather than approximated: the whole
        reason this exists is that approximating it looked like it worked.
        """
        if not self.reasoning_gear:
            return {}
        reasoning = self.geometry().get("reasoning") or {}
        gears = list(reasoning.get("gears") or [])
        kwargs = (reasoning.get("kwargs") or {}).get(self.reasoning_gear)
        if isinstance(kwargs, dict):
            # Under `chat_template_kwargs`, because that is what they are:
            # variables the checkpoint's jinja template reads, not request
            # fields. Sent at the top level they are silently dropped and the
            # engine uses its default gear -- measured, and it looks exactly
            # like a model that will not think.
            return {"chat_template_kwargs": dict(kwargs)}
        if gears:
            default = reasoning.get("default")
            raise ProviderError(
                f"{self.model!r} has no {self.reasoning_gear!r} reasoning "
                f"gear; it offers {', '.join(sorted(gears))}"
                + (f" (default {default!r})" if default else "")
                + "."
            )
        # No geometry to check against - an older serve, or one that does not
        # report gears. Send it as an effort and let the engine judge.
        return {"reasoning_effort": self.reasoning_gear}

    # -- cache geometry ------------------------------------------------

    def _targets(self, geometry: dict[str, Any]) -> dict[str, int]:
        """`cache` resolved into the rebuild endpoint's fields and units.

        Raises rather than clamps. A budget silently reduced to what fits is
        the same class of bug as a model silently ignoring `reasoning_effort`:
        the run goes on believing it asked for something it did not get.
        """
        limits = geometry.get("limits") or {}
        body: dict[str, int] = {}
        for key, wanted in self.cache.items():
            if key not in _POOLS:
                raise ProviderError(
                    f"unknown cache pool {key!r} for {self.name!r}; this "
                    f"provider understands {', '.join(sorted(_POOLS))}."
                )
            field, limit_key, page_field = _POOLS[key]
            bounds = limits.get(limit_key) or {}
            low, high = int(bounds.get("min", 0)), int(bounds.get("max", 0))
            unit = "tokens" if page_field else "slots"
            if high <= 0:
                has = sorted(
                    k for k, v in limits.items() if int((v or {}).get("max", 0)) > 0
                )
                raise ProviderError(
                    f"{self.model!r} has no {key!r} cache pool, so "
                    f"`cache.{key}` cannot be applied. This checkpoint has "
                    f"{', '.join(has) or 'none'}."
                )
            if not low <= wanted <= high:
                raise ProviderError(
                    f"cache.{key} is {wanted:,} {unit} for {self.model!r}, "
                    f"which allows {low:,}-{high:,}."
                )
            body[field] = (
                _pages(wanted, geometry.get(page_field, 1)) if page_field else wanted
            )
        return body

    @staticmethod
    def _already(geometry: dict[str, Any], targets: dict[str, int]) -> bool:
        """Whether the serve is already shaped this way.

        Worth checking because a rebuild stops serving while it runs, and the
        common case by far is a switch back to a model this provider has
        already shaped once.
        """
        return all(
            int(geometry.get(field, -1)) == value for field, value in targets.items()
        )

    def _apply_cache(self) -> None:
        """Shape the serve's cache the way this role's config asks.

        Only ever after a switch, and only while nothing is in flight: the
        endpoint is idle-only, and forge sends one call at a time per engine.
        """
        if not self.cache:
            return
        geometry = self.geometry(refresh=True)
        if not geometry:
            raise ProviderUnreachable(
                f"{self.name!r} configures a cache but the serve on port "
                f"{self.port} did not report /v1/cache/status, so there is no "
                f"way to know what it has or to shape it. Remove `cache`, or "
                f"upgrade FreeToken."
            )
        targets = self._targets(geometry)
        if self._already(geometry, targets):
            return
        try:
            post_json(
                f"http://127.0.0.1:{self.port}/v1/cache/rebuild",
                {**targets, "timeout": self.rebuild_seconds},
                headers={"Content-Type": "application/json"},
                timeout=self.rebuild_seconds + 60,
            )
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ProviderUnreachable(
                f"could not shape the cache for {self.model!r}: {exc}"
            ) from exc
        # Read back rather than trusted. A rebuild reports per-pool status and
        # a pool that will not fit the engine's budget is refused on its own,
        # which would otherwise leave the run on a geometry nobody chose.
        after = self.geometry(refresh=True)
        if not self._already(after, targets):
            got = ", ".join(f"{f}={after.get(f)}" for f in sorted(targets))
            raise ProviderUnreachable(
                f"asked for cache {targets} on {self.model!r} and the serve "
                f"reports {got}. The rebuild was refused, most likely for VRAM."
            )

    def _ensure_loaded(self) -> None:
        status = self._status()
        if status.get("running") and self._loaded_name(status) == self.model:
            return
        # Geometry belongs to the process being replaced, not to the model.
        self._geometry = None
        self._switch()
        # A fresh serve comes up at the engine's own default sizing whatever
        # was applied to its predecessor, so this is where a role's configured
        # shape is put back - before any work reaches it.
        self._apply_cache()

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
        # Resolved here rather than in `__init__`, because it is read off the
        # checkpoint that is loaded now. Operator's own fields win: `reasoning`
        # is a convenience over the gear table, not a lock on the request body.
        self.extra_body = _merge_body(
            self._reasoning_body(), self._declared_extra_body
        )
        return super().complete(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
        )

    def diagnostics(self) -> list[str]:
        """What `forge doctor` can see about this model that a probe cannot.

        The point of reporting geometry even when nothing is wrong is that a
        BYOM setup has no way to guess it. Which pools a checkpoint has, how
        large they may be, and what its reasoning gears are called are facts
        about the model, and until they are printed somewhere the operator is
        configuring blind.
        """
        notes = super().diagnostics()
        geometry = self.geometry()
        if not geometry:
            if self.cache or self.reasoning_gear:
                notes.append(
                    "this serve does not report /v1/cache/status, so `cache` "
                    "and `reasoning` cannot be checked against what the model "
                    "actually has."
                )
            return notes

        limits = geometry.get("limits") or {}
        pools = [
            f"{key} {int((limits.get(_POOLS[key][1]) or {}).get('min', 0)):,}"
            f"-{int((limits.get(_POOLS[key][1]) or {}).get('max', 0)):,}"
            for key in sorted(_POOLS)
            if int((limits.get(_POOLS[key][1]) or {}).get("max", 0)) > 0
        ]
        if pools:
            notes.append(f"cache pools available: {'; '.join(pools)}")

        reasoning = geometry.get("reasoning") or {}
        gears = list(reasoning.get("gears") or [])
        if gears:
            notes.append(
                f"reasoning gears: {', '.join(sorted(gears))} "
                f"(default {reasoning.get('default')!r})"
                + (
                    ""
                    if self.reasoning_gear or "reasoning_effort" in {
                        k.lower() for k in self._declared_extra_body
                    }
                    else " — nothing selects one, so the default applies."
                )
            )
        if self.reasoning_gear and gears and self.reasoning_gear not in gears:
            notes.append(
                f"reasoning is {self.reasoning_gear!r}, which this checkpoint "
                f"does not have; every call will be refused."
            )

        if self.cache:
            try:
                targets = self._targets(geometry)
            except ProviderError as exc:
                notes.append(str(exc))
            else:
                if not self._already(geometry, targets):
                    notes.append(
                        "cache is configured and the serve is not shaped that "
                        "way yet; it is applied on the next switch. A serve "
                        "always starts at the engine default, so anything "
                        "applied by hand elsewhere does not survive one."
                    )
        return notes

