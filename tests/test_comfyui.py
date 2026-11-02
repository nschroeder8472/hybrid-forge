"""ComfyUI as an image model, against a stub that speaks its HTTP API.

What this module holds to (docs/CREATIVE-TOOLS.md, phase 4; docs/IMAGE-LOOP.md,
phase 2):

1. **A workflow that cannot work is refused when the config loads**: a UI-format
   export, a binding to an input the graph does not have, an edit workflow with
   no image input, a binding nothing sends.
2. **A render sends the graph with forge's values in the bound inputs**, and
   returns the bytes ComfyUI saved, with the seed it was sent.
3. **ComfyUI's refusals are not swallowed**: `node_errors` is a configuration
   error, a failed render is a bad response.
4. **A rendering is recorded in images and seconds, not tokens.**
5. **Readiness checks what the workflows need**: node classes and model files.
6. **ComfyUI starts only for a backlog with image work**, and the GPU lease
   reports a backend that did not give the card back.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import unittest.mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forge import comfy_server, gpu  # noqa: E402
from forge.budget import BudgetGate  # noqa: E402
from forge.config import Config, ConfigError  # noqa: E402
from forge.providers import ImageUsage  # noqa: E402
from forge.providers.base import ProviderBadResponse  # noqa: E402
from forge.providers.comfyui import ComfyConfigError, ComfyUIProvider  # noqa: E402
from forge.providers.image import png_size  # noqa: E402
from forge.run_servers import RunServers  # noqa: E402
from forge.state import Store  # noqa: E402

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    + (64).to_bytes(4, "big") + (32).to_bytes(4, "big")
    + b"\x08\x06\x00\x00\x00" + b"\x00" * 16
)

GENERATE = {
    "3": {"class_type": "KSampler", "inputs": {"seed": 1, "steps": 20, "model": ["4", 0]}},
    "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "sdxl.safetensors"}},
    "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 1024, "height": 1024}},
    "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "placeholder"}},
    "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0]}},
}
EDIT = {
    "1": {"class_type": "LoadImage", "inputs": {"image": "example.png"}},
    "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "placeholder"}},
    "3": {"class_type": "KSampler", "inputs": {"seed": 1}},
    "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0]}},
}
BIND = {
    "prompt": ["6", "text"], "seed": ["3", "seed"],
    "width": ["5", "width"], "height": ["5", "height"], "image": ["1", "image"],
}


class Stub:
    """A ComfyUI that renders instantly and remembers what it was sent."""

    def __init__(self) -> None:
        self.prompts: list[dict] = []
        self.uploads: list[bytes] = []
        self.node_errors: dict = {}
        self.fail = False
        self.torch_vram = 0
        self.freed = 0
        self.classes = {
            "KSampler": {"input": {"required": {}}},
            "CheckpointLoaderSimple": {
                "input": {"required": {"ckpt_name": [["sdxl.safetensors", "other.safetensors"]]}}
            },
            "EmptyLatentImage": {"input": {"required": {}}},
            "CLIPTextEncode": {"input": {"required": {}}},
            "SaveImage": {"input": {"required": {}}},
            "LoadImage": {"input": {"required": {}}},
        }
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                pass

            def reply(self, body, status=200, raw=False) -> None:
                data = body if raw else json.dumps(body).encode()
                self.send_response(status)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                if self.path == "/system_stats":
                    self.reply({"system": {"comfyui_version": "0.43.0"},
                                "devices": [{"vram_free": 30 << 30,
                                             "torch_vram_total": stub.torch_vram}]})
                elif self.path.startswith("/history/"):
                    status = ({"status_str": "error", "messages": ["boom"]} if stub.fail
                              else {"status_str": "success", "completed": True})
                    self.reply({"p1": {"status": status, "outputs": {"9": {"images": [
                        {"filename": "x.png", "subfolder": "", "type": "output"}]}}}})
                elif self.path.startswith("/view"):
                    self.reply(PNG, raw=True)
                elif self.path.startswith("/object_info/"):
                    cls = self.path.rsplit("/", 1)[1]
                    self.reply({cls: stub.classes[cls]} if cls in stub.classes else {})
                else:
                    self.reply({}, 404)

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers["Content-Length"]))
                if self.path == "/prompt":
                    stub.prompts.append(json.loads(body)["prompt"])
                    self.reply({"prompt_id": "p1", "node_errors": stub.node_errors})
                elif self.path == "/upload/image":
                    stub.uploads.append(body)
                    self.reply({"name": "forge-upload.png", "subfolder": ""})
                elif self.path == "/free":
                    # The real server answers with an empty body, not JSON.
                    stub.freed += 1
                    self.reply(b"", raw=True)
                else:
                    self.reply({})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class ComfyCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / ".hybridforge" / "comfy").mkdir(parents=True)
        self.stub = Stub()
        self.addCleanup(self.stub.close)
        patcher = unittest.mock.patch.dict(
            os.environ, {"FORGE_PROFILE": str(self.root / "profile.json")}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def workflow(self, name: str, graph: dict) -> str:
        path = self.root / ".hybridforge" / "comfy" / f"{name}.json"
        path.write_text(json.dumps(graph), encoding="utf-8")
        return f".hybridforge/comfy/{name}.json"

    def block(self, **overrides) -> dict:
        block = {
            "kind": "comfyui",
            "url": self.stub.url,
            "root": str(self.root),
            "workflows": {"generate": self.workflow("gen", GENERATE),
                          "edit": self.workflow("edit", EDIT)},
            "bind": BIND,
        }
        block.update(overrides)
        return block

    def config(self, image_models: dict, comfyui: dict | None = None) -> Config:
        body = {
            "models": {"m": {"kind": "openai", "baseUrl": "http://127.0.0.1:1/v1",
                             "model": "x"}},
            "roles": {r: "m" for r in ("planner", "executor", "tester", "reviewer")},
            "imageModels": image_models,
        }
        (self.root / ".hybridforge" / "config.json").write_text(json.dumps(body))
        if comfyui is not None:
            (self.root / "profile.json").write_text(json.dumps({"comfyui": comfyui}))
        return Config.load(self.root)


class LoadTests(ComfyCase):
    def test_a_ui_format_export_is_refused_by_shape(self) -> None:
        ui = self.workflow("ui", {"nodes": [], "links": [], "version": 0.4})
        with self.assertRaises(ComfyConfigError) as caught:
            ComfyUIProvider("p", self.block(workflows={"generate": ui}))
        self.assertIn("Export (API)", str(caught.exception))

    def test_a_binding_to_a_missing_input_is_refused_at_config_load(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            self.config({"p": self.block(bind={**BIND, "seed": ["3", "noise_seed"]})})
        self.assertIn("noise_seed", str(caught.exception))

    def test_a_binding_nothing_sends_is_refused(self) -> None:
        with self.assertRaises(ComfyConfigError):
            ComfyUIProvider("p", self.block(bind={**BIND, "cfg": ["3", "steps"]}))

    def test_an_edit_workflow_with_no_image_binding_is_refused(self) -> None:
        bind = {k: v for k, v in BIND.items() if k != "image"}
        with self.assertRaises(ComfyConfigError):
            ComfyUIProvider("p", self.block(bind=bind))

    def test_capabilities_come_from_what_is_declared(self) -> None:
        both = ComfyUIProvider("p", self.block()).capabilities()
        self.assertTrue(both.supports_edit and both.supports_seed)
        only = ComfyUIProvider(
            "p", self.block(workflows={"generate": self.workflow("g", GENERATE)})
        ).capabilities()
        self.assertFalse(only.supports_edit)

    def test_the_comfyui_block_is_refused_in_a_repository_config(self) -> None:
        body = {"comfyui": {"root": "x", "python": "y"}}
        (self.root / ".hybridforge" / "config.json").write_text(json.dumps(body))
        with self.assertRaises(ConfigError):
            Config.load(self.root)


class WriteTests(ComfyCase):
    def test_saving_a_config_keeps_its_image_models(self) -> None:
        config = self.config({"p": self.block(url="")})
        config.write()
        saved = json.loads((self.root / ".hybridforge" / "config.json").read_text())
        self.assertIn("p", saved["imageModels"])
        self.assertEqual(Config.load(self.root).image_models.keys(), {"p"})


class RenderTests(ComfyCase):
    def test_generate_fills_the_bound_inputs_and_returns_the_image(self) -> None:
        provider = ComfyUIProvider("p", self.block())
        rendering = provider.generate("an anvil", width=512, height=256, seed=7)
        sent = self.stub.prompts[0]
        self.assertEqual(sent["6"]["inputs"]["text"], "an anvil")
        self.assertEqual(sent["3"]["inputs"]["seed"], 7)
        self.assertEqual((sent["5"]["inputs"]["width"], sent["5"]["inputs"]["height"]), (512, 256))
        self.assertEqual(sent["3"]["inputs"]["steps"], 20)
        self.assertEqual(rendering.data, PNG)
        self.assertEqual((rendering.width, rendering.height), (64, 32))
        self.assertEqual(rendering.seed, 7)
        self.assertEqual(rendering.usage.images, 1)

    def test_an_unset_size_keeps_the_workflows_own(self) -> None:
        ComfyUIProvider("p", self.block()).generate("x", seed=1)
        self.assertEqual(self.stub.prompts[0]["5"]["inputs"]["width"], 1024)

    def test_a_seed_forge_chose_is_reported(self) -> None:
        rendering = ComfyUIProvider("p", self.block()).generate("x")
        self.assertEqual(rendering.seed, self.stub.prompts[0]["3"]["inputs"]["seed"])

    def test_edit_uploads_the_image_and_binds_its_name(self) -> None:
        ComfyUIProvider("p", self.block()).edit("make it red", image=PNG, seed=3)
        self.assertEqual(len(self.stub.uploads), 1)
        self.assertIn(PNG, self.stub.uploads[0])
        self.assertEqual(self.stub.prompts[0]["1"]["inputs"]["image"], "forge-upload.png")

    def test_node_errors_are_a_configuration_error(self) -> None:
        self.stub.node_errors = {"3": {"errors": ["bad sampler"]}}
        with self.assertRaises(ComfyConfigError) as caught:
            ComfyUIProvider("p", self.block()).generate("x")
        self.assertIn("bad sampler", str(caught.exception))

    def test_a_failed_render_is_a_bad_response(self) -> None:
        self.stub.fail = True
        with self.assertRaises(ProviderBadResponse):
            ComfyUIProvider("p", self.block()).generate("x")

    def test_png_size_reads_the_header_and_nothing_else(self) -> None:
        self.assertEqual(png_size(PNG), (64, 32))
        self.assertEqual(png_size(b"not a png"), (0, 0))


class AccountingTests(ComfyCase):
    def test_a_rendering_is_recorded_in_images_and_seconds(self) -> None:
        store = Store(self.root / "run.db")
        self.addCleanup(store.close_all)
        BudgetGate(store).record_rendering("pixel", ImageUsage(images=1, seconds=3.5))
        with store._write() as connection:
            row = connection.execute(
                "SELECT model, images, render_seconds, prompt_tokens FROM usage"
            ).fetchone()
        self.assertEqual(tuple(row), ("pixel", 1, 3.5, 0))


class ServerTests(ComfyCase):
    def test_readiness_names_a_missing_node_class_and_a_missing_model_file(self) -> None:
        graph = json.loads(json.dumps(GENERATE))
        graph["4"]["inputs"]["ckpt_name"] = "flux.safetensors"
        graph["7"] = {"class_type": "MysteryNode", "inputs": {}}
        config = self.config({"p": self.block(
            url="", workflows={"generate": self.workflow("g2", graph)})})

        def info(cls: str):
            return self.stub.classes.get(cls)

        problems = comfy_server.missing(config, ["p"], info)
        self.assertTrue(any("MysteryNode" in p for p in problems))
        self.assertTrue(any("flux.safetensors" in p for p in problems))

    def test_everything_present_is_ready(self) -> None:
        config = self.config({"p": self.block(url="")})
        problems = comfy_server.missing(config, ["p"], lambda cls: self.stub.classes.get(cls))
        self.assertEqual(problems, [])

    def test_a_model_with_no_url_is_served_by_the_managed_comfyui(self) -> None:
        self.assertTrue(comfy_server.is_managed({"kind": "comfyui"}, 8188))
        self.assertTrue(comfy_server.is_managed(
            {"kind": "comfyui", "url": "http://127.0.0.1:8188"}, 8188))
        self.assertFalse(comfy_server.is_managed(
            {"kind": "comfyui", "url": "http://gpu-host:8188"}, 8188))

    def test_comfyui_is_not_started_for_a_backlog_with_no_image_work(self) -> None:
        main = self.root / "comfy" / "main.py"
        main.parent.mkdir()
        main.write_text("")
        config = self.config(
            {"p": self.block(url="")},
            {"root": str(main.parent), "python": sys.executable, "port": 8188},
        )
        self.assertIsNone(RunServers.for_config(config, images=False))
        servers = RunServers.for_config(config, images=True)
        self.assertEqual([spec.name for spec in servers.specs], ["comfyui"])

    def test_a_run_with_image_work_clears_the_card_before_anything_loads(self) -> None:
        # Phase 7: an adopted ComfyUI still held 28 GB of Flux.2, Qwen loaded
        # beside it into system RAM, and sign-off ran six times slower.
        config = self.config({"p": self.block()})
        servers = RunServers.for_config(config, images=True)
        self.assertIsNotNone(servers)
        servers.start()
        self.assertEqual(self.stub.freed, 1)

    def test_a_run_without_image_work_leaves_comfyui_alone(self) -> None:
        config = self.config({"p": self.block()})
        self.assertIsNone(RunServers.for_config(config, images=False))
        self.assertEqual(self.stub.freed, 0)

    def test_owned_flags_are_refused_in_args(self) -> None:
        with self.assertRaises(comfy_server.ComfyServerError):
            comfy_server.Settings.from_block({"root": "r", "python": "p", "args": ["--port", "1"]})


class LeaseTests(ComfyCase):
    def test_release_frees_and_reports_a_backend_still_holding_the_card(self) -> None:
        config = self.config({"p": self.block()})
        self.assertEqual(gpu.release_images(config), [])
        self.assertEqual(self.stub.freed, 1)
        self.stub.torch_vram = 20 << 30
        problems = gpu.release_images(config)
        self.assertEqual(len(problems), 1)
        self.assertIn("still holding", problems[0])


if __name__ == "__main__":
    unittest.main()
