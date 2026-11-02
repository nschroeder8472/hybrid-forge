"""The image ticket kind: a prompt, a render, and the same pipeline after.

What this module holds to (docs/IMAGE-LOOP.md phases 3-4; docs/CREATIVE-TOOLS.md
phase 5):

1. **A kind the loop cannot run is parked before any model is called** — an
   unknown kind no longer runs down the feature path.
2. **An image ticket needs what it needs, or it is parked by name**: an image
   model, exactly one image path, a reviewing seat that can see.
3. **The executor writes a PROMPT / NEGATIVE / SEED block**, the image model
   renders it into the ticket's image path, and the rendering is recorded in
   images and seconds before anything judges it.
4. **The review is shown the image**, first, on the seat that can see.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forge.config import Config, LoopSettings  # noqa: E402
from forge.loop import Orchestrator  # noqa: E402
from forge.prompts import parse_image_reply  # noqa: E402
from forge.providers import Completion, Usage  # noqa: E402
from forge.providers.comfyui import ComfyConfigError  # noqa: E402
from forge.providers.image import ImageCapabilities, ImageUsage, Rendering  # noqa: E402
from forge.state import TICKET_BLOCKED, Store, Ticket  # noqa: E402

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    + (32).to_bytes(4, "big") + (32).to_bytes(4, "big")
    + b"\x08\x06\x00\x00\x00" + b"\x00" * 16
)
TEXT = {"kind": "openai", "baseUrl": "http://127.0.0.1:1/v1", "model": "stub",
        "contextWindow": 8192, "maxOutputTokens": 1024}
SEEING = {**TEXT, "multimodal": True}


class FakeImageModel:
    kind = "fake"

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.error: Exception | None = None

    def generate(self, prompt, *, width=0, height=0, seed=None, negative=""):
        if self.error:
            raise self.error
        self.calls.append({"prompt": prompt, "seed": seed, "negative": negative})
        return Rendering(PNG, "image/png", 32, 32, seed if seed is not None else 99,
                         usage=ImageUsage(images=1, seconds=2.5))

    def capabilities(self) -> ImageCapabilities:
        return ImageCapabilities(supports_seed=True)


def orchestrator(*, image_model: str = "pixel", reviewer: dict = SEEING):
    root = Path(tempfile.mkdtemp()).resolve()
    config = Config(
        root=root,
        models={"m": TEXT, "eye": reviewer},
        roles={"planner": "m", "executor": "m", "tester": "m", "reviewer": "eye"},
        commands={"lint": "", "typecheck": "", "test": ""},
        loop=LoopSettings(ratify_passes=0, executor_turns=0, image_model=image_model),
    )
    config.image_models = {"pixel": {"kind": "fake", "promptGuide": "start with 'pixel art'"}}
    fake = FakeImageModel()
    config.image_provider = lambda name: fake  # type: ignore[method-assign]
    store = Store(root / "t.db")
    return Orchestrator(config, store), root, store.create_run("goal"), fake, store


def ticket(**overrides) -> Ticket:
    base = dict(
        title="a chest icon",
        spec="A 32x32 pixel-art treasure chest.",
        criteria=["assets/chest.png is 32x32"],
        allowed_files=["assets/chest.png", "tests/chest_test.py"],
        kind="image",
    )
    base.update(overrides)
    return Ticket("IMG-001", **base)


class ReplyTests(unittest.TestCase):
    def test_the_three_fields_are_read(self) -> None:
        reply = parse_image_reply("PROMPT: a chest\nNEGATIVE: text, blur\nSEED: 41")
        self.assertEqual((reply.prompt, reply.negative, reply.seed), ("a chest", "text, blur", 41))

    def test_a_wrapped_prompt_joins(self) -> None:
        reply = parse_image_reply("PROMPT: a wooden chest\nwith iron bands\nSEED:")
        self.assertEqual(reply.prompt, "a wooden chest with iron bands")
        self.assertIsNone(reply.seed)

    def test_a_seed_that_is_not_a_number_is_dropped(self) -> None:
        self.assertIsNone(parse_image_reply("PROMPT: x\nSEED: random").seed)

    def test_thinking_is_ignored(self) -> None:
        reply = parse_image_reply("<think>PROMPT: draft</think>\nPROMPT: final")
        self.assertEqual(reply.prompt, "final")

    def test_blocked(self) -> None:
        self.assertEqual(parse_image_reply("BLOCKED: no size given").blocked, "no size given")

    def test_markdown_bold_labels_are_read(self) -> None:
        self.assertEqual(parse_image_reply("**PROMPT:** a chest").prompt, "a chest")


class GateTests(unittest.TestCase):
    def parked(self, orch, run_id, store, t) -> str:
        store.add_tickets(run_id, [t])
        self.assertFalse(orch._kind_gate(run_id, t))
        saved = store.list_tickets(run_id)[0]
        self.assertEqual(saved.status, TICKET_BLOCKED)
        return saved.blocked_note

    def test_an_unknown_kind_is_parked(self) -> None:
        orch, _root, run_id, _fake, store = orchestrator()
        note = self.parked(orch, run_id, store, ticket(kind="video"))
        self.assertIn("'video'", note)

    def test_no_image_model_is_parked_by_name(self) -> None:
        orch, _root, run_id, _fake, store = orchestrator(image_model="")
        self.assertIn("loop.imageModel", self.parked(orch, run_id, store, ticket()))

    def test_two_image_paths_are_parked(self) -> None:
        orch, _root, run_id, _fake, store = orchestrator()
        t = ticket(allowed_files=["a.png", "b.png", "tests/t_test.py"])
        self.assertIn("exactly one image", self.parked(orch, run_id, store, t))

    def test_a_blind_reviewer_is_parked(self) -> None:
        orch, _root, run_id, _fake, store = orchestrator(reviewer=TEXT)
        self.assertIn("cannot be shown an image", self.parked(orch, run_id, store, ticket()))

    def test_a_runnable_image_ticket_passes(self) -> None:
        orch, _root, run_id, _fake, store = orchestrator()
        self.assertTrue(orch._kind_gate(run_id, ticket()))

    def test_a_feature_ticket_is_untouched(self) -> None:
        orch, _root, run_id, _fake, store = orchestrator(image_model="")
        self.assertTrue(orch._kind_gate(run_id, ticket(kind="feature")))


class AttemptTests(unittest.TestCase):
    def run_attempt(self, replies: list[str], **setup):
        orch, root, run_id, fake, store = orchestrator(**setup)
        seen: list[dict] = []

        def call(*args, **kwargs):
            seen.append({"role": args[1] if len(args) > 1 else "", "messages": args[2], **kwargs})
            text = replies[min(len(seen) - 1, len(replies) - 1)]
            return Completion(text=text, usage=Usage(), finish_reason="stop")

        orch._call = call
        result = orch._attempt(run_id, ticket(), "")
        return result, seen, fake, root, store, run_id

    REPLIES = [
        "PROMPT: pixel art, a wooden chest\nNEGATIVE: text\nSEED: 41",
        "tests/chest_test.py\n```python\ndef test_a():\n    assert True\n```",
        "ACCEPT\nthe chest reads as a chest",
    ]

    def test_the_prompt_is_rendered_into_the_image_path(self) -> None:
        result, seen, fake, root, _store, _run = self.run_attempt(self.REPLIES)
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(fake.calls, [{"prompt": "pixel art, a wooden chest", "seed": 41,
                                       "negative": "text"}])
        self.assertEqual((root / "assets" / "chest.png").read_bytes(), PNG)

    def test_the_executor_is_shown_the_models_prompt_guide(self) -> None:
        _result, seen, *_ = self.run_attempt(self.REPLIES)
        executor = next(c for c in seen if c["role"] == "executor")
        self.assertIn("start with 'pixel art'", executor["messages"][-1].text)

    def test_the_review_sees_the_image_first(self) -> None:
        _result, seen, *_ = self.run_attempt(self.REPLIES)
        review = next(c for c in seen if c["role"] == "reviewer")
        self.assertEqual(review["images"][0].data, PNG)

    def test_the_rendering_is_recorded_in_images_and_seconds(self) -> None:
        _result, _seen, _fake, _root, store, _run = self.run_attempt(self.REPLIES)
        with store._write() as connection:
            rows = connection.execute(
                "SELECT model, images, render_seconds FROM usage WHERE images > 0"
            ).fetchall()
        self.assertEqual([tuple(r) for r in rows], [("pixel", 1, 2.5)])

    def test_the_render_is_kept_in_the_attempts_artifacts(self) -> None:
        _result, _seen, _fake, root, _store, run_id = self.run_attempt(self.REPLIES)
        kept = list((root / ".hybridforge").rglob("render.png"))
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].read_bytes(), PNG)

    def test_a_reply_with_no_prompt_ends_the_attempt_and_renders_nothing(self) -> None:
        result, _seen, fake, root, *_ = self.run_attempt(["Here is my plan for the image."])
        self.assertFalse(result.ok)
        self.assertIn("PROMPT:", result.detail)
        self.assertEqual(fake.calls, [])
        self.assertFalse((root / "assets" / "chest.png").exists())

    def test_blocked_blocks(self) -> None:
        result, *_ = self.run_attempt(["BLOCKED: no size given"])
        self.assertTrue(result.blocked)

    def test_a_workflow_comfyui_refuses_blocks_rather_than_retrying(self) -> None:
        orch, root, run_id, fake, store = orchestrator()
        fake.error = ComfyConfigError("node_errors: bad sampler")
        orch._call = lambda *a, **k: Completion(
            text="PROMPT: x", usage=Usage(), finish_reason="stop")
        result = orch._attempt(run_id, ticket(), "")
        self.assertTrue(result.blocked)
        self.assertIn("bad sampler", result.detail)

    def test_a_retry_starts_from_the_last_prompt(self) -> None:
        orch, root, run_id, fake, store = orchestrator()
        sent: list[str] = []

        def call(*args, **kwargs):
            sent.append(args[2][-1].text)
            return Completion(text="PROMPT: first try\nSEED: 1", usage=Usage(),
                              finish_reason="stop")

        orch._call = call
        orch._render_attempt(run_id, ticket(), "", [])
        orch._render_attempt(run_id, ticket(), "the chest is blue", [])
        self.assertIn("PROMPT: first try", sent[1])
        self.assertIn("the chest is blue", sent[1])


if __name__ == "__main__":
    unittest.main()


class IngestKindTests(unittest.TestCase):
    """`**Kind:**` is read as written; the loop, not ingest, decides what runs."""

    PLAN = """## IMG-001: A chest

