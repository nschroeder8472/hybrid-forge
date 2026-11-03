// Hidden acceptance tests for TASK.md's pinned `src/game.js` and `src/ai.js`.
// Never copied into an arm's repository. Run with:
//   PROJECT=<repo> node --test acceptance/game.accept.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const root = process.env.PROJECT;
const game = await import(pathToFileURL(join(root, "src", "game.js")).href);
const ai = await import(pathToFileURL(join(root, "src", "ai.js")).href);
const { newGame, play, isOver } = game;
const { bestMove } = ai;

const at = (...moves) => moves.reduce((g, i) => play(g, i), newGame());
const throwsMessage = (fn, message) =>
  assert.throws(fn, (e) => e instanceof Error && e.message === message);

test("newGame has the pinned shape", () => {
  assert.deepEqual(newGame(), {
    board: [null, null, null, null, null, null, null, null, null],
    next: "X", winner: null, line: null, draw: false,
  });
});

test("play never mutates its argument", () => {
  const g = newGame();
  const snapshot = JSON.stringify(g);
  play(g, 4);
  assert.equal(JSON.stringify(g), snapshot);
});

test("next alternates", () => {
  assert.equal(at(4).next, "O");
  assert.equal(at(4, 0).next, "X");
});

test("a row win reports its line", () => {
  const g = at(0, 3, 1, 4, 2);
  assert.equal(g.winner, "X");
  assert.deepEqual(g.line, [0, 1, 2]);
});

test("a column win for O reports its line", () => {
  const g = at(0, 1, 3, 4, 8, 7);
  assert.equal(g.winner, "O");
  assert.deepEqual(g.line, [1, 4, 7]);
});

test("a move completing two lines reports the first in pinned order", () => {
  // X holds 1, 2, 3, 6; X plays 0 and completes [0,1,2] and [0,3,6].
  const g = at(1, 4, 2, 5, 3, 7, 6, 8, 0);
  assert.equal(g.winner, "X");
  assert.deepEqual(g.line, [0, 1, 2]);
});

test("a full board with no line is a draw", () => {
  const g = at(0, 1, 2, 4, 3, 5, 7, 6, 8);
  assert.equal(g.draw, true);
  assert.equal(g.winner, null);
  assert.equal(isOver(g), true);
});

test("isOver is false for a game in progress", () => {
  assert.equal(isOver(newGame()), false);
  assert.equal(isOver(at(4)), false);
});

test("invalid index messages", () => {
  for (const bad of [-1, 9, 1.5, "4", null]) {
    throwsMessage(() => play(newGame(), bad), "index must be an integer from 0 to 8");
  }
});

test("a taken cell is refused by its index", () => {
  throwsMessage(() => play(at(4), 4), "cell 4 is taken");
});

test("a finished game is refused before a taken cell", () => {
  throwsMessage(() => play(at(0, 3, 1, 4, 2), 0), "game is over");
  throwsMessage(() => play(at(0, 3, 1, 4, 2), 5), "game is over");
});

test("an invalid index is refused before a finished game", () => {
  throwsMessage(() => play(at(0, 3, 1, 4, 2), 9), "index must be an integer from 0 to 8");
});

test("bestMove takes an immediate win", () => {
  assert.equal(bestMove(at(0, 3, 1, 4)), 2);
});

test("bestMove blocks an immediate loss", () => {
  assert.equal(bestMove(at(0, 4, 1)), 2);
});

test("bestMove answers a corner with the centre", () => {
  assert.equal(bestMove(at(0)), 4);
});

test("bestMove is null when the game is over", () => {
  assert.equal(bestMove(at(0, 3, 1, 4, 2)), null);
});

test("bestMove does not mutate", () => {
  const g = at(0, 4);
  const snapshot = JSON.stringify(g);
  bestMove(g);
  assert.equal(JSON.stringify(g), snapshot);
});

function outcomes(aiPlays) {
  const seen = { X: 0, O: 0, draw: 0 };
  const walk = (g) => {
    if (isOver(g)) { seen[g.winner ?? "draw"] += 1; return; }
    if (g.next === aiPlays) return walk(play(g, bestMove(g)));
    for (let i = 0; i < 9; i++) if (g.board[i] === null) walk(play(g, i));
  };
  walk(newGame());
  return seen;
}

test("as O, the computer never loses to any line of play", () => {
  assert.equal(outcomes("O").X, 0);
});

test("as X, the computer never loses to any line of play", () => {
  assert.equal(outcomes("X").O, 0);
});
