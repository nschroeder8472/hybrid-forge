"""llama.cpp's router server — one endpoint, several checkpoints, loaded on demand.

`llama-server --models-dir` (or `--models-preset`) starts in *router* mode: it
reads a catalogue of models, spawns a child server per model on an ephemeral
port, and proxies `/v1/chat/completions` to whichever one the request names.
That is the swapping mechanism, and it belongs to llama.cpp rather than to a
proxy in front of it.

The wire protocol is ordinary OpenAI, so everything about building the request
and reading the answer is inherited. Three things are not ordinary.

**The catalogue is not the config.** A models-dir entry is named after the
*directory* it was found in, so a checkpoint at `AIModels/nemotron-…-gguf/` is
called `nemotron-…-gguf` and nothing else. A config that names the file, the
HuggingFace repo, or the alias the operator has in their head is refused —
`{"error": {"code": 400, "message": "model 'x' not found"}}` — which is a good
failure, and this provider turns it into a better one by listing what the
router does have, at `forge doctor` time rather than mid-run.

That refusal is also why this module is small, and part of why it is the only
local backend forge carries. The one it replaced answered to *any* model name
and echoed it back, so a config naming three checkpoints got one checkpoint and
three labels — and every artifact, usage row and cost figure attributed work to
a model that had not written it. The router routes by id and 400s an id it does
not have, so forge's record of which model wrote what is true for free. What is
left is worth having anyway:

**A load is not instant and is not the call's fault.** Loading a 30B checkpoint
takes tens of seconds. With `--models-autoload` the first request after a swap
simply blocks for it, so a load that never finishes is reported as the
completion timing out — naming the endpoint, which was healthy the whole time.
So the load is asked for explicitly, waited for against `loadSeconds`, and a
model still not `loaded` at the deadline says so.

**Several models stay resident.** `--models-max` defaults to 4, which is right
on a box with the VRAM for it and fatal on one without: the router keeps the
previous role's checkpoint loaded and the next one has nowhere to go.
`exclusive` unloads everything else first, trading a reload per role
alternation for a ceiling of one checkpoint at a time.

**An unload is not instant either, and the router does not block for it.**
`/models/unload` answers once the child server has been asked to exit; the
slot it occupies is free once the child has actually exited. Claim it in
between and the load is refused with `500 model limit reached, try again
later` — which on this backend is an unreachable model, i.e. a role that
cannot vote and a delegation that cannot be attempted. So the eviction is
waited out against the catalogue before the load is asked for, and a refusal
that arrives anyway is retried rather than raised. Measured on the same
b10666 build: an unload lands in 0-2.3s and the window is narrow enough that
54 alternations in one run crossed it untouched before one did not.

Measured on build b10666, a 30B A3B MoE at Q4_K_M: `POST /models/load` returns
immediately, status reaches `loaded` in 10-20s, and the model then generates at
about 16 tok/s. That last number is why `tokensPerSecond` is worth setting on
this backend — the 30 tok/s default derives a timeout too short to ever reach a
large `maxOutputTokens`.
"""

from __future__ import annotations

import time
from typing import Any

from ._http import get_json, post_json
from .base import (
    DERIVE_TIMEOUT,
    Capabilities,
    Completion,
    Message,
    ProviderBadResponse,
    ProviderError,
    ProviderUnreachable,
)
from .openai_compat import OpenAICompatProvider

