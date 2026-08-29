# Shipping the inference server

Forge has one local backend. That makes "install llama.cpp" the first step of
every setup and the first place one goes wrong — quietly, which is the part
that matters.

The evidence is from this project's own hardware. The same 30B A3B MoE at
Q4_K_M, on the same RTX 5090:

| build | throughput |
|---|---|
| Vulkan | 16 tok/s |
| CUDA 13.3 | 353 tok/s |

Nothing reports the slow path. The server starts, the models answer, `forge
doctor` is green, the tickets pass. The run simply takes twenty hours instead
of one, and the person who followed the setup guide has no way to know they
took the wrong branch of it. A setup step that can be silently wrong by a
factor of 22 is a step forge should not be delegating to prose.

So `forge llama install` fetches a build, picks the backend from what the
machine actually has, and says which it picked.

---

## What ships today

`forge/llama.py` and `forge llama`. The acquisition half is complete and
tested; §"What is not done yet" is the rest.

```
forge llama                what this machine has, and what it should have
forge llama install        fetch the pinned build for this machine
forge llama list           what has already been fetched
```

```
$ forge llama
machine:  win-x64, cuda-13.3
nvidia:   compute capability 12.0
server:   C:\...\WinGet\Packages\ggml.llamacpp...\llama-server.EXE
          b10666, llama-server on PATH
pinned:   b10687

Note: this is b10666, not the pinned b10687. Numbers measured against one
build do not carry to another; `forge llama install` fetches the pinned one
alongside it.
```

### Pinned, not latest

llama.cpp publishes several tagged builds a day — `b10683` through `b10687`
inside four hours, on the day this was written. Tracking `latest` would mean
two machines set up an hour apart run different inference code, which makes a
measurement taken on one meaningless on the other.

That is not a hypothetical cost here. Every loop and prompt decision in this
repository is settled by replaying artifacts and quoting counts, and a count is
only evidence if the thing that produced it is identified. `PINNED_BUILD` moves
deliberately, in a commit, alongside whatever was measured against it.
`--build bXXXXX` overrides for a one-off.

### The backend is chosen, not guessed at

Detection reads the GPU's **compute capability** from `nvidia-smi`, not the
card's name. A name-to-architecture table is exactly the kind of thing that is
wrong for every GPU released after it was written.

| machine | picks | why |
|---|---|---|
| NVIDIA, compute ≥ 12.0, Windows | `cuda-13.3` | Blackwell needs CUDA 12.8+; the 12.4 build loads and then finds no kernel for the architecture |
| NVIDIA, older, Windows | newest published CUDA | nothing requires the older toolkit |
| NVIDIA, Linux | `vulkan` | the project publishes ROCm, SYCL, Vulkan and CPU for Linux — no CUDA archive exists to fetch |
| Apple Silicon or Intel Mac | `metal` | every macOS build carries Metal; there is no second option |
| no NVIDIA GPU | `cpu` | a CUDA build that cannot load is worse than a slow one that can |

`--backend` overrides all of it. Detection cannot see a passthrough GPU or a
driver about to be replaced, and someone who has measured their own box beats a
rule of thumb.

### Verified before it is unpacked

This downloads an executable over the network and puts it where a later command
will run it. TLS says the bytes came from GitHub; it does not say they are the
bytes GitHub described.

The release API publishes a SHA-256 per asset. The download is hashed as it
streams and compared before anything is unpacked. A mismatch **deletes** the
file rather than leaving it — a failed download invites someone to unpack it by
hand to see what went wrong, and that is the one thing that must not happen to
an archive of executables that arrived corrupted or substituted. An asset the
API published no digest for is refused rather than trusted.

Archive members are checked for absolute paths and for anything resolving
outside the destination *before* extraction, because by the time a traversal is
visible on disk it has already overwritten whatever it was aimed at. Tar
extraction additionally runs under `filter="data"`, which drops device nodes,
setuid bits and absolute links.

### The naming convention, and what its omissions mean

Assets are `llama-<tag>-bin-<os>[-<backend>]-<arch>.<ext>`, and the parts that
are missing carry as much as the parts that are there:

- **macOS builds have no backend segment** — Metal is in all of them.
- **A plain `ubuntu-x64` is the CPU build** — there is no `-cpu-` spelling.
- **Windows CUDA needs a second archive.** The runtime libraries ship
  separately as `cudart-llama-bin-win-cuda-<ver>-x64.zip`, and without it
  `llama-server.exe` exits on a missing DLL and says nothing about CUDA. That
  is a long walk from the symptom, so `resolve` returns both and `install`
  unpacks both into the same directory.

### Where it goes, and what wins

One directory per tag under `%LOCALAPPDATA%\hybrid-forge\llama` or
`~/.cache/hybrid-forge/llama`, overridable with `FORGE_LLAMA_HOME`. Per tag so
that switching builds to compare them does not clobber either.

A build forge installed wins over one on `PATH`: it is the one the pin
describes. `PATH` is the fallback rather than an error — someone who built from
source for a backend nobody publishes has done the right thing and should not
be told to undo it. `forge doctor` reports which one is behind the local models
and whether it matches the pin, because a health probe cannot see it.

---

## What is not done yet

In rough order of what would help most.

**Starting the server.** Forge writes the preset and knows where the binary is,
so `forge llama serve` is a small step from here — and it would close the last
gap where a person copies a path between two commands. The reason it is not
here yet is that the server outlives any one forge command by design: it owns
the GPU, holds checkpoints resident across runs, and is shared by every project
on the machine. Something that starts it has to decide whether it also stops
it, what happens to a run when it does, and what happens when two projects want
different presets. That is a design question, not a coding one.

**Fetching on demand.** A first `forge go` against a config with local models
and no server could offer to install one. Weighed against downloading 500 MB
because someone typed `forge go` — it should be a prompt, and a prompt needs
the answer to the previous question first.

**Offline and air-gapped installs.** `forge llama install --from <file>` over a
downloaded archive, verified against a digest recorded in the repository rather
than fetched. The verification path already exists; what is missing is a place
to keep known digests so they are reviewed in a diff rather than trusted at
download time.

**Linux CUDA.** The project publishes none, so an NVIDIA box on Linux takes
Vulkan and the throughput that comes with it — which is the 22x measured above.
Building from source in a container against the user's CUDA is the real answer
and is a much larger piece of work than anything here.

**A `--models-max` the preset can carry.** It is a server flag, not a per-model
one, so `forge models` cannot write it and the person starting the server has
to know. If forge starts the server, it can compute it: it knows every
checkpoint's file size and can read the GPU's memory.

---

## Why not the alternatives

**Vendoring a submodule and building from source.** Correct, and hours per
machine plus a CUDA toolchain. It is the right answer for the Linux CUDA gap
above and the wrong one for the common case.

**Platform wheels on PyPI.** A 500 MB wheel per platform-backend pair, of which
there are more than twenty, rebuilt on every llama.cpp release. The
distribution problem is real but it is not forge's to solve, and GitHub already
solved it.

**Leaving it to the reader.** What this replaces. It works, and it worked here
for months — at 16 tok/s, for a while, without anyone noticing.
