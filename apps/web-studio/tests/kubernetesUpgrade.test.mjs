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

test("etcd health entries of a rolling upgrade are rendered from the result", async () => {
  const { getKubernetesUpgradeEtcdHealth } = await import("../src/domain/kubernetesUpgrade.ts");

  assert.deepEqual(
    getKubernetesUpgradeEtcdHealth({
      etcd_health: [
        { name: "etcd.quorum/cp-0", ok: true, detail: "healthy after 1 probe(s)" },
        { name: "etcd.quorum/cp-1", ok: false, detail: "context deadline exceeded" },
      ],
    }),
    [
      { name: "etcd.quorum/cp-0", ok: true, detail: "healthy after 1 probe(s)" },
      { name: "etcd.quorum/cp-1", ok: false, detail: "context deadline exceeded" },
    ],
  );
  assert.deepEqual(getKubernetesUpgradeEtcdHealth(null), []);
  assert.deepEqual(getKubernetesUpgradeEtcdHealth({ etcd_health: "nope" }), []);
});

test("recovery report renders node drift, etcd members and snapshots", async () => {
  const { getKubernetesRecoveryReport } = await import("../src/domain/kubernetesUpgrade.ts");
  const report = getKubernetesRecoveryReport({
    target_version: "v1.34.1",
    nodes: [
      { name: "cp-0", role: "control-plane", version: "v1.33.4", on_target: false },
      { name: "w-0", role: "worker", version: "v1.34.1", on_target: true },
    ],
    drift: 1,
    etcd: { healthy: true, members: [{ pod: "etcd-cp-0", node: "cp-0", healthy: true, detail: "ok" }] },
    snapshots: { entries: [{ name: "snapshot-pre-v1.34.1.db", bytes: 4096, taken: "2026-11-21T00:46:00Z" }] },
    warnings: [],
  });
  assert.equal(report.nodes.length, 2);
  assert.equal(report.nodes[0].role, "control-plane");
  assert.equal(report.drift, 1);
  assert.equal(report.etcdHealthy, true);
  assert.equal(report.etcdMembers[0].pod, "etcd-cp-0");
  assert.equal(report.snapshots[0].bytes, 4096);
});

test("recovery report stays null for an absent or malformed payload", async () => {
  const { getKubernetesRecoveryReport } = await import("../src/domain/kubernetesUpgrade.ts");
  assert.equal(getKubernetesRecoveryReport(null), null);
  assert.equal(getKubernetesRecoveryReport({ nodes: "nope" }), null);
});
