"""Prompts that carry an image, and the four things that must not change.

A reviewer that cannot see the image is not a reviewer — which is true of a
screenshot attached to an ordinary code ticket, not only of the image tickets
`docs/IMAGE-LOOP.md` is about. `Message.content` was typed `str` and every
adapter formatted it as one, so there was nowhere to put the picture.

What this module holds to:

1. **A string still means what it meant.** Every prompt in the loop builds one,
   and each adapter must produce the request body it produced before — the
   parts form exists for the one thing a string cannot carry.
2. **A model that cannot see is not asked to.** Refused before the request,
   loudly, rather than by dropping the image out of a question the model will
   answer anyway.
3. **The budget gate prices the image.** `base.py` says the gate "is only as
   good as this number", and an image priced at zero is several thousand
   tokens it believes it has room for.
4. **The record keeps the digest, not the bytes.** An artifact directory is
   read by eye; megabytes of base64 in it is a record nobody can read.
"""

from __future__ import annotations

import base64
import json
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forge.artifacts import Artifacts  # noqa: E402
from forge.providers.anthropic_api import AnthropicProvider  # noqa: E402
from forge.providers.anthropic_api import _turn as anthropic_turn  # noqa: E402
from forge.providers.base import (  # noqa: E402
    Capabilities,
    ImagePart,
    Message,
    Provider,
    ProviderCannotSee,
    TextPart,
    split_system,
)
from forge.providers.claude_cli import ClaudeCLIProvider  # noqa: E402
from forge.providers.gemini import GeminiProvider  # noqa: E402
from forge.providers.gemini import _parts as gemini_parts  # noqa: E402
from forge.providers.llamacpp import LlamaCppProvider  # noqa: E402
from forge.providers.openai_compat import OpenAICompatProvider  # noqa: E402
from forge.providers.openai_compat import _turn as openai_turn  # noqa: E402
from forge.ratify import _prompt_digest  # noqa: E402
from forge.tokens import estimate_messages, estimate_text  # noqa: E402

# Not a valid PNG, and it does not need to be: nothing in the loop decodes an
# image. What matters is that the bytes are bytes — non-UTF-8, so anything that
# tries to treat them as text fails loudly here rather than in a request body.
PIXELS = b"\x89PNG\r\n\x1a\n\xff\xfe\x00\x01binary"


def image(**overrides) -> ImagePart:
    return ImagePart(media_type="image/png", data=PIXELS, **overrides)


def with_image(text: str = "does this match the criteria?") -> Message:
    return Message(role="user", content=[TextPart(text), image()])


class TestAStringIsStillAString(unittest.TestCase):
    """The regression this change is most likely to cause. Every prompt the
    loop builds is a string, so if the parts form leaked into their request
    bodies, every adapter would be sending a shape it has never sent."""

    def setUp(self):
        self.message = Message(role="user", content="hi")

    def test_it_reads_as_one_text_part(self):
        self.assertEqual(self.message.parts, [TextPart("hi")])
        self.assertEqual(self.message.text, "hi")
        self.assertEqual(self.message.images, [])

    def test_anthropic_sends_the_bare_string(self):
        self.assertEqual(
            anthropic_turn(self.message), {"role": "user", "content": "hi"}
        )

    def test_the_openai_shape_sends_the_bare_string(self):
        self.assertEqual(openai_turn(self.message), {"role": "user", "content": "hi"})

    def test_gemini_sends_the_one_text_part_it_always_did(self):
        self.assertEqual(gemini_parts(self.message), [{"text": "hi"}])

    def test_the_estimate_is_the_number_it_was(self):
        # The old estimator was `estimate_text(content) + overhead` per message.
        messages = [Message(role="user", content="x" * 400)] * 3

        self.assertEqual(
            estimate_messages(messages), (estimate_text("x" * 400) + 4) * 3
        )

    def test_a_system_message_still_splits_out(self):
        system, turns = split_system(
            [Message(role="system", content="rules"), self.message]
        )

        self.assertEqual(system, "rules")
        self.assertEqual(turns, [self.message])


