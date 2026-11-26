import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const component = readFileSync(
  new URL("../src/components/Metal3FleetView.vue", import.meta.url),
  "utf8",
);

test("the fleet console offers approved worker scaling", () => {
  assert.match(component, /label="Worker Scale"/);
  assert.match(component, /createAgentCommand\(targetAgent\.id, "cluster\.scale"/);
  assert.match(component, /target_cluster_name: deployment\.cluster_name/);
  assert.match(component, /desired_workers: desired/);
  assert.match(component, /execution_mode: scaleMode\(deployment\)/);
  assert.match(component, /machine_deployment: .*trim\(\) \|\| null/);
});

test("worker scaling is blocked when the agent is offline or the count is invalid", () => {
  assert.match(component, /!targetAgentOnline\(scope\.row\) \|\| !desiredWorkerCount\(scope\.row\)/);
  assert.match(component, /value >= 1 && value <= 2500/);
  assert.match(component, /approval is required before replicas change/);
});

test("the fleet console requests repair through the approved agent command", () => {
  assert.match(component, /Repair unhealthy worker/);
  assert.match(component, /createAgentCommand\(targetAgent\.id, "cluster\.worker\.repair"/);
  assert.match(component, /reason: "Day-2 repair from the fleet console"/);
  assert.match(component, /at most one unhealthy worker Machine per approved command/);
});
