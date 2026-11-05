// The reference test: how a test in this project is written. node:test and
// node:assert/strict, ES modules, one behaviour per test.
import { test } from "node:test";
import assert from "node:assert/strict";

test("arrays compare by value", () => {
  assert.deepEqual([1, 2, 3].map((n) => n * 2), [2, 4, 6]);
});