# How long to wait for a checkpoint to become servable. Twenty seconds is
# typical for a 30B at Q4; the ceiling is for a cold page cache and a much
# larger file.
DEFAULT_LOAD_SECONDS = 300
# Between status probes while a load is in flight.
_POLL_SECONDS = 2.0
# Between probes while waiting for an eviction to land. Finer than the load
# poll because it is paid on every role alternation and the thing it waits for
# is short: measured on a 30B at Q4, an unload lands in 0-2.3s.
_EVICT_POLL_SECONDS = 0.5
# How long to wait for the router to free a `--models-max` slot. Twenty-five
# times the measured unload, because the only thing that legitimately holds a
# slot longer is a request still in flight on the child being evicted.
_SLOT_SECONDS = 60.0
# What `/v1/models` reports under `status.value`.
_LOADED = "loaded"
_LOADING = "loading"
# The router's refusal when every `--models-max` slot is still spoken for. Not
# a fault, and the message says so: the condition clears on its own.
_LIMIT_REACHED = "model limit reached"
# The flags a preset uses to pin a child server's context window. The router
# reports the argv it will spawn, which is the only place a per-model window is
# visible — `/props` belongs to the router and answers `n_ctx: 0`.
_CTX_FLAGS = ("-c", "--ctx-size")
# Every spelling that turns the multimodal projector off. A preset's
# `no-mmproj = true` arrives as `--no-mmproj-auto`, not as the `--no-mmproj`
# alias the help text advertises, so matching only the advertised one reports a
# projector on a child that has none.
_NO_PROJECTOR = ("--no-mmproj", "--no-mmproj-auto")


