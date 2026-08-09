"""Shell out to a local binary that reads a prompt and writes a completion.

The escape hatch for anything without an HTTP server: `llama.cpp`'s CLI,
`mlx_lm.generate`, a wrapper script around a model you built yourself. If it
can read stdin and write stdout, it can be an executor here.

Config shape:

    {
      "kind": "command",
      "command": ["llama-cli", "-m", "/models/qwen.gguf", "-p", "{prompt}"],
      "promptOn": "stdin" | "argv",
      "contextWindow": 32768
    }

`{prompt}` in any argv element is substituted with the rendered prompt when
`promptOn` is "argv"; otherwise the prompt is written to stdin. The prompt
itself is never passed through a shell — argv is exec'd directly, so a spec
containing backticks or semicolons is text, not a command.
"""

from __future__ import annotations

import subprocess
from typing import Any

from .base import (
    Capabilities,
    Completion,
    Message,
    Provider,
    ProviderBadResponse,
    ProviderUnreachable,
    Usage,
)


def render_prompt(messages: list[Message]) -> str:
    """Flatten a message list into plain text for a completion-style binary.

    Local CLIs rarely accept a chat structure, so roles become labeled sections.
    Blunt, but unambiguous, and every local model has seen this shape.
    """
    parts = []
    for message in messages:
        label = {"system": "System", "user": "User", "assistant": "Assistant"}[message.role]
        parts.append(f"### {label}\n{message.content}")
    parts.append("### Assistant\n")
    return "\n\n".join(parts)


class SubprocessProvider(Provider):
    kind = "command"

    def __init__(self, name: str, config: dict[str, Any]):
        super().__init__(name, config)
        self.command: list[str] = list(config.get("command") or [])
        if not self.command:
            raise ValueError(f"provider {name!r} of kind 'command' needs a non-empty `command`")
        self.prompt_on = config.get("promptOn", "stdin")
        self.model = config.get("model") or self.command[0]

    def complete(
        self,
        messages: list[Message],
        *,
        max_tokens: int,
        temperature: float = 0.2,
        timeout: int = 600,
    ) -> Completion:
        prompt = render_prompt(messages)

        argv = list(self.command)
        stdin_data: str | None = None
        if self.prompt_on == "argv":
            argv = [a.replace("{prompt}", prompt) for a in argv]
        else:
            stdin_data = prompt

        # Placeholders for the numeric knobs, substituted only where the user
        # asked for them — every CLI spells these flags differently.
        argv = [
            a.replace("{max_tokens}", str(max_tokens)).replace("{temperature}", str(temperature))
            for a in argv
        ]

        try:
            result = subprocess.run(  # noqa: S603 - argv list, never shell=True
                argv,
                input=stdin_data,
                capture_output=True,
                text=True,
                # UTF-8 both ways regardless of the host locale — a prompt
                # containing an em dash must not fail to reach the binary, and
                # a completion containing one must not fail to come back.
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise ProviderUnreachable(f"command not found: {argv[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ProviderUnreachable(f"{argv[0]} timed out after {timeout}s") from exc

        if result.returncode != 0:
            raise ProviderBadResponse(
                f"{argv[0]} exited {result.returncode}: {(result.stderr or '')[:500]}"
            )

        text = result.stdout.strip()
        usage = Usage(
            prompt_tokens=self.count_tokens(messages),
            completion_tokens=self.count_tokens([Message(role="assistant", content=text)]),
            estimated=True,
        )
        return Completion(text=text, usage=usage, model=self.model, raw={"argv": argv})

    def capabilities(self) -> Capabilities:
        return Capabilities(
            context_window=int(self.config.get("contextWindow", 8192)),
            max_output_tokens=int(self.config.get("maxOutputTokens", 4096)),
        )
