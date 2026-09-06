"""`ui.allowRemoteWrites`: read from the file, and written back to it.

HW-001. The field the write gate reads. The round trip is the part worth
pinning: the gate consults it on every write request, so a field that loads but
does not survive `Config.write()` turns the endpoints off again the next time
anything saves the configuration.

    python -m unittest discover tests
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from forge.config import Config, UISettings

MODELS = {"m": {"kind": "openai", "model": "x", "contextWindow": 8192}}
ROLES = {role: "m" for role in ("planner", "executor", "tester", "reviewer")}


def write_config(root: Path, ui: dict) -> Path:
    """A minimal loadable config carrying the `ui` block under test."""
    path = root / ".hybridforge" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"models": MODELS, "roles": ROLES, "ui": ui}), encoding="utf-8"
    )
    return path


class TestTheDefault(unittest.TestCase):
    def test_a_settings_object_built_with_no_arguments_is_off(self):
        self.assertFalse(UISettings().allow_remote_writes)

    def test_a_ui_block_that_says_nothing_about_writes_is_off(self):
        root = Path(tempfile.mkdtemp()).resolve()
        write_config(root, {"host": "0.0.0.0", "port": 8799})

        config = Config.load(root)

        self.assertFalse(config.ui.allow_remote_writes)
        self.assertEqual(config.ui.host, "0.0.0.0")


class TestTheFileTurnsItOn(unittest.TestCase):
    def test_the_camel_case_key_is_read(self):
        root = Path(tempfile.mkdtemp()).resolve()
        write_config(root, {"allowRemoteWrites": True})

        config = Config.load(root)

        self.assertTrue(config.ui.allow_remote_writes)


class TestItSurvivesBeingWrittenBack(unittest.TestCase):
    """`Config.write()` serialises the whole `ui` block; this is in it."""

    def _round_trip(self, allowed: bool) -> Config:
        root = Path(tempfile.mkdtemp()).resolve()
        write_config(root, {"allowRemoteWrites": allowed})
        Config.load(root).write()
        return Config.load(root)

    def test_the_written_file_carries_the_key_beside_the_others(self):
        root = Path(tempfile.mkdtemp()).resolve()
        write_config(root, {"host": "127.0.0.1", "port": 8799, "enabled": True})
        config = Config.load(root)
        config.ui.allow_remote_writes = True

        path = config.write()
        payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertTrue(payload["ui"]["allowRemoteWrites"])
        self.assertEqual(payload["ui"]["host"], "127.0.0.1")
        self.assertEqual(payload["ui"]["port"], 8799)
        self.assertTrue(payload["ui"]["enabled"])

    def test_true_survives_a_round_trip(self):
        self.assertTrue(self._round_trip(True).ui.allow_remote_writes)

    def test_false_survives_a_round_trip(self):
        self.assertFalse(self._round_trip(False).ui.allow_remote_writes)


if __name__ == "__main__":
    unittest.main()
