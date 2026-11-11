import test from "node:test";
import assert from "node:assert/strict";
import { getKubernetesUpgradeFailure } from "../src/domain/kubernetesUpgrade.ts";

test("Kubernetes upgrade failure details include message and code", () => {
  assert.equal(
    getKubernetesUpgradeFailure({ message: "Preflight failed", code: "PREFLIGHT_FAILED" }),
    "Preflight failed (PREFLIGHT_FAILED)",
  );
});

test("Kubernetes upgrade failure details fall back to code or message", () => {
  assert.equal(getKubernetesUpgradeFailure({ code: "UPGRADE_FAILED" }), "UPGRADE_FAILED");
  assert.equal(getKubernetesUpgradeFailure({ message: "  Worker check failed  " }), "Worker check failed");
});

test("Kubernetes upgrade failure details ignore unstructured fields", () => {
  assert.equal(getKubernetesUpgradeFailure({ retryable: false }), "");
  assert.equal(getKubernetesUpgradeFailure(null), "");
});
