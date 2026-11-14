# Task: an ornate treasure chest sprite sheet

Make a sprite sheet for a game: one ornate treasure chest, in three positions,
each seen from twelve angles. Use your own judgement for how; the pinned parts
below are what an automated check reads.

## The chest

An ornate treasure chest: dark wood, gold or brass fittings and decoration,
clearly fancier than a plain crate. Three positions:

- **closed** — lid shut;
- **ajar** — lid partially open, roughly a third of the way;
- **open** — lid fully open, and the inside visible: piles of gold coins and
  coloured gems.

It must be recognisably the same chest in every frame: same shape, same
proportions, same colours, same decoration, in all three positions and all
twelve angles.

## The angles

- **Isometric, 8:** the camera looks down at the chest at a classic isometric
  angle while the chest turns 0, 45, 90, 135, 180, 225, 270 and 315 degrees
  about its vertical axis (0 degrees: the front, where the lock is, faces the
  viewer's lower left).
- **Flat, 4:** side-on views at eye level, for side-view games — front (the
  lock facing the viewer), right side, back, left side.

## Pinned output

- `sheets/chest.png`: an RGBA PNG, exactly 1536 x 384, transparent
  background. 128 x 128 frames in a grid: rows top to bottom `closed`, `ajar`,
  `open`; columns left to right `iso_0`, `iso_45`, `iso_90`, `iso_135`,
  `iso_180`, `iso_225`, `iso_270`, `iso_315`, `flat_front`, `flat_right`,
  `flat_back`, `flat_left`. Each chest centred in its frame, the same scale in
  every frame, with a small transparent margin.
- `sheets/chest.json`: `{"frame": [128, 128], "frames": {"closed/iso_0": [x, y, 128, 128], ...}}`
  with all 36 frames.

Whatever produces the sheet stays in the repository, so it can be rebuilt.
Commit the work to git.
