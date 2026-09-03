"""Shared HTTP plumbing for the HTTP-backed providers.

Stdlib only, on purpose: the daemon has to run on the same machines the plugin
already runs on, and `pip install` failures are a bad way to discover that your
overnight run never started.

The important work here is turning transport-level failures into the normalized
errors in `base`, especially extracting a rate-limit reset time from whichever
header the backend happens to use.
"""

from __future__ import annotations

import email.utils
import json
import time
import urllib.error
import urllib.request
from typing import Any

from .base import (
    ProviderAuthError,
    ProviderBadResponse,
    ProviderUnreachable,
    RateLimited,
)

# Header names carrying a reset time, most precise first. Different vendors,
# same idea; some are epoch seconds, some are HTTP dates, some are durations.
_RESET_HEADERS = (
    "anthropic-ratelimit-tokens-reset",
    "anthropic-ratelimit-requests-reset",
    "x-ratelimit-reset-tokens",
    "x-ratelimit-reset-requests",
    "x-ratelimit-reset",
    "ratelimit-reset",
)


def _parse_reset(value: str) -> float | None:
    """Best-effort conversion of a reset header into a unix timestamp.

    Accepts RFC3339 (`2026-08-08T21:04:00Z`), epoch seconds, and plain
    durations (`30`, `1m30s`). Returns None when the value makes no sense
    rather than guessing wildly — the caller falls back to retry-after.
    """
    value = value.strip()
    if not value:
        return None

    # RFC3339 / ISO8601
    if "T" in value:
        try:
            from datetime import datetime

            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass

    # Duration shorthand: 1m30s, 45s, 2h
    if any(c in value for c in "hms") and not value.replace(".", "").isdigit():
        seconds = 0.0
        number = ""
        for char in value:
            if char.isdigit() or char == ".":
                number += char
            elif char in "hms" and number:
                seconds += float(number) * {"h": 3600, "m": 60, "s": 1}[char]
                number = ""
        if seconds:
            return time.time() + seconds
        return None

    try:
        numeric = float(value)
    except ValueError:
        return None

    # Heuristic: anything past 2001 is an absolute epoch, anything smaller is a
    # duration in seconds. Both forms appear in the wild under the same header.
    return numeric if numeric > 1_000_000_000 else time.time() + numeric


def _retry_after(headers: Any) -> float | None:
    raw = headers.get("retry-after") if headers else None
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        parsed = email.utils.parsedate_to_datetime(raw)
        return max(0.0, parsed.timestamp() - time.time()) if parsed else None


def _reset_at(headers: Any) -> float | None:
    if not headers:
        return None
    for header in _RESET_HEADERS:
        raw = headers.get(header)
        if raw:
            parsed = _parse_reset(raw)
            if parsed:
                return parsed
    return None


def post_json(
    url: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str],
    timeout: int,
) -> dict[str, Any]:
    """POST JSON, return parsed JSON, raise a normalized ProviderError on failure."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8")[:1000]
        except Exception:  # noqa: BLE001 - the error body is best-effort
            pass

        if exc.code == 429 or exc.code == 529:
            raise RateLimited(
                f"{url} returned {exc.code}: {detail[:300]}",
                reset_at=_reset_at(exc.headers),
                retry_after=_retry_after(exc.headers),
            ) from exc
        if exc.code in (401, 403):
            raise ProviderAuthError(f"{url} returned {exc.code}: {detail[:300]}") from exc
        if exc.code >= 500:
            raise ProviderUnreachable(f"{url} returned {exc.code}: {detail[:300]}") from exc
        # 400s that mention context are worth naming precisely — the loop reacts
        # to overflow differently from a generic bad request.
        lowered = detail.lower()
        if "context" in lowered and (
            "length" in lowered or "window" in lowered or "too long" in lowered
        ):
            from .base import ContextOverflow

            raise ContextOverflow(f"{url}: {detail[:300]}") from exc
        raise ProviderBadResponse(f"{url} returned {exc.code}: {detail[:300]}") from exc
    except urllib.error.URLError as exc:
        raise ProviderUnreachable(f"could not reach {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise ProviderUnreachable(f"timed out after {timeout}s reaching {url}") from exc

    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise ProviderBadResponse(f"{url} returned non-JSON: {body[:300]}") from exc


def get_json(url: str, *, headers: dict[str, str], timeout: int = 30) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise ProviderUnreachable(f"could not reach {url}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ProviderBadResponse(f"{url} returned non-JSON") from exc
