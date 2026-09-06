"""The gate that decides whether a per-ticket write may be served.

HW-002. `write_refusal` is the whole of the access control for the dashboard's
write endpoints: there is no authentication anywhere in this server, so what it
returns is what stands between a note reaching an executor prompt and anyone
who can reach the port.

Both directions are pinned, because the failure worth guarding against is the
gate that fails open — a refusal computed and then not returned reads exactly
like a gate that decided to allow.

    python -m unittest discover tests
"""

from __future__ import annotations

import unittest
from pathlib import Path

from forge.config import Config
from forge.ui.writes import write_refusal


def config_for(host: str, *, allow_remote_writes: bool = False) -> Config:
    """A config that differs from its neighbours only where it should."""
    config = Config(
        root=Path("."),
        models={"m": {"kind": "openai", "model": "x", "contextWindow": 8192}},
        roles={role: "m" for role in ("planner", "executor", "tester", "reviewer")},
    )
    config.ui.host = host
    config.ui.allow_remote_writes = allow_remote_writes
    return config


class TestALoopbackDashboardServesALoopbackClient(unittest.TestCase):
    """The case the default is for: the operator, on their own machine."""

    def test_ipv4_loopback_bound_and_ipv4_loopback_peer_is_served(self):
        self.assertEqual(write_refusal(config_for("127.0.0.1"), "127.0.0.1"), "")

    def test_an_ipv6_loopback_peer_is_served(self):
        self.assertEqual(write_refusal(config_for("127.0.0.1"), "::1"), "")

    def test_a_dual_stack_mapped_loopback_peer_is_served(self):
        # What accept() hands back for an IPv4 client on a dual-stack socket.
        self.assertEqual(
            write_refusal(config_for("127.0.0.1"), "::ffff:127.0.0.1"), ""
        )


class TestAnythingElseIsRefusedByName(unittest.TestCase):
    """Either half failing is a refusal, and it names the setting that lifts it."""

    def test_an_exposed_bind_is_refused_even_from_loopback(self):
        refusal = write_refusal(config_for("0.0.0.0"), "127.0.0.1")

        self.assertIn("allowRemoteWrites", refusal)
        self.assertIn("0.0.0.0", refusal)

    def test_a_remote_peer_is_refused_even_on_a_loopback_bind(self):
        # The proxy case: bound to loopback, reached from somewhere else.
        refusal = write_refusal(config_for("127.0.0.1"), "10.0.0.4")

        self.assertIn("allowRemoteWrites", refusal)
        self.assertIn("10.0.0.4", refusal)

    def test_a_peer_the_socket_could_not_name_is_refused(self):
        refusal = write_refusal(config_for("127.0.0.1"), "")

        self.assertIn("allowRemoteWrites", refusal)


class TestTheOperatorCanTurnItOn(unittest.TestCase):
    """Set by name, in a file, rather than following from a bind address."""

    def test_an_exposed_bind_and_a_remote_peer_are_served_when_allowed(self):
        config = config_for("0.0.0.0", allow_remote_writes=True)

        self.assertEqual(write_refusal(config, "10.0.0.4"), "")


if __name__ == "__main__":
    unittest.main()