**Kind:** {kind}

### Spec

Draw a chest.

### Allowed files

- assets/chest.png
"""

    def kind_of(self, kind: str) -> str:
        from forge.ingest import parse_plan

        return parse_plan(self.PLAN.format(kind=kind))[0].kind

    def test_image_is_kept(self) -> None:
        # It used to be read as `feature`, and the ticket ran down the code path.
        self.assertEqual(self.kind_of("image"), "image")

    def test_case_is_folded(self) -> None:
        self.assertEqual(self.kind_of("Bug"), "bug")

    def test_an_unknown_kind_is_kept_for_the_gate_to_refuse(self) -> None:
        self.assertEqual(self.kind_of("imag"), "imag")


class EditLoopTests(unittest.TestCase):
    """Phase 6: an EDIT reply edits the last raw render; a stall parks the ticket."""

    def setup(self, edit_model: str = "kontext", stall: int = 3):
        orch, root, run_id, fake, store = orchestrator()
        orch.config.loop.edit_model = edit_model
        orch.config.loop.image_stall = stall
        edits: list[dict] = []

        class FakeEditor(FakeImageModel):
            def edit(self, prompt, *, image, mask=None, seed=None):
                edits.append({"instruction": prompt, "image": image})
                return Rendering(PNG, "image/png", 32, 32, 5,
                                 usage=ImageUsage(images=1, seconds=4.0))

        editor = FakeEditor()
        orch.config.image_provider = (  # type: ignore[method-assign]
            lambda name: editor if name == "kontext" else fake
        )
        return orch, root, run_id, fake, store, edits

    def keep_render(self, root: Path, run_id: int, attempt: int, data: bytes) -> None:
        folder = (
            root / ".hybridforge" / "artifacts" / f"run-{run_id}" / "IMG-001"
            / f"attempt-{attempt}"
        )
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "render.png").write_bytes(data)

    def replying(self, orch, text: str) -> list[str]:
        sent: list[str] = []

        def call(*args, **kwargs):
            sent.append(args[2][-1].text)
            return Completion(text=text, usage=Usage(), finish_reason="stop")

        orch._call = call
        return sent

    def test_an_edit_reply_edits_the_last_raw_render(self) -> None:
        orch, root, run_id, fake, store, edits = self.setup()
        raw = PNG + b"raw-1024"
        self.keep_render(root, run_id, 1, raw)
        t = ticket()
        t.attempts = 2
        self.replying(orch, "EDIT: Make the lock plate gold. Keep everything else.")
        written, refused = orch._render_attempt(run_id, t, "lock is silver", [])
        self.assertIsNone(refused)
        self.assertEqual(edits[0]["image"], raw)
        self.assertEqual(edits[0]["instruction"], "Make the lock plate gold. Keep everything else.")
        self.assertEqual(fake.calls, [])
        self.assertEqual(written, ["assets/chest.png"])

    def test_the_edit_is_offered_only_with_an_edit_model_and_a_previous_render(self) -> None:
        orch, root, run_id, _fake, _store, _edits = self.setup()
        sent = self.replying(orch, "PROMPT: x")
        orch._render_attempt(run_id, ticket(), "", [])
        self.assertNotIn("EDIT:", sent[0])  # no previous render yet
        self.keep_render(root, run_id, 1, PNG)
        t = ticket()
        t.attempts = 2
        orch._render_attempt(run_id, t, "", [])
        self.assertIn("EDIT:", sent[1])

    def test_without_an_edit_model_an_edit_reply_is_not_rendered(self) -> None:
        orch, root, run_id, fake, _store, edits = self.setup(edit_model="")
        self.keep_render(root, run_id, 1, PNG)
        t = ticket()
        t.attempts = 2
        self.replying(orch, "EDIT: make it gold")
        _written, refused = orch._render_attempt(run_id, t, "", [])
        self.assertIsNotNone(refused)
        self.assertIn("PROMPT:", refused.detail)
        self.assertEqual((edits, fake.calls), ([], []))

    def test_the_same_issue_three_times_running_parks_without_a_render(self) -> None:
        orch, _root, run_id, fake, _store, _edits = self.setup()
        sent = self.replying(orch, "PROMPT: x")
        rejections = [f"REJECT\nattempt {i}\nISSUES: silver-lock, other-{i}" for i in range(3)]
        _written, refused = orch._render_attempt(run_id, ticket(), "", rejections)
        self.assertTrue(refused.blocked)
        self.assertIn("silver-lock", refused.detail)
        self.assertEqual((sent, fake.calls), ([], []))

    def test_changing_issues_do_not_park(self) -> None:
        orch, _root, run_id, fake, _store, _edits = self.setup()
        self.replying(orch, "PROMPT: x")
        rejections = ["ISSUES: gold-wood", "ISSUES: too-small", "ISSUES: silver-lock"]
        _written, refused = orch._render_attempt(run_id, ticket(), "", rejections)
        self.assertIsNone(refused)
        self.assertEqual(len(fake.calls), 1)

    def test_a_stall_window_of_zero_never_parks(self) -> None:
        orch, _root, run_id, fake, _store, _edits = self.setup(stall=0)
        self.replying(orch, "PROMPT: x")
        _written, refused = orch._render_attempt(run_id, ticket(), "", ["ISSUES: a"] * 5)
        self.assertIsNone(refused)

    def test_the_image_reviewer_is_asked_for_issues_and_mode(self) -> None:
        orch, _root, run_id, _fake, _store, _edits = self.setup()
        seen: list[dict] = []
        replies = [
            "PROMPT: pixel art, a chest",
            "tests/chest_test.py\n```python\ndef test_a():\n    assert True\n```",
            "ACCEPT",
        ]

        def call(*args, **kwargs):
            seen.append({"role": args[1], "messages": args[2]})
            return Completion(text=replies[min(len(seen) - 1, 2)], usage=Usage(),
                              finish_reason="stop")

        orch._call = call
        orch._attempt(run_id, ticket(), "")
        review = next(c for c in seen if c["role"] == "reviewer")
        note = review["messages"][-1].text
        self.assertIn("ISSUES:", note)
        self.assertIn("MODE: edit", note)

    def test_issue_names_are_normalised(self) -> None:
        from forge.prompts import review_issues

        self.assertEqual(
            review_issues("REJECT\n**ISSUES:** Gold Wood, silver_lock\nMODE: edit"),
            {"gold-wood", "silver-lock"},
        )


class EditConfigTests(unittest.TestCase):
    def test_an_edit_model_without_an_edit_workflow_is_refused(self) -> None:
        import json
        import os

        from forge.config import ConfigError

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".hybridforge" / "comfy").mkdir(parents=True)
            graph = {"6": {"class_type": "CLIPTextEncode", "inputs": {"text": "x"}},
                     "9": {"class_type": "SaveImage", "inputs": {}}}
            (root / ".hybridforge" / "comfy" / "g.json").write_text(json.dumps(graph))
            body = {
                "models": {"m": TEXT},
                "roles": {r: "m" for r in ("planner", "executor", "tester", "reviewer")},
                "imageModels": {"pixel": {"kind": "comfyui", "url": "http://127.0.0.1:1",
                                          "workflows": {"generate": ".hybridforge/comfy/g.json"},
                                          "bind": {"prompt": ["6", "text"]}}},
                "loop": {"editModel": "pixel"},
            }
            (root / ".hybridforge" / "config.json").write_text(json.dumps(body))
            with unittest.mock.patch.dict(os.environ, {"FORGE_PROFILE": str(root / "p.json")}):
                with self.assertRaises(ConfigError) as caught:
                    Config.load(root)
        self.assertIn("no `edit` workflow", str(caught.exception))