class TestAnImageReachesEachBackendInItsOwnShape(unittest.TestCase):
    """Three wire formats for the same part. Each is checked against the bytes
    rather than against a fixture of itself: a base64 that does not decode back
    to the image is a request that will be refused by the API, and a `str()` of
    the bytes would pass a shape check while sending garbage."""

    def setUp(self):
        self.message = with_image()
        self.encoded = base64.b64encode(PIXELS).decode("ascii")

    def test_anthropic_sends_a_base64_image_block(self):
        blocks = anthropic_turn(self.message)["content"]

        self.assertEqual(blocks[0], {"type": "text", "text": self.message.text})
        self.assertEqual(
            blocks[1],
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": self.encoded,
                },
            },
        )

    def test_gemini_sends_inline_data(self):
        parts = gemini_parts(self.message)

        self.assertEqual(
            parts[1],
            {"inline_data": {"mime_type": "image/png", "data": self.encoded}},
        )

    def test_the_openai_shape_sends_a_data_url(self):
        # A `data:` URL rather than a link: the file exists only on the daemon,
        # so a server told to fetch one would be reaching for something it
        # cannot see.
        blocks = openai_turn(self.message)["content"]

        self.assertEqual(blocks[0], {"type": "text", "text": self.message.text})
        self.assertEqual(
            blocks[1]["image_url"]["url"], f"data:image/png;base64,{self.encoded}"
        )

    def test_every_encoding_decodes_back_to_the_image(self):
        url = openai_turn(self.message)["content"][1]["image_url"]["url"]
        sent = (
            anthropic_turn(self.message)["content"][1]["source"]["data"],
            gemini_parts(self.message)[1]["inline_data"]["data"],
            url.split(",", 1)[1],
        )

        for encoded in sent:
            self.assertEqual(base64.b64decode(encoded), PIXELS)


