import test from "node:test";
import assert from "node:assert/strict";
import {
  bmhInventoryVerdict,
  ephPreflightVerdict,
  snapshotWarnings,
} from "../src/domain/preflightSnapshots.ts";

test("an inspection that never ran is informational", () => {
  assert.deepEqual(bmhInventoryVerdict(null), {
    tone: "info",
    label: "Inspection not run",
    detail: "",
  });
  assert.deepEqual(bmhInventoryVerdict({ available: false }), {
    tone: "info",
    label: "Inspection not run",
    detail: "",
  });
});

test("a run that reached the EPH but failed is not green", () => {
  const verdict = bmhInventoryVerdict({
    available: true,
    status: "failed",
    error_code: "BMH_INSPECT_NOT_SUCCEEDED",
    reason: "BMH_INSPECT_NOT_SUCCEEDED",
    host_count: 0,
    matched: 0,
  });
  assert.equal(verdict.tone, "warning");
  assert.equal(verdict.label, "Inspection failed");
  assert.equal(verdict.detail, "BMH_INSPECT_NOT_SUCCEEDED");
});

test("a full match is the only success case", () => {
  assert.deepEqual(
    bmhInventoryVerdict({
      available: true,
      status: "succeeded",
      host_count: 3,
      matched: 3,
      unmatched: [],
    }),
    { tone: "success", label: "3 / 3 host(s) matched", detail: "" },
  );
});

test("partial and empty inventories are warnings", () => {
  const partial = bmhInventoryVerdict({
    available: true,
    status: "succeeded",
    host_count: 3,
    matched: 2,
    unmatched: ["bmh-3"],
  });
  assert.equal(partial.tone, "warning");
  assert.match(partial.detail, /1 baremetal host/);

  const empty = bmhInventoryVerdict({
    available: true,
    status: "succeeded",
    host_count: 0,
    matched: 0,
  });
  assert.equal(empty.tone, "warning");
  assert.match(empty.detail, /no baremetal host/);
});

test("EPH preflight is green only when the run succeeded and the EPH is ready", () => {
  assert.deepEqual(
    ephPreflightVerdict({ available: true, status: "succeeded", eph_ready: true }),
    { tone: "success", label: "EPH ready", detail: "" },
  );
  assert.equal(
    ephPreflightVerdict({ available: true, status: "succeeded", eph_ready: false }).tone,
    "warning",
  );
});

test("a failed EPH preflight is not green even when it reported ready", () => {
  const verdict = ephPreflightVerdict({
    available: true,
    status: "failed",
    error_code: "EPH_PREFLIGHT_TIMEOUT",
    eph_ready: true,
  });
  assert.equal(verdict.tone, "warning");
  assert.equal(verdict.label, "EPH preflight failed");
  assert.equal(verdict.detail, "EPH_PREFLIGHT_TIMEOUT");
});

test("an unreachable EPH preflight is informational", () => {
  assert.deepEqual(ephPreflightVerdict(null), {
    tone: "info",
    label: "EPH preflight unavailable",
    detail: "",
  });
});

test("snapshot warnings survive a missing or malformed field", () => {
  // Older rows were stored before warnings were persisted; reading
  // `.warnings.length` on them would throw inside the template.
  assert.deepEqual(snapshotWarnings({ available: true }), []);
  assert.deepEqual(snapshotWarnings(null), []);
  assert.deepEqual(snapshotWarnings({ available: true, warnings: null }), []);
  assert.deepEqual(
    snapshotWarnings({ available: true, warnings: ["control link missing", "", 7] }),
    ["control link missing"],
  );
  assert.deepEqual(snapshotWarnings({ available: true, warnings: "pxe port down" }), [
    "pxe port down",
  ]);
});
