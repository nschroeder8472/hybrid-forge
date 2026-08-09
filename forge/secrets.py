"""Credential detection for anything the loop is about to persist or publish.

Its own module because the concern is its own: the loop writes model-authored
text into a durable store that every future session reads back. A credential
that reaches project memory is not a one-off leak — it is replayed into every
subsequent prompt, and there is no undo.

Not exhaustive; no such list is. It covers the formats that actually turn up in
a working tree, and the asymmetry is deliberate: a false positive costs one
skipped memory entry, a miss costs a live key in permanent storage.
"""

from __future__ import annotations

import re

# (label, pattern, placeholder_exempt)
#
# `placeholder_exempt` is False for structural patterns — a string shaped like
# `AKIA` + 16 uppercase alphanumerics is a credential shape whatever letters it
# happens to spell, and exempting it would mean a real key containing the
# substring "example" sails through. It is True only for the loose keyword
# pattern below, which is the one that genuinely fires on prose.
_PATTERNS: tuple[tuple[str, re.Pattern[str], bool], ...] = (
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), False),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), False),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), False),
    ("Anthropic key", re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{16,}"), False),
    ("OpenAI-style key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b"), False),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b"), False),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"), False),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\."), False),
    ("bearer token", re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/]{24,}"), False),
    (
        "assigned secret",
        # `api_key = "…"`, `PASSWORD: …`, `token=…` with a value long enough to
        # be real. Short values are almost always placeholders in prose.
        re.compile(
            r"(?i)\b(api[_-]?key|secret|token|passwd|password|credential)\b"
            r"\s*[:=]\s*[\"']?[A-Za-z0-9_\-/+]{16,}"
        ),
        True,
    ),
)

# Obvious documentation placeholders. Without this the loop would refuse to
# record perfectly good conventions about *how to configure* credentials.
_PLACEHOLDERS = re.compile(
    r"(?i)(your[_-]?(api[_-]?)?key|xxx+|\.\.\.|<[a-z_]+>|placeholder|redacted|\*{4,})"
)


def find_secrets(text: str) -> list[str]:
    """Names of credential shapes found in `text`; empty when it looks clean."""
    if not text:
        return []
    found = []
    for label, pattern, placeholder_exempt in _PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        if placeholder_exempt and _PLACEHOLDERS.search(match.group(0)):
            continue
        found.append(label)
    return found


def looks_clean(text: str) -> bool:
    return not find_secrets(text)
