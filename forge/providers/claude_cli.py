"""Headless Claude Code CLI (`claude -p`) as a provider.

This is what lets the daemon use a Claude subscription rather than an API key:
it drives the same CLI the user already logs into, so planning and review run
on their existing plan with no second credential.

It is also the reason the loop needs a budget gate at all. Subscription plans
enforce a rolling usage window, and when it is exhausted the CLI does not
return a 429 with a header — it prints a sentence. Parsing that sentence into
a reset timestamp is the difference between a run that parks itself for an
hour and wakes up, and a run that dies at 2am.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta
from typing import Any

from .base import (
    DERIVE_TIMEOUT,
    Capabilities,
    Completion,
    Message,
    Provider,
    ProviderBadResponse,
    ProviderUnreachable,
    RateLimited,
    Usage,
    split_system,
)

# The CLI's limit message has changed wording across releases, so match the
# stable part ("limit reached") rather than a full sentence.
#
# The spend-limit forms are deliberately separate from the rate-limit ones. A
# spend limit exits non-zero with a JSON body whose usage counters are all
# zero, which reads exactly like an ordinary step failure — so before this was
# matched, the loop spent its whole retry budget re-running a ticket against a
# wall that only a billing change can move.
_LIMIT_PATTERN = re.compile(
    r"("
    r"usage limit reached|rate limit(?:ed)?|limit will reset|out of (?:usage|credits)"
    r"|(?:monthly|weekly|daily|spend|spending|credit|balance|quota) limit"
    r"|limit exceeded|insufficient (?:credit|balance|funds)"
    r")",
    re.IGNORECASE,
)

# A spend limit does not lift on a clock the message tells us about — it needs
# a human to raise the cap or a new billing period. Retrying on the usual
# short cadence just burns attempts, so these park far longer.
_SPEND_LIMIT_PATTERN = re.compile(
    r"("
    r"(?:monthly|weekly|daily|spend|spending|credit|balance) limit"
    r"|insufficient (?:credit|balance|funds)"
    r")",
    re.IGNORECASE,
)
_SPEND_LIMIT_RETRY_SECONDS = 3600.0

# Reset-time forms seen in practice: a unix timestamp, an ISO instant, or a
# human clock time like "3pm" / "3:30 PM (UTC)".
_RESET_EPOCH = re.compile(r"reset(?:s|ting)?\s+at\s+(\d{10,13})", re.IGNORECASE)
_RESET_ISO = re.compile(
    r"reset(?:s|ting)?\s+at\s+(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?)", re.IGNORECASE
)
_RESET_CLOCK = re.compile(
    r"reset(?:s|ting)?\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)", re.IGNORECASE
)


def parse_reset_time(text: str, *, now: float | None = None) -> float | None:
    """Extract a reset timestamp from a CLI limit message.

    Returns None when the message carries no time — the caller then falls back
    to a conservative wait rather than hammering a limit that has not lifted.
    """
    now = time.time() if now is None else now

    match = _RESET_EPOCH.search(text)
    if match:
        value = int(match.group(1))
        # 13 digits is milliseconds.
        return value / 1000 if value > 10**12 else float(value)

    match = _RESET_ISO.search(text)
    if match:
        try:
            return datetime.fromisoformat(match.group(1).replace(" ", "T")).timestamp()
        except ValueError:
            pass

    match = _RESET_CLOCK.search(text)
    if match:
        hour = int(match.group(1)) % 12
        minute = int(match.group(2) or 0)
        if match.group(3).lower() == "pm":
            hour += 12
        current = datetime.fromtimestamp(now)
        target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target.timestamp() <= now:
            # The clock time has already passed today, so it means tomorrow.
            target += timedelta(days=1)
        return target.timestamp()

    return None


class ClaudeCLIProvider(Provider):
    """Runs `claude -p` as a one-shot completion.

    Each call is a fresh non-interactive session. That is deliberate: the
    daemon owns conversation state in SQLite, so a crashed or context-exhausted
    CLI invocation never takes the run's memory with it.
    """

    kind = "claude-cli"

    def __init__(self, name: str, config: dict[str, Any]):
        super().__init__(name, config)
        self.binary = config.get("binary", "claude")
        self.model = config.get("model", "")  # empty = whatever the CLI defaults to
        self.extra_args: list[str] = list(config.get("extraArgs") or [])
        self.cwd = config.get("cwd")
        # Skipping permission prompts is what makes unattended runs possible;
        # it is also a real grant of authority, so it stays opt-in.
        self.allow_all_tools = bool(config.get("allowAllTools", False))
        # `claude -p` is an agent, not a completion endpoint. Left with its
        # tools it runs a multi-turn session: it reads files, and it bills for
        # every turn. A measured reviewer call spent 208k cache-read tokens and
        # $0.34 to judge a diff the loop had already put in its prompt.
        #
        # That costs money, and it also quietly voids a guarantee. The loop
        # decides what each role may see (`_sources_for`, the per-ticket diff)
        # and what it may write (`patch.py`). A role with tools reads and
        # writes whatever it likes, so the same config gives different
        # guarantees depending on which adapter is behind a role.
        #
        # So: no tools by default, which makes this adapter behave the way the
        # loop already assumes — one call, one completion. Set "default" to get
        # the agent back, or name the tools it may use.
        self.tools = config.get("tools", "")

    def complete(
        self,
        messages: list[Message],
        *,
        max_tokens: int,
        temperature: float = 0.2,
        timeout: int = DERIVE_TIMEOUT,
    ) -> Completion:
        # Zero means the caller did not care; the budget decides. An
        # explicit timeout is always truthy and passes through.
        timeout = timeout or self.request_timeout(max_tokens)
        system, turns = split_system(messages)

        argv = [self.binary, "-p", "--output-format", "json"]
        if self.model:
            argv += ["--model", self.model]
        if system:
            argv += ["--append-system-prompt", system]
        if self.allow_all_tools:
            argv += ["--dangerously-skip-permissions"]
        # Passed even when empty — `--tools ""` is how the CLI is told to run
        # without any, and that is the default here.
        argv += ["--tools", self.tools]
        argv += self.extra_args

        prompt = "\n\n".join(
            f"{'User' if m.role == 'user' else 'Assistant'}: {m.content}" for m in turns
        )

        if not shutil.which(self.binary):
            raise ProviderUnreachable(
                f"{self.binary!r} not found on PATH; install Claude Code on the daemon host "
                "or point this provider at another backend"
            )

        try:
            result = subprocess.run(  # noqa: S603 - argv list, never shell=True
                argv,
                input=prompt,
                capture_output=True,
                text=True,
                # Pinned rather than left to the locale. Every prompt in this
                # project contains an em dash, and on a Windows host — where
                # the preferred encoding is cp1252 — writing one to the child's
                # stdin raises UnicodeEncodeError before the CLI sees anything.
                # The failure then surfaces as "no stdin data received", which
                # points nowhere near the actual cause.
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                cwd=self.cwd,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProviderUnreachable(f"{self.binary} timed out after {timeout}s") from exc

        combined = f"{result.stdout}\n{result.stderr}"

        # Check for a usage limit before anything else: a limited run exits
        # non-zero with no JSON, and misreading that as a parse failure would
        # burn the loop's retry budget against a wall that has not moved.
        if _LIMIT_PATTERN.search(combined):
            reset_at = parse_reset_time(combined)
            spend = _SPEND_LIMIT_PATTERN.search(combined)
            kind = "spend limit" if spend else "usage limit"
            raise RateLimited(
                f"{self.binary} reports a {kind}: {combined.strip()[:300]}",
                reset_at=reset_at,
                # No parseable time: wait a conservative 15 minutes and re-probe
                # rather than guessing the window length. A spend limit needs a
                # billing change rather than a clock, so back off much further.
                retry_after=(
                    None
                    if reset_at
                    else (_SPEND_LIMIT_RETRY_SECONDS if spend else 900)
                ),
            )

        if result.returncode != 0:
            raise ProviderBadResponse(
                f"{self.binary} exited {result.returncode}: {combined.strip()[:500]}"
            )

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ProviderBadResponse(
                f"{self.binary} returned non-JSON: {result.stdout[:300]}"
            ) from exc

        text = data.get("result") or ""
        raw_usage = data.get("usage") or {}
        # `input_tokens` counts only the uncached remainder. Every call the CLI
        # makes re-sends a large cached prefix (system prompt, tools, CLAUDE.md),
        # so the cache counters carry nearly all of the real input and reading
        # `input_tokens` alone reports a fraction of a percent of the true cost.
        usage = Usage(
            prompt_tokens=int(raw_usage.get("input_tokens", 0)),
            completion_tokens=int(raw_usage.get("output_tokens", 0)),
            cache_creation_tokens=int(raw_usage.get("cache_creation_input_tokens", 0)),
            cache_read_tokens=int(raw_usage.get("cache_read_input_tokens", 0)),
            # The CLI prices the call itself, including the cache-tier rates.
            cost_usd=float(data.get("total_cost_usd") or 0.0),
            estimated=not raw_usage,
        )
        if usage.estimated:
            usage.prompt_tokens = self.count_tokens(messages)
            usage.completion_tokens = self.count_tokens(
                [Message(role="assistant", content=text)]
            )

        return Completion(
            text=text,
            usage=usage,
            finish_reason="stop",
            model=self._model_name(data) or self.model or "claude-cli",
            raw=data,
        )

    @staticmethod
    def _model_name(data: dict[str, Any]) -> str:
        """Resolve the model that served the call.

        The result envelope has no top-level `model`, but `modelUsage` is keyed
        by it — so without this every CLI-backed call lands in the ledger under
        the literal string "claude-cli" and per-model totals are unusable.
        """
        model_usage = data.get("modelUsage")
        if isinstance(model_usage, dict) and model_usage:
            # Attribute to whichever model did the most work on this call.
            return max(
                model_usage,
                key=lambda name: int(
                    (model_usage[name] or {}).get("outputTokens", 0) or 0
                ),
            )
        return ""

    def capabilities(self) -> Capabilities:
        return Capabilities(
            context_window=int(self.config.get("contextWindow", 200_000)),
            max_output_tokens=int(self.config.get("maxOutputTokens", 32_000)),
            # The CLI has no temperature knob; the loop must not try to set one.
            supports_temperature=False,
        )
