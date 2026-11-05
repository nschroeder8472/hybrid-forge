"""The smith's tools: anvil, gauge, mold. See docs/THE-FORGE.md."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from PIL import Image, ImageDraw

from forge.api import Forge
from forge.config import ROLES
from forge.mcp.server import Server
from forge.providers.image import Rendering
from forge.smith import mold as mold_module
from forge.smith.anvil import anvil
from forge.smith.gauge import gauge, measure
from forge.smith.mold import mold

_PROFILE_BEFORE: str | None = None


def setUpModule():
    global _PROFILE_BEFORE
    _PROFILE_BEFORE = os.environ.get("FORGE_PROFILE")
    os.environ["FORGE_PROFILE"] = str(Path(tempfile.mkdtemp()) / "profile.json")


def tearDownModule():
    if _PROFILE_BEFORE is None:
        os.environ.pop("FORGE_PROFILE", None)
    else:
        os.environ["FORGE_PROFILE"] = _PROFILE_BEFORE


_LINT = (
    "import pathlib, sys\n"
    "p = pathlib.Path('errors.txt')\n"
    "text = p.read_text() if p.exists() else ''\n"
    "print(text, end='')\n"
    "sys.exit(1 if text.strip() else 0)\n"
)


def _project(extra: dict | None = None) -> Path:
    root = Path(tempfile.mkdtemp()).resolve()
    (root / ".hybridforge").mkdir()
    (root / "lint.py").write_text(_LINT, encoding="utf-8")
    config = {
        "models": {"m": {"kind": "openai", "model": "x", "baseUrl": "http://127.0.0.1:1/v1"}},
        "roles": {r: "m" for r in ROLES},
        "commands": {"lint": f'"{sys.executable}" lint.py'},
        "ui": {"enabled": False},
    }
    config.update(extra or {})
    (root / ".hybridforge" / "config.json").write_text(json.dumps(config), encoding="utf-8")
    (root / ".gitignore").write_text("errors.txt\n", encoding="utf-8")
    for args in (["init", "-q"], ["add", "-A"],
                 ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "seed"]):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    return root


def _errors(root: Path, *lines: str) -> None:
    (root / "errors.txt").write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


def _mark_png(path: Path, colour=(230, 90, 80), size=256) -> None:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.line([(40, 40), (size - 40, size - 40)], fill=colour + (255,), width=24)
    draw.line([(size - 40, 40), (40, size - 40)], fill=colour + (255,), width=24)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)


class TestAnvil(unittest.TestCase):
    def test_the_first_call_records_a_baseline_and_says_so(self):
        root = _project()
        _errors(root, "a.py:1:1: F821 undefined name old")
        forge = Forge(root)
        try:
            first = anvil(forge)
        finally:
            forge.close()
        self.assertIn("this call recorded one", first["baseline"])

    def test_a_new_failure_is_introduced_and_an_old_one_is_counted(self):
        root = _project()
        _errors(root, "a.py:1:1: F821 undefined name old")
        forge = Forge(root)
        try:
            anvil(forge, mark=True)
            _errors(root, "a.py:1:1: F821 undefined name old",
                    "a.py:2:1: F821 undefined name new")
            struck = anvil(forge)
        finally:
            forge.close()
        (lint,) = struck["steps"]
        self.assertEqual(lint["introduced"], ["a.py:2:1: f821 undefined name new"])
        self.assertEqual(lint["already"], 1)
        self.assertFalse(struck["clean"])
        self.assertIn("step_id", lint)

    def test_an_import_of_nothing_is_flagged(self):
        root = _project()
        forge = Forge(root)
        try:
            anvil(forge, mark=True)
            (root / "main.js").write_text('import { x } from "./missing.js";\n', encoding="utf-8")
            struck = anvil(forge)
        finally:
            forge.close()
        self.assertEqual(struck["gates"]["unresolved_imports"], ["main.js: ./missing.js"])
        self.assertFalse(struck["clean"])

    def test_a_dependency_dropped_from_a_manifest_is_flagged(self):
        root = _project()
        manifest = {"name": "p", "devDependencies": {"eslint": "^9", "vitest": "^2"}}
        (root / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
        subprocess.run(["git", "add", "package.json"], cwd=root, check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit",
                        "-qm", "m"], cwd=root, check=True, capture_output=True)
        forge = Forge(root)
        try:
            anvil(forge, mark=True)
            manifest["devDependencies"].pop("vitest")
            (root / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
            struck = anvil(forge)
        finally:
            forge.close()
        self.assertEqual(struck["gates"]["dropped_dependencies"],
                         ["package.json: no longer declares vitest"])


class TestGauge(unittest.TestCase):
    def test_a_cut_out_mark_is_measured(self):
        root = _project()
        _mark_png(root / "assets" / "x.png")
        m = measure(root / "assets" / "x.png")
        self.assertTrue(m["transparent_corners"])
        self.assertFalse(m["fully_opaque"])
        self.assertTrue(0.05 < m["opaque_share"] < 0.5)
        self.assertGreater(m["opaque_mean_rgb"][0], m["opaque_mean_rgb"][2])
        self.assertEqual(m["centre_alpha"], 255)

    def test_drawn_lines_are_found_where_they_are(self):
        root = _project()
        img = Image.new("RGB", (300, 300), (40, 45, 45))
        draw = ImageDraw.Draw(img)
        for x in (100, 200):
            draw.line([(x, 0), (x, 299)], fill=(240, 240, 240), width=6)
        img.save(root / "board.png")
        m = measure(root / "board.png")
        self.assertTrue(m["fully_opaque"])
        found = [c for c in m["bright_columns"] if abs(c - 100) <= 4 or abs(c - 200) <= 4]
        self.assertGreaterEqual(len(found), 2, m["bright_columns"])

    def test_a_sheet_and_the_tree_changes(self):
        root = _project()
        _mark_png(root / "a.png")
        _mark_png(root / "b.png", colour=(40, 180, 170))
        forge = Forge(root)
        try:
            result = gauge(forge, paths=["a.png", "b.png"], sheet=True, changes=True)
        finally:
            forge.close()
        self.assertTrue(Path(result["sheet"]).is_file())
        self.assertEqual({c["path"] for c in result["changes"]}, {"a.png", "b.png"})


class _FakeImageModel:
    """Returns pre-made PNGs in turn, one per render."""

    def __init__(self, renders: list[bytes]):
        self.renders = list(renders)

    def generate(self, prompt, *, seed=None, negative="", width=0, height=0):
        data = self.renders.pop(0)
        return Rendering(data=data, media_type="image/png", width=256, height=256, seed=seed)


def _png_bytes(transparent: bool) -> bytes:
    img = Image.new("RGBA", (256, 256), (0, 0, 0, 0) if transparent else (255, 255, 255, 255))
    ImageDraw.Draw(img).ellipse([60, 60, 196, 196], outline=(40, 180, 170, 255), width=20)
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


class TestMold(unittest.TestCase):
    def test_candidates_are_measured_filtered_and_one_is_placed(self):
        root = _project()
        forge = Forge(root)
        forge.config.loop.image_model = "fake"
        fake = _FakeImageModel([_png_bytes(True), _png_bytes(False), _png_bytes(True)])
        try:
            with unittest.mock.patch.object(type(forge.config), "image_provider",
                                            lambda self, name: fake), \
                    unittest.mock.patch.object(mold_module, "image_servers",
                                               lambda config: None), \
                    unittest.mock.patch.object(mold_module.gpu, "clear_llama",
                                               lambda config: ([], [])), \
                    unittest.mock.patch.object(mold_module.gpu, "release_images",
                                               lambda config: []):
                cast = mold(forge, prompt="a teal ring", n=3, name="o",
                            require={"transparent_corners": True})
            passed = [c["k"] for c in cast["candidates"] if c["passed"]]
            self.assertEqual(passed, [1, 3])
            self.assertTrue(Path(cast["sheet"]).is_file())
            placed = mold(forge, batch=cast["batch"], pick=3, to="assets/o.png")
        finally:
            forge.close()
        self.assertEqual(placed["placed"], "assets/o.png")
        self.assertTrue((root / "assets" / "o.png").is_file())
        # The batch is scratch, not work: it stays out of git.
        status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"],
                                cwd=root, capture_output=True, text=True).stdout
        self.assertNotIn("mold/", status)

    def test_no_image_model_is_said_plainly(self):
        forge = Forge(_project())
        try:
            with self.assertRaisesRegex(Exception, "no image model"):
                mold(forge, prompt="anything")
        finally:
            forge.close()


class TestSmithKit(unittest.TestCase):
    def test_the_server_presents_the_smith_tools_by_default(self):
        reader = io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n")
        writer = io.StringIO()
        Server(_project()).serve(reader, writer)
        names = [t["name"] for t in json.loads(writer.getvalue())["result"]["tools"]]
        self.assertEqual(names, ["anvil", "gauge", "mold"])


if __name__ == "__main__":
    unittest.main()
