"""Machine-level setup answers, remembered between repositories.

The endpoints do not change per repo. Your executor lives at one address, your
memory server at another, and the roles you want them playing are a decision
you make once — so `forge init` asks for them once and reuses them from here on
every subsequent repo.

What stays here is deliberately narrow: **models, roles, memory, and the UI
port.** What does *not* is anything the repo decides — `commands`, `room`,
`neverDelegate`. Those differ every time, and a `cargo test` carried into a
Python repo does not fail loudly. It fails `maxAttempts` times per ticket and
parks the whole backlog, which looks like a model problem and is not.

**No credentials are ever written here.** Providers already resolve keys
through `apiKeyEnv` — the *name* of an environment variable — so that name is
what gets stored. An inline `apiKey` typed into a config by hand is stripped on
the way in rather than copied to a second location the user does not know
exists.

Location follows the platform convention, so uninstalling means deleting one
predictable path:

    $FORGE_PROFILE                        explicit override, wins everywhere
    %APPDATA%\\hybrid-forge\\profile.json   Windows
    $XDG_CONFIG_HOME/hybrid-forge/…       POSIX, when set
    ~/.config/hybrid-forge/profile.json   POSIX default
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROFILE_FILE = "profile.json"
APP_DIR = "hybrid-forge"

# Never persisted, at any nesting depth. `apiKeyEnv` is the supported path and
# survives; this is the literal-secret field it exists to replace.
_SECRET_KEYS = ("apiKey", "api_key", "token", "password", "secret")


def profile_path() -> Path:
    """Where this machine's profile lives. Never creates anything."""
    override = os.environ.get("FORGE_PROFILE")
    if override:
        return Path(override).expanduser()

    if os.name == "nt":
        base = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / APP_DIR / PROFILE_FILE


def strip_secrets(block: Any) -> Any:
    """Recursively drop literal credentials, keeping `apiKeyEnv` names.

    Applied on write rather than trusting callers: the profile is a file the
    user will not think to audit, and a key copied into it silently outlives
    the config it came from.
    """
    if isinstance(block, dict):
        return {
            key: strip_secrets(value)
            for key, value in block.items()
            if key not in _SECRET_KEYS
        }
    if isinstance(block, list):
        return [strip_secrets(item) for item in block]
    return block


@dataclass
class Profile:
    """Answers worth reusing on the next repo.

    Every field is optional. A profile that only knows the executor endpoint is
    still useful — it means one fewer question next time, and the wizard simply
    asks for the rest.
    """

    models: dict[str, dict[str, Any]] = field(default_factory=dict)
    roles: dict[str, str] = field(default_factory=dict)
    memory: dict[str, Any] = field(default_factory=dict)
    ui_port: int = 8799
    # Where it was loaded from, for messages like "reusing settings from …".
    path: Path | None = None

    @property
    def is_empty(self) -> bool:
        return not self.models

    @classmethod
    def load(cls, path: Path | None = None) -> "Profile":
        """Read the profile, or return an empty one.

        A corrupt or unreadable profile is treated as absent. It holds
        preferences, not state — re-asking four questions is a far better
        outcome than refusing to initialize a repo.
        """
        path = path or profile_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls(path=path)
        if not isinstance(data, dict):
            return cls(path=path)

        return cls(
            models=data.get("models") or {},
            roles=data.get("roles") or {},
            memory=data.get("memory") or {},
            ui_port=int(data.get("ui", {}).get("port", 8799) or 8799),
            path=path,
        )

    def save(self, path: Path | None = None) -> Path:
        """Write the profile, creating its directory. Returns the path written."""
        path = path or self.path or profile_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "_comment": (
                "Machine-level Hybrid Forge defaults, reused by `forge init` in "
                "every repo on this machine. Safe to edit or delete. Per-repo "
                "settings (commands, room, neverDelegate) are not stored here. "
                "Credentials are never stored here — use apiKeyEnv."
            ),
            "models": strip_secrets(self.models),
            "roles": self.roles,
            "memory": strip_secrets(self.memory),
            "ui": {"port": self.ui_port},
        }
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        self.path = path
        return path

    # ------------------------------------------------------------------

    def describe(self) -> str:
        """One line naming what would be reused, for the wizard's banner."""
        if self.is_empty:
            return "no saved settings yet"
        roles = ", ".join(f"{role}={name}" for role, name in sorted(self.roles.items()))
        memory = self.memory.get("url") or "(none)"
        return f"models: {', '.join(sorted(self.models))} | {roles} | memory: {memory}"
