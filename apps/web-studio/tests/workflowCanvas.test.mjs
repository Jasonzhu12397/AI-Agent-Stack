import { test } from "node:test";
import assert from "node:assert/strict";
import {
  appendEdge,
  readNodePositions,
  removeNodePosition,
  renameNodePosition,
  writeNodePositions,
} from "../src/domain/workflowCanvas.ts";

const definition = {
  nodes: [
    { id: "start", type: "start", config: {} },
    { id: "answer", type: "llm", config: {} },
    { id: "end", type: "end", config: {} },
  ],
  edges: [{ source: "start", target: "answer" }],
};

test("persisted node positions round-trip through the draft ui block", () => {
  const written = writeNodePositions(definition, {
    start: { x: 24, y: 24 },
    answer: { x: 260.4, y: 31.6 },
  });
  assert.deepEqual(written.ui, {
    node_positions: {
      start: { x: 24, y: 24 },
      answer: { x: 260, y: 32 },
    },
  });
  assert.deepEqual(readNodePositions(written), {
    start: { x: 24, y: 24 },
    answer: { x: 260, y: 32 },
  });
});

test("stale positions for nodes outside the definition are dropped", () => {
  const written = writeNodePositions(
    { nodes: definition.nodes, edges: definition.edges },
    { removed: { x: 40, y: 40 }, start: { x: -5, y: 10 } },
  );
  assert.deepEqual(written.ui.node_positions, { start: { x: 0, y: 10 } });
  assert.deepEqual(readNodePositions(written), { start: { x: 0, y: 10 } });
});

test("coordinates survive a rename without leaking to the previous id", () => {
  const moved = renameNodePosition(
    { answer: { x: 300, y: 80 } },
    "answer",
    "answer_v2",
  );
  assert.deepEqual(moved, { answer_v2: { x: 300, y: 80 } });
  assert.equal(Object.hasOwn(moved, "answer"), false);
});

test("positions are removed with their node and an empty ui block disappears", () => {
  const positions = removeNodePosition({ start: { x: 10, y: 20 } }, "start");
  const written = writeNodePositions(definition, positions);
  assert.equal(written.ui, undefined);
});

test("drop connections append once and reject self or duplicate edges", () => {
  const added = appendEdge(definition, "answer", "end");
  assert.equal(added.ok, true);
  if (!added.ok) return;
  assert.deepEqual(added.edges, [
    { source: "start", target: "answer" },
    { source: "answer", target: "end" },
  ]);

  assert.equal(appendEdge(definition, "answer", "answer").ok, false);
  assert.equal(appendEdge(definition, "start", "answer").ok, false);
  assert.equal(appendEdge(definition, "start", "missing").ok, false);
});
