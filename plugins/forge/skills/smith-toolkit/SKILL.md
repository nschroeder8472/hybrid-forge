---
name: smith-toolkit
description: Use when the forge MCP tools (anvil, gauge, mold) are available and the work needs art Claude cannot draw well from code — painted icons, illustrations, textures — or needs failures attributed against a baseline, or images measured instead of judged by eye. Says when each tool has paid off in measurement and when it has not.
---

# The smith's tools

You do the work. These tools each make one motion faster or more precise;
none decides anything, and using none is often right. What follows is what
the benches in `bench/` measured, so pick a tool up for the reasons it has
earned, not by default.

## mold — when it pays

Use it for **single, painted, detailed images**: item icons, portraits,
illustrations, textures. On six painted RPG icons it was preferred blind in
six of seven rounds over Opus drawing with code and Blender, at a third of the
cost.

Skip it when:

- **one object must stay identical across many views** (a sprite sheet of a
  turning prop): separate renders do not keep shape and decoration consistent.
  Model it in Blender and render every angle instead;
- **the style is clean and graphic** (chalk marks, flat UI art): Opus drawing
  with Pillow was preferred 6-0 over Flux on a chalkboard game.

How to use it well: write a specific prompt (subject, material, lighting,
background, framing), cast 4-6 candidates, Read the one contact sheet, place
the best with `batch`, `pick`, `to`. Without a project formatter the render
comes back raw at 1024x1024 on the background you asked for; cut it out and
size it yourself. `edit` changes an existing render by instruction.

## gauge

Measure before judging: transparency, opaque share and box, colours, where
drawn lines are. `sheet: true` puts several images on one page so you look
once instead of reading each.

## anvil

`mark: true` once before you edit, then call it to see which failures are
yours, which were already there, and which never reached the code. Cheap; it
has not been shown to change outcomes, so treat it as a convenience.
