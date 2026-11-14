"""Hand-written baselines the decision models have to beat. Fixed before any model ran.

failure_kind: keyword rules of the kind forge's `failures.py` is made of.
first_try:    the base rate — there is no rule today, so the bar is
              "knows nothing about the ticket".
"""

from __future__ import annotations

import re

KINDS = ("setup", "compile", "lint", "test")

_SETUP = re.compile(
    r"could not find `Cargo\.toml`|Cannot find module|is not recognized as an internal"
    r"|command not found|No such file or directory|requires JVM|No module named",
    re.IGNORECASE,
)
_COMPILE = re.compile(
    r"error\[E\d+\]|compile(Test)?Java FAILED|error: linking with|SyntaxError"
    r"|cannot find symbol|error TS\d+|unknown start of token|unexpected closing delimiter"
    r"|key with no value",
)
_TEST = re.compile(
    r"\.\.\. FAILED|^FAIL:|panicked at|AssertionError|Task :test FAILED|FAILED \((failures|errors)="
    r"|> \w+\(\) FAILED",
    re.MULTILINE,
)


def failure_kind(step: str, output: str) -> str:
    if _SETUP.search(output):
        return "setup"
    if _COMPILE.search(output):
        return "compile"
    if _TEST.search(output):
        return "test"
    if step.startswith(("lint", "baseline-lint", "typecheck", "baseline-typecheck")):
        return "lint"
    return "test" if step.startswith(("test", "baseline-test")) else "lint"


def first_try(base_rate: float) -> float:
    return base_rate
