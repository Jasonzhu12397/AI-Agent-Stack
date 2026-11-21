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

test("preflight checks surface with their name, verdict and detail", async () => {
  const { getKubernetesUpgradePreflight } = await import("../src/domain/kubernetesUpgrade.ts");
  const preflight = getKubernetesUpgradePreflight({
    preflight: {
      blocked: true,
      checks: [
        { name: "node.ready/cp-0", ok: true, detail: "Ready=True" },
        { name: "etcd.snapshot", ok: false, detail: "set KUBERNETES_UPGRADE_ETCD_SNAPSHOT" },
        { name: 42, ok: true },
        { ok: false },
      ],
    },
  });

  assert.deepEqual(preflight, {
    blocked: true,
    checks: [
      { name: "node.ready/cp-0", ok: true, detail: "Ready=True" },
      { name: "etcd.snapshot", ok: false, detail: "set KUBERNETES_UPGRADE_ETCD_SNAPSHOT" },
    ],
  });
});

test("a run without preflight evidence renders nothing", async () => {
  const { getKubernetesUpgradePreflight } = await import("../src/domain/kubernetesUpgrade.ts");

  assert.equal(getKubernetesUpgradePreflight(null), null);
  assert.equal(getKubernetesUpgradePreflight({ upgrade_steps: [] }), null);
  assert.equal(getKubernetesUpgradePreflight({ preflight: "blocked" }), null);
  assert.deepEqual(getKubernetesUpgradePreflight({ preflight: {} }), { blocked: false, checks: [] });
});
