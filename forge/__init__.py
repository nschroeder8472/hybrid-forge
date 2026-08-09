"""Hybrid Forge: an autonomous plan-and-execute loop over any models you bring.

A daemon owns the state machine and drives the models, rather than a model
deciding what happens next. See `forge.loop` for the state machine, and
`forge.providers` for the adapter layer that makes the model choice yours.
"""

__version__ = "0.2.0"
