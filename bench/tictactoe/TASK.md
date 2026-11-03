# Task: a browser tic-tac-toe game with custom art

Build a tic-tac-toe game that runs in a browser from a static file server
(`python -m http.server` from the project root), with custom art for the X, the
O and the board. Use your own judgement for everything not pinned below; the
pinned parts are what an automated acceptance check will call.

## The look

A chalkboard: the board is dark slate with a hand-drawn 3x3 grid in white
chalk; the marks are thick chalk strokes, X in warm coral red and O in bright
teal. The art should look drawn, not like plain vector primitives, and the
pieces must sit centred in the board's drawn cells.

## Pinned interface

### Files

- `index.html` at the project root, loading `src/main.js` as an ES module.
- `assets/x.png` and `assets/o.png`: square PNGs, at least 128x128, with a
  transparent background (corner pixels alpha 0).
- `assets/board.png`: a square, fully opaque PNG, at least 512x512.

### `src/game.js` (ES module, no DOM)

- `newGame()` returns `{ board: [9 x null], next: "X", winner: null, line: null, draw: false }`.
- `play(game, index)` returns a new game (never mutates its argument) with
  `game.next` placed at `index`; sets `winner` and `line` (the first winning
  line in row, column, diagonal order: `[[0,1,2],[3,4,5],[6,7,8],[0,3,6],[1,4,7],[2,5,8],[0,4,8],[2,4,6]]`)
  or `draw`; `next` alternates. Throws `Error` with exactly:
  `index must be an integer from 0 to 8`, then `game is over`, then
  `cell <index> is taken`, checked in that order.
- `isOver(game)` returns whether there is a winner or a draw.

### `src/ai.js` (ES module, no DOM)

- `bestMove(game)` returns the index of a perfect-play move for `game.next`, or
  `null` when the game is over. Perfect play: it never loses, takes an
  immediate win when one exists, and blocks an immediate loss otherwise.

### The page

- `#board` contains exactly nine `button.cell` elements, cell 0 first, in
  row-major order. A cell holding a mark shows an `img` whose `src` ends in
  `assets/x.png` or `assets/o.png`; an empty cell has no `img`.
- `#status` shows `X to move`, `O to move`, `X wins`, `O wins` or `Draw`.
- `#new-game` (a button) starts a new game.
- `#vs-computer` (a checkbox): when checked, the computer plays O and answers
  each X move with `bestMove`, within one second.
- Clicking a cell plays it. No console errors.

## Quality bar

Tests for the rules and the computer opponent, runnable with
`node --test "tests/*.test.js"`. The project's existing reference tests must
still pass. Leave the work committed to git.
