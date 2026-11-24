import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const view = readFileSync(
  new URL("../src/components/WorkflowStudioView.vue", import.meta.url),
  "utf8",
);

test("node insertion repairs the draft instead of refusing to act", () => {
  assert.match(view, /function repairSkeleton/);
  assert.match(view, /added the missing end node and connected it/);
  assert.match(view, /merged .*extra end node\(s\) into one/);
  assert.doesNotMatch(view, /exactly one end node/);
});

test("node insertion is blocked only when no draft is selected", () => {
  assert.match(view, /Select or create a workflow draft before inserting nodes\./);
  assert.match(view, /:disabled="!selectedWorkflowId"/);
});

test("branches that end on a condition cannot receive a pre-end insertion", () => {
  assert.match(view, /insert the node on one branch instead of before the end/);
});

test("run failures surface the task error, not just the queue id", () => {
  assert.match(view, /run\.task_error/);
  assert.match(view, /The worker starts it within a second/);
});