class LlamaCppProvider(OpenAICompatProvider):
    kind = "llamacpp"

    def __init__(self, name: str, config: dict[str, Any]):
        super().__init__(name, config)
        # `baseUrl` carries the `/v1` the completions live under; the router's
        # management endpoints — /models/load, /models/unload, /props — sit one
        # level up from it. The inherited default is OpenAI's own endpoint,
        # which is a cloud address and belongs to a different adapter.
        if not config.get("baseUrl"):
            self.base_url = "http://127.0.0.1:8080/v1"
        self.load_seconds = int(config.get("loadSeconds", DEFAULT_LOAD_SECONDS))
        # Whether this role insists on being the only checkpoint resident. Off
        # by default: the router's own `--models-max` is the right control on a
        # box with the VRAM, and evicting a model the next role is about to ask
        # for again is pure reload cost.
        self.exclusive = bool(config.get("exclusive", False))
        self._catalog_cache: dict[str, Any] | None = None

    # -- the router ----------------------------------------------------

    def _router_url(self) -> str:
        """The router root. `/v1` is the OpenAI prefix; management is above it."""
        if self.base_url.endswith("/v1"):
            return self.base_url[: -len("/v1")]
        return self.base_url

    def catalog(self, *, refresh: bool = False) -> dict[str, Any]:
        """Every model the router knows, by id, with its status and argv.

        Refreshed rather than held: what is resident changes under us whenever
        another role goes, and finding that out is the whole point of reading
        it.
        """
        if self._catalog_cache is not None and not refresh:
            return self._catalog_cache
        try:
            doc = get_json(
                f"{self._router_url()}/v1/models",
                headers=self._headers(),
                timeout=20,
            )
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 - reported as unreachable below
            raise ProviderUnreachable(
                f"llama.cpp router at {self._router_url()} did not answer: {exc}"
            ) from exc
        self._catalog_cache = {
            str(entry.get("id")): entry
            for entry in (doc or {}).get("data", [])
            if entry.get("id")
        }
        return self._catalog_cache

    def _entry(self, catalog: dict[str, Any]) -> dict[str, Any]:
        """This model's catalogue entry, or an error naming what does exist.

        The single likeliest misconfiguration on this backend, because a
        models-dir entry is named after its directory and nothing warns you
        that the name you invented is not that.
        """
        entry = catalog.get(self.model)
        if entry is not None:
            return entry
        have = ", ".join(sorted(catalog)) or "nothing"
        raise ProviderError(
            f"the llama.cpp router at {self._router_url()} has no model "
            f"{self.model!r}; it serves {have}. A --models-dir entry is named "
            f"after the directory holding the .gguf, not after the file, the "
            f"HuggingFace repo, or an alias — use one of the names above, or "
            f"give the router a --models-preset that declares {self.model!r}."
        )

    @staticmethod
    def _status(entry: dict[str, Any]) -> str:
        return str((entry.get("status") or {}).get("value") or "")

    def _load(self, model: str) -> None:
        """Ask for a checkpoint, waiting out a router that has no slot yet.

        `500 model limit reached, try again later` is not a fault. It is the
        router saying that an eviction it has already accepted has not finished
        — `/models/unload` answers as soon as the child has been asked to go,
        not once it has gone, and until it has, its `--models-max` slot is
        still spoken for. The message names the remedy, so take it, bounded.
        Anything else is raised as it stands.
        """
        deadline = time.time() + _SLOT_SECONDS
        while True:
            try:
                post_json(
                    f"{self._router_url()}/models/load",
                    {"model": model},
                    headers={"Content-Type": "application/json", **self._headers()},
                    timeout=120,
                )
                return
            except ProviderError as exc:
                if _LIMIT_REACHED not in str(exc).lower():
                    raise
                if time.time() >= deadline:
                    raise ProviderUnreachable(
                        f"the llama.cpp router had no slot for {model!r} within "
                        f"{_SLOT_SECONDS:g}s; it is still answering "
                        f"{_LIMIT_REACHED!r}. Either a request is still in "
                        f"flight on the checkpoint being evicted, or "
                        f"--models-max is below the number of checkpoints the "
                        f"roles want resident at once."
                    ) from exc
                time.sleep(_EVICT_POLL_SECONDS)
            except Exception as exc:  # noqa: BLE001
                raise ProviderUnreachable(
                    f"llama.cpp router refused to load {model!r}: {exc}"
                ) from exc

    def _unload(self, model: str) -> None:
        """Evict a checkpoint, tolerating one that was already gone.

        The router answers `400 model is not running` for that, which here is
        an ordinary race rather than a fault: between reading the catalogue and
        acting on it, another role may have evicted the same model.
        """
        try:
            post_json(
                f"{self._router_url()}/models/unload",
                {"model": model},
                headers={"Content-Type": "application/json", **self._headers()},
                timeout=120,
            )
        except ProviderBadResponse as exc:
            if "not running" not in str(exc).lower():
                raise
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ProviderUnreachable(
                f"llama.cpp router refused to unload {model!r}: {exc}"
            ) from exc

    def _await_unloaded(self, model: str) -> None:
        """Wait for an evicted checkpoint to actually leave the catalogue.

        The router frees the `--models-max` slot when the child exits, not when
        it accepts the unload, and the catalogue is the only place that is
        observable. Skipping this wait is a coin flip: 54 alternations in one
        measured run crossed the window without incident, and then a load
        landed inside it and came back `500 model limit reached`. On this
        backend that reaches the loop as an unreachable model, which spends a
        ticket's whole attempt budget in about a second.
        """
        deadline = time.time() + _SLOT_SECONDS
        while True:
            entry = self.catalog(refresh=True).get(model)
            if entry is None or self._status(entry) not in (_LOADED, _LOADING):
                return
            if time.time() >= deadline:
                raise ProviderUnreachable(
                    f"the llama.cpp router still had {model!r} resident "
                    f"{_SLOT_SECONDS:g}s after accepting an unload for it, so "
                    f"there is no slot for {self.model!r}. A request still in "
                    f"flight on that checkpoint will hold it open until the "
                    f"request finishes; the router's log says which."
                )
            time.sleep(_EVICT_POLL_SECONDS)

    def _evict_others(self, catalog: dict[str, Any]) -> None:
        """Ask every other resident checkpoint to go, then wait for it to.

        Asked in one pass and waited for in another, so evicting three
        checkpoints costs one wait rather than three.
        """
        evicted = [
            model
            for model, entry in catalog.items()
            if model != self.model and self._status(entry) in (_LOADED, _LOADING)
        ]
        for model in evicted:
            self._unload(model)
        for model in evicted:
            self._await_unloaded(model)

    def _ensure_loaded(self) -> None:
        catalog = self.catalog(refresh=True)
        self._entry(catalog)
        if self.exclusive:
            self._evict_others(catalog)
            # Re-read: the eviction wait has already refreshed the catalogue
            # under us, and this model's own status may have moved with it.
            catalog = self.catalog(refresh=True)
        entry = self._entry(catalog)
        if self._status(entry) == _LOADED:
            return
        if self._status(entry) != _LOADING:
            self._load(self.model)

        deadline = time.time() + self.load_seconds
        while time.time() < deadline:
            status = self._status(self._entry(self.catalog(refresh=True)))
            if status == _LOADED:
                return
            if status and status != _LOADING:
                # The router publishes no exit reason — a child that dies
                # simply reverts to `unloaded` — so the log is the only place
                # the cause exists. Out of VRAM is by far the commonest, and it
                # is worth naming: with `--models-max` above 1 the checkpoint
                # that has the memory is usually the previous role's, still
                # resident and invisible from here.
                raise ProviderUnreachable(
                    f"the llama.cpp router put {self.model!r} back in state "
                    f"{status!r} instead of loading it, which means its child "
                    f"server exited. The router's log carries the reason; the "
                    f"usual one is that another checkpoint still holds the VRAM "
                    f"(`ErrorOutOfDeviceMemory`), which `exclusive` prevents."
                )
            time.sleep(_POLL_SECONDS)
        raise ProviderUnreachable(
            f"the llama.cpp router did not load {self.model!r} within "
            f"{self.load_seconds}s. Raise `loadSeconds` if the checkpoint is "
            f"large or the page cache is cold; a child server that exits "
            f"instead of binding reports its reason in the router's log."
        )

    # -- what this checkpoint has --------------------------------------

    @staticmethod
    def _ctx_from_args(entry: dict[str, Any]) -> int:
        """The `-c` the router will spawn this model's child server with.

        The router's own `/props` reports `n_ctx: 0` — it holds no model — and
        a child server's port is ephemeral, so the argv the catalogue publishes
        is the only place a per-model window is visible without loading it.

        Absent means the child takes llama.cpp's own default, which is *not*
        the checkpoint's trained maximum. Believing the trained maximum is the
        failure the budget gate exists to prevent: it approves a prompt the
        server then truncates from the front, dropping the system prompt and
        the spec, and what comes back reads as a weak model rather than a
        truncated request.
        """
        args = [str(a) for a in (entry.get("status") or {}).get("args") or []]
        for flag in _CTX_FLAGS:
            if flag in args:
                index = args.index(flag)
                if index + 1 < len(args):
                    try:
                        return int(args[index + 1])
                    except ValueError:
                        return 0
        return 0

    def capabilities(self) -> Capabilities:
        if self._caps is not None:
            return self._caps
        caps = Capabilities(
            context_window=int(self.config.get("contextWindow", 0) or 0),
            max_output_tokens=int(self.config.get("maxOutputTokens", 4096)),
            supports_temperature=bool(self.config.get("supportsTemperature", True)),
            # A property of the checkpoint, not of the adapter: a GGUF with a
            # projector beside it can see and one without cannot. `multimodal`
            # is the same key `presets` reads to decide whether to write
            # `mmproj-auto = false`, so a model declared blind here is one the
            # router was told not to load a projector for.
            supports_images=bool(self.config.get("multimodal", False)),
        )
        if not caps.context_window:
            # The preset's own `-c`, when it pins one. Never the trained
            # maximum out of the GGUF: that describes the model, not the window
            # the server allocated.
            try:
                pinned = self._ctx_from_args(self._entry(self.catalog()))
            except ProviderError:
                # A router that is down, or does not have this model yet. Fall
                # back without caching: caching it would outlive the outage,
                # and the budget gate would plan the rest of the run against
                # 8192 and report every ticket as too large for a model that
                # is fine.
                caps.context_window = 8192
                return caps
            # The router answered and the preset pins nothing — a stable fact
            # about the preset, so this one is worth keeping.
            caps.context_window = pinned or 8192
        self._caps = caps
        return caps

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
        # each role is its own provider instance, so what is resident is decided
        # by whichever one went last — and on a resumed run, by whatever the
        # previous run left behind.
        self._ensure_loaded()
        return super().complete(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
        )

    # -- what doctor can see -------------------------------------------

    def _props(self) -> dict[str, Any]:
        try:
            return get_json(
                f"{self._router_url()}/props", headers=self._headers(), timeout=15
            )
        except Exception:  # noqa: BLE001 - absence is reported by the caller
            return {}

    def diagnostics(self) -> list[str]:
        """What `forge doctor` can see here that a probe cannot.

        Every one of these answers a healthy probe and still costs a run.
        """
        notes = super().diagnostics()

        props = self._props()
        if props and str(props.get("role") or "") != "router":
            notes.append(
                f"the server at {self._router_url()} is serving a single model "
                f"rather than running in router mode, so it cannot swap. Start "
                f"it with --models-dir or --models-preset, or use kind `openai` "
                f"and accept one model per port."
            )
            return notes

        try:
            catalog = self.catalog(refresh=True)
            entry = self._entry(catalog)
        except ProviderError as exc:
            notes.append(str(exc))
            return notes

        resident = sorted(
            model for model, e in catalog.items() if self._status(e) == _LOADED
        )
        limit = int(props.get("max_instances", 0) or 0)
        if not self.exclusive and limit != 1:
            held = f" ({', '.join(resident)})" if resident else ""
            notes.append(
                f"the router keeps up to {limit or 'unlimited'} models resident "
                f"(--models-max) and currently holds {len(resident)}{held}. On a "
                f"GPU without room for all of them a load fails or spills to "
                f"host memory; set `exclusive` on this model to evict the others "
                f"first, or start the router with --models-max 1."
            )

        pinned = self._ctx_from_args(entry)
        configured = self.capabilities().context_window
        if not pinned:
            notes.append(
                f"the router's preset for {self.model!r} pins no context size, "
                f"so its child server starts at llama.cpp's default rather than "
                f"the checkpoint's trained maximum. contextWindow is "
                f"{configured:,}; if that is larger than the default, the budget "
                f"gate will approve prompts the server truncates from the front "
                f"— losing the system prompt and the spec. Give the router a "
                f"--models-preset that sets `-c`."
            )
        elif configured > pinned:
            notes.append(
                f"contextWindow is {configured:,} but the router starts "
                f"{self.model!r} with -c {pinned:,}. The budget gate would plan "
                f"against a window {configured / pinned:.1f}x larger than the "
                f"server allocates, and the overflow is truncated from the front. "
                f"Lower contextWindow to {pinned:,}, or raise the preset's -c."
            )

        args = [str(a) for a in (entry.get("status") or {}).get("args") or []]
        # Two ways to end up holding a projector, and only one of them is
        # visible as a flag. A --models-dir entry beside an `mmproj-*.gguf`
        # gets an explicit `--mmproj`; an `hf-repo` entry pulls one inside the
        # child if the repo publishes it, so the argv says nothing and the
        # router's log is the only place it appears. `--no-mmproj` covers both.
        projector = "--mmproj" in args
        implicit = any(a in ("--hf-repo", "-hf", "-hfr") for a in args)
        # `no-mmproj = true` in a preset reaches the child as the canonical
        # `--no-mmproj-auto`; `--no-mmproj` is the alias an operator types.
        disabled = any(a in _NO_PROJECTOR for a in args)
        if (projector or implicit) and not disabled:
            how = (
                "loads a multimodal projector (--mmproj)"
                if projector
                else "is pulled with -hf, which downloads and loads a "
                "multimodal projector too whenever the repo publishes one"
            )
            notes.append(
                f"{self.model!r} {how}, which costs VRAM no text-only role uses. "
                f"Add `no-mmproj = true` to its --models-preset entry."
            )
        return notes