class TestAModelThatCannotSeeIsNotAskedTo(unittest.TestCase):
    """Refused before the request, not after. A blind reviewer handed a prompt
    with the image dropped answers the question anyway, confidently, about
    nothing."""

    def _blind(self, **config) -> OpenAICompatProvider:
        return OpenAICompatProvider(
            "local",
            {
                "baseUrl": "http://x:11434/v1",
                "model": "text-only",
                "contextWindow": 32768,
                "maxOutputTokens": 4096,
                **config,
            },
        )

    def test_an_openai_compatible_endpoint_is_blind_until_told_otherwise(self):
        # "OpenAI-compatible" is a claim about the request shape, not about the
        # model behind it, and nothing here can ask an endpoint whether it sees.
        self.assertFalse(self._blind().capabilities().supports_images)
        self.assertTrue(
            self._blind(multimodal=True).capabilities().supports_images
        )

    def test_a_local_checkpoint_declares_it_per_checkpoint(self):
        # The same key `presets` reads to decide whether to write
        # `mmproj-auto = false`: a projector costs VRAM no text-only role uses.
        def provider(**config):
            return LlamaCppProvider(
                "local",
                {"model": "q", "contextWindow": 8192, **config},
            )

        self.assertFalse(provider().capabilities().supports_images)
        self.assertTrue(provider(multimodal=True).capabilities().supports_images)

    def test_the_cli_cannot_be_shown_one_at_all(self):
        # Its prompt is text on stdin, and it defaults to no tools — so naming
        # a path instead would be a reviewer ruling on a filename.
        provider = ClaudeCLIProvider("cli", {})

        self.assertFalse(provider.capabilities().supports_images)

    def test_the_request_is_never_made(self):
        provider = self._blind()
        mod = sys.modules["forge.providers.openai_compat"]

        def refuse(*_args, **_kwargs):
            raise AssertionError("the request was sent to a model that cannot see")

        with unittest.mock.patch.object(mod, "post_json", refuse):
            with self.assertRaises(ProviderCannotSee) as caught:
                provider.complete([with_image()], max_tokens=64)

        self.assertIn("cannot see", str(caught.exception))

    def test_the_refusal_names_the_role_and_the_model(self):
        with self.assertRaises(ProviderCannotSee) as caught:
            self._blind().complete([with_image()], max_tokens=64)

        message = str(caught.exception)
        self.assertIn("local", message)
        self.assertIn("text-only", message)

    def test_a_text_prompt_to_a_blind_model_is_untouched(self):
        provider = self._blind()
        sent: list[dict] = []
        mod = sys.modules["forge.providers.openai_compat"]
        payload = {
            "choices": [
                {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
            ]
        }

        def capture(_url, body, **_kwargs):
            sent.append(body)
            return payload

        with unittest.mock.patch.object(mod, "post_json", capture):
            provider.complete([Message(role="user", content="hi")], max_tokens=64)

        self.assertEqual(sent[0]["messages"], [{"role": "user", "content": "hi"}])

    def test_a_cloud_model_that_sees_is_not_refused(self):
        provider = AnthropicProvider(
            "cloud",
            {
                "apiKey": "k",
                "model": "claude-opus-5",
                "contextWindow": 200_000,
                "maxOutputTokens": 8192,
            },
        )

        self.assertTrue(provider.capabilities().supports_images)

    def test_but_it_can_be_told_it_does_not(self):
        # For a future model that cannot, and for an operator who would rather
        # have the refusal than the bill.
        provider = AnthropicProvider(
            "cloud",
            {
                "apiKey": "k",
                "model": "claude-opus-5",
                "contextWindow": 200_000,
                "maxOutputTokens": 8192,
                "vision": False,
            },
        )

        self.assertFalse(provider.capabilities().supports_images)

    def test_gemini_sees_unless_told_otherwise(self):
        def provider(**config):
            return GeminiProvider("g", {"apiKey": "k", "model": "gemini-2.5-pro", **config})

        self.assertTrue(provider().capabilities().supports_images)
        self.assertFalse(provider(vision=False).capabilities().supports_images)


class TestTheBudgetGatePricesAnImage(unittest.TestCase):
    """An image priced at zero is not a rounding error. A 2400x1000 screenshot
    is several thousand tokens the gate believes it has room for, and the gate's
    whole job is deciding whether a prompt fits before anything is spent."""

    class _Model(Provider):
        kind = "test"

        def capabilities(self) -> Capabilities:
            return Capabilities(context_window=8192, max_output_tokens=1024)

        def complete(self, messages, **kwargs):  # pragma: no cover - never called
            raise AssertionError("not part of this test")

    def test_a_measured_image_is_priced_by_area(self):
        # Anthropic documents roughly (width x height) / 750.
        priced = estimate_messages(
            [Message(role="user", content=[image(width=1500, height=1000)])]
        )

        self.assertAlmostEqual(priced, int(1500 * 1000 / 750) + 1 + 4, delta=1)

    def test_an_unmeasured_image_is_priced_at_the_worst_case(self):
        # The daemon does not decode images, so an image whose dimensions
        # nobody supplied has to be charged what the largest one would cost.
        priced = estimate_messages([Message(role="user", content=[image()])])

        self.assertGreater(priced, 3000)

    def test_it_reaches_the_gate_through_count_tokens(self):
        model = self._Model("m", {})

        text_only = model.count_tokens([Message(role="user", content="hi")])
        with_picture = model.count_tokens([with_image()])

        self.assertGreater(with_picture, text_only + 1000)

    def test_text_beside_an_image_is_still_counted(self):
        message = Message(role="user", content=[TextPart("x" * 400), image()])

        self.assertGreater(
            estimate_messages([message]),
            estimate_messages([Message(role="user", content=[image()])]),
        )


class TestARecordNeverInlinesAnImage(unittest.TestCase):
    """`Artifacts` exists to be read by eye at 2am. `json.dumps(default=str)`
    would write the repr of the bytes — megabytes of `\\x89PNG` in the one
    place whose entire purpose is being readable."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.artifacts = Artifacts(self.root, 1)

    def _written(self) -> str:
        files = list((self.root / "artifacts" / "run-1").rglob("*.json"))
        self.assertEqual(len(files), 1, files)
        return files[0].read_text(encoding="utf-8")

    def test_an_image_part_is_recorded_as_its_digest(self):
        part = image(width=800, height=600, path="assets/hero.png")

        self.artifacts.record("T-1", 1, "review", {"status": "ok", "image": part})

        recorded = json.loads(self._written())["image"]
        self.assertEqual(recorded["digest"], part.digest[:16])
        self.assertEqual(recorded["bytes"], len(PIXELS))
        self.assertEqual(recorded["path"], "assets/hero.png")
        self.assertEqual((recorded["width"], recorded["height"]), (800, 600))

    def test_the_bytes_themselves_are_nowhere_in_the_file(self):
        self.artifacts.record(
            "T-1",
            1,
            "review",
            {"status": "ok", "parts": [image()], "raw_bytes": PIXELS},
        )

        written = self._written()
        self.assertNotIn(base64.b64encode(PIXELS).decode("ascii"), written)
        self.assertNotIn("PNG", written)

    def test_an_ordinary_payload_is_written_unchanged(self):
        payload = {"status": "ok", "usage": {"total_tokens": 12}, "verdict": "APPROVE"}

        self.artifacts.record("T-1", 1, "review", payload)

        recorded = json.loads(self._written())
        self.assertEqual(recorded["usage"], {"total_tokens": 12})
        self.assertEqual(recorded["verdict"], "APPROVE")


class TestAPromptFingerprintSeesThePicture(unittest.TestCase):
    """`ratify` asks "have we asked this before" by fingerprinting the prompt.
    Two prompts differing only in the image they carry are two questions, and
    hashing the text alone would answer yes to the second one."""

    def _digest(self, part: ImagePart) -> str:
        return _prompt_digest([Message(role="user", content=[TextPart("look"), part])])

    def test_the_same_image_fingerprints_the_same(self):
        self.assertEqual(self._digest(image()), self._digest(image()))

    def test_a_different_image_does_not(self):
        other = ImagePart(media_type="image/png", data=PIXELS + b"x")

        self.assertNotEqual(self._digest(image()), self._digest(other))

    def test_text_alone_still_fingerprints(self):
        digest = _prompt_digest([Message(role="user", content="look")])

        self.assertTrue(digest)
        self.assertNotEqual(digest, self._digest(image()))


if __name__ == "__main__":
    unittest.main()
