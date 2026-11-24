import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const view = readFileSync(
  new URL("../src/components/WorkflowStudioView.vue", import.meta.url),
  "utf8",
);
const api = readFileSync(new URL("../src/services/api.ts", import.meta.url), "utf8");

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

test("converging branches get one node per branch, never a shared guarded join", () => {
  assert.match(view, /One node was inserted on each of the/);
  assert.doesNotMatch(view, /join = conditionalParents\.length > 0/);
  assert.match(view, /insertedIds/);
});

test("run failures surface the task error, not just the queue id", () => {
  assert.match(view, /run\.task_error/);
  assert.match(view, /The worker starts it within a second/);
});

test("node references come from the scoped catalog, not hand-typed ids", () => {
  assert.match(api, /export function getWorkflowCatalog/);
  assert.match(api, /workflow-catalog/);
  assert.match(view, /loadCatalog\(\)/);
  assert.match(view, /catalog\.models/);
  assert.match(view, /catalog\.knowledge_bases/);
  assert.match(view, /catalog\.tools/);
  assert.match(view, /catalog\.agents/);
  assert.doesNotMatch(view, /Paste the tool_id/);
});
