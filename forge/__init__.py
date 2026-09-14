"""Hybrid Forge: an autonomous plan-and-execute loop over any models you bring.

A daemon owns the state machine and drives the models, rather than a model
deciding what happens next. See `forge.loop` for the state machine, and
`forge.providers` for the adapter layer that makes the model choice yours.
"""

from importlib.metadata import PackageNotFoundError, version as _installed_version

# Read from the installed distribution rather than restated here. The literal
# that used to live on this line said 0.2.0 while pyproject.toml said 0.2.1,
# because nothing read it and so nothing caught the drift. A version that
# disagrees with the package it names is worse than no version at all when the
# question being asked is "which build is on that other machine".
try:
    __version__ = _installed_version("hybrid-forge")
except PackageNotFoundError:
    # Running from a source tree that was never installed — `python -m forge`
    # out of a clone. There is no distribution to ask, and inventing a number
    # would be the drift this import exists to prevent.
    __version__ = "0+unknown"

__all__ = ["__version__"]
