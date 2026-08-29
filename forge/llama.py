"""Fetching the llama.cpp build this project runs on.

Forge has exactly one local backend, which makes "install llama.cpp yourself"
the first step of every setup and the first place one goes wrong. The evidence
for that is not hypothetical: the same 30B A3B MoE at Q4_K_M measured 16 tok/s
on a Vulkan build and 353 tok/s on CUDA, on the same 5090. A person following a
setup guide has no way to know they took the slow path — nothing errors, the
run just takes twenty hours.

So forge fetches a build itself, picks the backend from what the machine
actually has, and says which it picked.

**Pinned, not latest.** llama.cpp publishes several tagged builds a day
(`b10683` through `b10687` inside four hours, on the day this was written).
Tracking `latest` would mean two machines set up an hour apart run different
inference code, which makes a measurement taken on one meaningless on the
other — and measurements are how everything in this repository gets decided.
`PINNED_BUILD` moves deliberately, in a commit, with whatever was measured
against it.

**Verified, then extracted.** This downloads an executable over the network and
puts it where a later command will run it, which deserves more than TLS. The
GitHub release API publishes a SHA-256 digest per asset; the download is
compared against it before anything is unpacked, and a mismatch deletes the
file rather than quarantining it. Archive members are checked for paths that
escape the destination before extraction rather than after.

**What the naming convention encodes.** Assets are
`llama-<tag>-bin-<os>[-<backend>]-<arch>.<ext>`, and the omissions carry
meaning: macOS builds have no backend segment because Metal is in every one of
them, and a plain `ubuntu-x64` is the CPU build. Windows CUDA is the exception
that costs people an afternoon — the runtime libraries ship in a *second*
archive, `cudart-llama-bin-win-cuda-<ver>-x64.zip`, and without it the server
exits on a missing DLL rather than saying it wants CUDA.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

# The build forge is tested against. See the module docstring on why this is a
# constant rather than a lookup of `latest`.
PINNED_BUILD = "b10687"

_RELEASES = "https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/{tag}"
_USER_AGENT = "hybrid-forge"

# CUDA toolkits the project publishes Windows builds for, newest first. The
# choice is not free: Blackwell (compute capability 12.0, the 5090 and its
# siblings) needs CUDA 12.8 or newer, so a 12.4 build loads and then fails to
# find a kernel for the architecture.
_WINDOWS_CUDA = ("13.3", "12.4")
_MIN_CUDA_FOR = ((12.0, 12.8),)  # (compute capability, minimum CUDA)

# Read in 1 MiB blocks: large enough that a 400 MB archive is not 400,000
# syscalls, small enough to hash without holding the file in memory.
_CHUNK = 1024 * 1024


class LlamaError(Exception):
    """Fetching or installing a build failed in a way worth reporting."""


@dataclass(frozen=True)
class Target:
    """The machine a build is being fetched for."""

    os: str  # "win" | "macos" | "ubuntu"
    arch: str  # "x64" | "arm64"
    backend: str  # "cuda-13.3" | "metal" | "vulkan" | "cpu" | ...

    def describe(self) -> str:
        return f"{self.os}-{self.arch}, {self.backend}"


@dataclass(frozen=True)
class Asset:
    name: str
    url: str
    size: int
    # "sha256:<hex>" as the API reports it, or "" for a release that predates
    # the field. An unverifiable asset is refused rather than trusted.
    digest: str

    @property
    def sha256(self) -> str:
        prefix = "sha256:"
        return self.digest[len(prefix):] if self.digest.startswith(prefix) else ""


# -- what this machine is ------------------------------------------------


def _machine_arch() -> str:
    machine = platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        return "arm64"
    if machine in ("x86_64", "amd64", "x64"):
        return "x64"
    raise LlamaError(
        f"no llama.cpp build is published for {platform.machine()!r}. Build it "
        f"from source and put `llama-server` on PATH; forge uses whatever it "
        f"finds there when no build is installed."
    )


def _machine_os() -> str:
    if sys.platform == "win32":
        return "win"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "ubuntu"
    raise LlamaError(
        f"no llama.cpp build is published for {sys.platform!r}. Build it from "
        f"source and put `llama-server` on PATH."
    )


def compute_capability() -> float | None:
    """The NVIDIA compute capability of GPU 0, or None if there is no NVIDIA GPU.

    Asked of `nvidia-smi` rather than derived from a card name, because the
    name-to-architecture table is exactly the kind of thing that is wrong for
    every GPU released after it was written.
    """
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=compute_cap", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    first = (out.stdout or "").strip().splitlines()
    try:
        return float(first[0].strip())
    except (IndexError, ValueError):
        return None


def _cuda_for(capability: float) -> str:
    """The newest published CUDA build this GPU can use, oldest acceptable last.

    Blackwell is the case that matters. Compute capability 12.0 needs CUDA
    12.8+, so of the two Windows builds only 13.3 works — and the failure with
    12.4 is a missing-kernel error at load, not a refusal to install.
    """
    minimum = 0.0
    for cap, needs in _MIN_CUDA_FOR:
        if capability >= cap:
            minimum = max(minimum, needs)
    for version in _WINDOWS_CUDA:
        if float(version) >= minimum:
            return f"cuda-{version}"
    raise LlamaError(
        f"a GPU with compute capability {capability} needs CUDA {minimum} or "
        f"newer, and the published Windows builds are "
        f"{', '.join(_WINDOWS_CUDA)}. Build from source against a newer toolkit."
    )


def detect(backend: str = "") -> Target:
    """What to fetch for this machine, or what the caller asked for instead.

    An explicit `backend` is taken as given — someone who has measured their
    own box beats a rule of thumb, and this cannot see a passthrough GPU or a
    driver that is about to be replaced.
    """
    host_os, arch = _machine_os(), _machine_arch()
    if backend:
        return Target(os=host_os, arch=arch, backend=backend)

    if host_os == "macos":
        # Every macOS build carries Metal. There is no other choice to make.
        return Target(os=host_os, arch=arch, backend="metal")

    capability = compute_capability()
    if capability is not None:
        if host_os == "win":
            return Target(os=host_os, arch=arch, backend=_cuda_for(capability))
        # Linux publishes no CUDA archive — only ROCm, SYCL, Vulkan and CPU —
        # so an NVIDIA box either builds from source or takes Vulkan and the
        # throughput that comes with it.
        return Target(os=host_os, arch=arch, backend="vulkan")

    return Target(os=host_os, arch=arch, backend="cpu")


# -- naming ---------------------------------------------------------------


def asset_name(tag: str, target: Target) -> str:
    """The release asset for this target, by the convention in the docstring."""
    suffix = "zip" if target.os == "win" else "tar.gz"
    if target.os == "macos":
        # Metal is in every macOS build, so there is no backend segment.
        return f"llama-{tag}-bin-macos-{target.arch}.{suffix}"
    if target.os == "ubuntu" and target.backend == "cpu":
        # A plain ubuntu build is the CPU one; there is no `-cpu-` spelling.
        return f"llama-{tag}-bin-ubuntu-{target.arch}.{suffix}"
    return f"llama-{tag}-bin-{target.os}-{target.backend}-{target.arch}.{suffix}"


def runtime_asset_name(tag: str, target: Target) -> str:
    """The CUDA runtime archive a Windows CUDA build needs beside it, or "".

    Without it `llama-server.exe` exits on a missing `cudart64_*.dll` and says
    nothing about CUDA, which is a long way to walk from the symptom.
    """
    if target.os != "win" or not target.backend.startswith("cuda-"):
        return ""
    version = target.backend[len("cuda-"):]
    return f"cudart-llama-bin-win-cuda-{version}-{target.arch}.zip"


# -- the release ----------------------------------------------------------


def release(tag: str, *, timeout: int = 30) -> dict:
    request = urllib.request.Request(
        _RELEASES.format(tag=tag),
        headers={"User-Agent": _USER_AGENT, "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise LlamaError(
                f"llama.cpp has no release tagged {tag!r}. Builds are tagged "
                f"`b<number>` — check the tag against "
                f"https://github.com/ggml-org/llama.cpp/releases."
            ) from exc
        raise LlamaError(f"GitHub returned {exc.code} for release {tag!r}") from exc
    except Exception as exc:  # noqa: BLE001 - reported with the tag that failed
        raise LlamaError(f"could not read release {tag!r}: {exc}") from exc


def resolve(tag: str, target: Target, *, timeout: int = 30) -> list[Asset]:
    """Every archive needed for this target, the server build first.

    Returns more than one only on Windows CUDA, where the runtime is separate.
    """
    data = release(tag, timeout=timeout)
    by_name = {a.get("name"): a for a in data.get("assets", [])}

    wanted = [asset_name(tag, target)]
    runtime = runtime_asset_name(tag, target)
    if runtime:
        wanted.append(runtime)

    found: list[Asset] = []
    for name in wanted:
        entry = by_name.get(name)
        if entry is None:
            have = sorted(n for n in by_name if n.startswith(f"llama-{tag}-bin-"))
            raise LlamaError(
                f"release {tag} has no asset {name!r} for {target.describe()}. "
                f"It publishes: {', '.join(have) or 'nothing matching'}."
            )
        found.append(Asset(
            name=name,
            url=entry.get("browser_download_url", ""),
            size=int(entry.get("size") or 0),
            digest=str(entry.get("digest") or ""),
        ))
    return found


# -- fetching -------------------------------------------------------------


def _download(asset: Asset, into: Path, *, timeout: int = 300) -> Path:
    """Fetch one asset and prove it is the one the API described.

    The digest is checked before the file is used for anything, and a mismatch
    removes it. Keeping a failed download around invites someone to unpack it
    by hand to see what went wrong, which is the one thing that must not happen
    to an archive of executables that arrived corrupted or substituted.
    """
    if not asset.sha256:
        raise LlamaError(
            f"the GitHub API published no SHA-256 for {asset.name!r}, so the "
            f"download cannot be verified. Refusing to install an unverifiable "
            f"executable; fetch it by hand if you have checked it yourself."
        )

    into.mkdir(parents=True, exist_ok=True)
    path = into / asset.name
    digest = hashlib.sha256()
    request = urllib.request.Request(asset.url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response, \
                open(path, "wb") as handle:
            while True:
                block = response.read(_CHUNK)
                if not block:
                    break
                digest.update(block)
                handle.write(block)
    except Exception as exc:  # noqa: BLE001
        path.unlink(missing_ok=True)
        raise LlamaError(f"could not download {asset.name}: {exc}") from exc

    got = digest.hexdigest()
    if got != asset.sha256:
        path.unlink(missing_ok=True)
        raise LlamaError(
            f"{asset.name} does not match the SHA-256 GitHub published for it "
            f"(expected {asset.sha256}, got {got}). The download has been "
            f"deleted. Retry; if it happens twice, something between you and "
            f"GitHub is rewriting the file."
        )
    return path


def _safe_members(names: list[str], destination: Path) -> None:
    """Refuse an archive that would write outside `destination`.

    Checked before extraction rather than cleaned up after: by the time a
    traversal has been noticed on disk it has already overwritten whatever it
    was aimed at.
    """
    root = destination.resolve()
    for name in names:
        if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
            raise LlamaError(f"archive member {name!r} is an absolute path; refusing")
        target = (root / name).resolve()
        if root != target and root not in target.parents:
            raise LlamaError(
                f"archive member {name!r} resolves outside the install "
                f"directory; refusing to extract it"
            )


def _extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as bundle:
            _safe_members(bundle.namelist(), destination)
            bundle.extractall(destination)
        return
    with tarfile.open(archive) as bundle:
        _safe_members(bundle.getnames(), destination)
        # `filter="data"` drops device nodes, setuid bits and absolute links.
        # The member check above already refuses traversal; this is the second
        # lock on the same door.
        bundle.extractall(destination, filter="data")


def server_binary(root: Path) -> Path | None:
    """`llama-server` inside an install, wherever the archive chose to put it.

    The layout differs by platform and has moved between builds — sometimes at
    the top level, sometimes under `build/bin`. Searching is cheaper than
    tracking it.
    """
    name = "llama-server.exe" if sys.platform == "win32" else "llama-server"
    direct = root / name
    if direct.is_file():
        return direct
    for found in root.rglob(name):
        if found.is_file():
            return found
    return None


def install_root(base: Path | None = None) -> Path:
    """Where builds live. One directory per tag, so switching does not clobber."""
    if base is not None:
        return base
    env = os.environ.get("FORGE_LLAMA_HOME")
    if env:
        return Path(env)
    if sys.platform == "win32":
        appdata = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return Path(appdata) / "hybrid-forge" / "llama"
    return Path(os.path.expanduser("~")) / ".cache" / "hybrid-forge" / "llama"


def installed(base: Path | None = None) -> dict[str, Path]:
    """Every build already fetched, tag -> path to its `llama-server`."""
    root = install_root(base)
    if not root.is_dir():
        return {}
    found = {}
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        binary = server_binary(entry)
        if binary is not None:
            found[entry.name] = binary
    return found


def install(
    tag: str = PINNED_BUILD,
    *,
    backend: str = "",
    base: Path | None = None,
    force: bool = False,
    timeout: int = 600,
) -> tuple[Path, Target]:
    """Fetch, verify and unpack a build. Returns the server binary and target.

    Idempotent: an install that is already present and has a `llama-server` in
    it is returned as it stands unless `force` says otherwise. Re-downloading
    400 MB to discover it is the same 400 MB is not a useful default.
    """
    target = detect(backend)
    root = install_root(base) / tag

    if not force:
        existing = server_binary(root)
        if existing is not None:
            return existing, target

    assets = resolve(tag, target, timeout=min(timeout, 60))
    staging = root.parent / f".{tag}.download"
    if force and root.exists():
        shutil.rmtree(root, ignore_errors=True)
    try:
        for asset in assets:
            archive = _download(asset, staging, timeout=timeout)
            _extract(archive, root)
            archive.unlink(missing_ok=True)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    binary = server_binary(root)
    if binary is None:
        raise LlamaError(
            f"unpacked {tag} for {target.describe()} but found no `llama-server` "
            f"in {root}. The asset layout may have changed; the files are left "
            f"there to look at."
        )
    if sys.platform != "win32":
        binary.chmod(binary.stat().st_mode | 0o111)
    return binary, target


def resolve_server(
    tag: str = PINNED_BUILD, *, base: Path | None = None
) -> tuple[Path | None, str]:
    """The `llama-server` to use, and where it came from.

    A build forge installed wins over one on PATH: it is the one the pin
    describes and the one a measurement was taken against. PATH is the fallback
    rather than an error, because someone who built from source for a backend
    that is not published has done the right thing and should not be told to
    undo it.
    """
    fetched = installed(base).get(tag)
    if fetched is not None:
        return fetched, f"installed build {tag}"
    on_path = shutil.which("llama-server")
    if on_path:
        return Path(on_path), "llama-server on PATH"
    return None, "not found"


def build_of(binary: Path) -> str:
    """The build number a binary reports, or "" if it will not say.

    `llama-server --version` writes

        version: 0.3.0-dev (build 10666, commit 4e97ac86e)

    to stderr. The number after `build` is the release tag without its `b`; the
    semantic version in front of it is not — reading the first integer on the
    line gets `0` from `0.3.0-dev`, which is a plausible-looking wrong answer.
    """
    try:
        out = subprocess.run(
            [str(binary), "--version"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    match = re.search(r"\bbuild\s+(\d+)", (out.stderr or "") + (out.stdout or ""))
    return f"b{match.group(1)}" if match else ""
