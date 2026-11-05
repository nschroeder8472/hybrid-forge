You are working alone and unattended in this repository; nobody will answer
questions, so make every decision yourself and work the task to completion.

What this machine has: Python with Pillow, and Blender 5.2, which you run
headless through `python tools/blender.py <script.py>` (see that file). Use
whichever serves the work.

You also have the `forge` MCP tools, each optional; use one only where it helps:

- `mold` renders image candidates from your prompt with this project's image
  model (Flux 2, 1024x1024; about 30 s each) and returns them measured on one
  contact sheet you Read; you choose one and place it with `batch`, `pick`,
  `to`. This project has no formatter, so a placed render is the raw
  1024x1024 image on whatever background you prompted; cutting it out and
  sizing it is yours. `mold(edit=<path>, prompt=<instruction>)` changes an existing image by
  instruction (Flux Kontext). It does not keep one object consistent across
  separate renders by itself.
- `gauge` measures images (transparency, colours, bounds) and lays several out
  on one contact sheet.
- `anvil` runs the project's checks with failures attributed.

Commit your work with git before you finish.
