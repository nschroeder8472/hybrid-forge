# Results — six painted item icons, 2026-11-03

| | B: Opus alone (Pillow, Blender) | D: Opus with `mold` (Flux 2) |
|---|---|---|
| Hidden checks | 16/17 | 16/17 |
| Claude cost | $3.28 | $1.05 |
| Wall clock | 20 min | 11 min |
| Renders | — | 40 |

Both missed only "helmet reads steel", a threshold too strict for painted
metal. B modelled the items in Blender and gave them a painterly finish in
post; D generated them with Flux 2 and cut them out with Pillow.

## Blind rating

One rater, blind: each icon head to head with random sides and fresh letters
per round, then the whole set.

| Round | Preferred |
|---|---|
| sword | D (Flux) |
| shield | D |
| potion | no preference |
| scroll | D |
| ring | D |
| helmet | D |
| whole set | D |

Six of seven to the image model, one tie, none to Opus alone.

## What it says

On painted single-item art, `mold` won on every axis measured: preferred in
every decided round, a third of the cost and about half the time. It is the
first and only measured case for a forge tool beating Opus alone. It sits
beside two that went the other way: chalk tic-tac-toe art, where Opus drawing
with Pillow beat Flux 6-0, and a 36-frame chest sheet, where Opus chose
Blender over the generator because separate renders cannot hold one object
consistent across views.
