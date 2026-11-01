# Blender props

A project whose source is `bpy` scene scripts: phase 1 of
[docs/CREATIVE-TOOLS.md](../../docs/CREATIVE-TOOLS.md). The loop writes Python,
which it already knows how to do, and Blender is only the project's build: it
runs the tests and renders captures. The daemon does not import `bpy` and does
not know Blender exists.

```
art/
  scenes/common.py     helpers: clear_scene, object_from_bmesh, triangle_count, ...
  scenes/crate.py      the reference scene every other scene follows
  tests/crate_test.py  the reference test
  tests/run_checks.py  runs tests/*_test.py inside Blender, exits non-zero on failure
  tools/capture.py     renders the scenes an attempt changed into FORGE_CAPTURE_DIR
SPEC.md                three tickets: an anvil, a barrel, a workshop using both
```

## Requirements

- Blender 5.2 with `blender` on PATH. On Windows, either add
  `C:\Program Files\Blender Foundation\Blender 5.2` to PATH or replace `blender`
  in `.hybridforge/config.json` with the full quoted path.
- `flake8` for the lint step: `python -m pip install flake8`.
- Models and roles from the machine profile, and a model named `claude` that
  can see. `loop.viewModel` names it, because the captures are only worth taking
  if the seat that reviews them can see them.

## Running it

Copy this directory somewhere outside the forge checkout, and make it a git
repository. The capture script asks git which scenes changed:

```
git init && git add -A && git commit -m "start"
forge ingest SPEC.md
forge go
```

By hand, from `art/`:

```
blender -b --factory-startup --python-exit-code 1 --python tests/run_checks.py
blender -b --factory-startup --python tools/capture.py      # writes capture_out/
```

## Three things that fail silently without this layout

- **`--python-exit-code 1`.** Without it Blender exits 0 when a script raises,
  so a test file that crashed on import passes verification. `run_checks.py`
  also exits with the suite's result.
- **`clear_scene()` before `build()`.** `--factory-startup` still loads the
  default cube, camera and light. A test counting meshes counts the cube, and a
  capture renders it, and in phase 0 the cube hid the object being captured.
- **`*_test.py`, not `check_*.py`.** forge's preflight canary drops a probe file
  named the way the project names its tests, and a runner whose pattern does
  not collect it reads as a test command that does not run Python, so the run
  stops before it starts.
