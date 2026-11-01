"""The format step over a file that is not text.

Phase 2 of docs/CREATIVE-TOOLS.md makes GIMP a formatter: `commands.format`
for `.png` runs a Python-Fu script over the images an attempt landed, GIMP
opening each path it is handed and saving it back. Two things the step did for
text were wrong for bytes:

1. **Change detection compared decoded text.** Before-and-after were read as
   UTF-8 with replacement, so two PNGs that differ only in bytes that are not
   valid UTF-8 decode identically, and a formatter that changed the file was
   reported as having changed nothing.
2. **An unchanged binary file was shown to the reviewer as text** — thousands
   of replacement characters standing in for a PNG's contents.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forge.config import ROLES, Config, Workspace  # noqa: E402
from forge.loop import Orchestrator  # noqa: E402
from forge.state import Store, Ticket  # noqa: E402

MODEL = {
    "kind": "openai",
    "baseUrl": "http://127.0.0.1:1/v1",
    "model": "stub",
    "contextWindow": 8192,
    "maxOutputTokens": 1024,
}

# Bytes that are invalid UTF-8 everywhere it matters: decoded with
# replacement, BEFORE and AFTER are the same string.
BEFORE = b"\x89PNG\r\n\x1a\n\x80\x80\x80"
AFTER = b"\x89PNG\r\n\x1a\n\x81\x80\x80"

# A formatter that rewrites one byte of each file it is handed.
FLIP = (
    'python -c "'
    "import sys,pathlib;"
    "[pathlib.Path(p).write_bytes(pathlib.Path(p).read_bytes()[:8]+b'\\x81'"
    "+pathlib.Path(p).read_bytes()[9:]) for p in sys.argv[1:]]"
    '"'
)


class BinaryFormatTests(unittest.TestCase):
    def orchestrator(self, format_command: str) -> tuple[Orchestrator, Path, int]:
        root = Path(tempfile.mkdtemp()).resolve()
        (root / "assets").mkdir()
        (root / "assets" / "hero.png").write_bytes(BEFORE)
        config = Config(
            root=root, models={"m": MODEL}, roles={role: "m" for role in ROLES}
        )
        config.declare_workspaces([
            Workspace(
                root=".",
                commands={"lint": "", "test": "", "format": {".png": format_command}},
            ),
        ])
        store = Store(config.db_path)
        self.addCleanup(store.close_all)
        return Orchestrator(config, store), root, store.create_run("goal")

    def test_the_two_files_really_do_decode_alike(self) -> None:
        # The premise of the next test, stated so a change to it is visible.
        self.assertEqual(
            BEFORE.decode("utf-8", errors="replace"),
            AFTER.decode("utf-8", errors="replace"),
        )

    def test_a_formatter_that_changed_a_binary_file_is_reported(self) -> None:
        orchestrator, root, run_id = self.orchestrator(FLIP)
        ticket = Ticket("IMG-1", title="t", spec="s", criteria=["c"],
                        allowed_files=["assets/hero.png"])

        changed = orchestrator._format_pass(run_id, ticket, ["assets/hero.png"])

        self.assertEqual((root / "assets" / "hero.png").read_bytes(), AFTER)
        self.assertEqual(changed, ["assets/hero.png"])

    def test_an_unchanged_binary_file_is_described_not_decoded(self) -> None:
        orchestrator, _root, _run_id = self.orchestrator("")

        shown = orchestrator._written_but_unchanged(["assets/hero.png"], diff="")

        self.assertNotIn("�", shown["assets/hero.png"])
        self.assertIn("binary", shown["assets/hero.png"])
        self.assertIn(str(len(BEFORE)), shown["assets/hero.png"])

    def test_an_unchanged_text_file_is_still_shown_as_text(self) -> None:
        orchestrator, root, _run_id = self.orchestrator("")
        (root / "notes.md").write_text("hello\n", encoding="utf-8")

        shown = orchestrator._written_but_unchanged(["notes.md"], diff="")

        self.assertEqual(shown["notes.md"], "hello\n")


if __name__ == "__main__":
    unittest.main()
