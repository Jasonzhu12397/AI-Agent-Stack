import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const api = readFileSync(new URL("../src/services/api.ts", import.meta.url), "utf8");
const view = readFileSync(new URL("../src/components/AuditView.vue", import.meta.url), "utf8");

test("global audit client uses the platform-admin endpoints", () => {
  assert.match(api, /export function getGlobalAuditEvents/);
  assert.match(api, /"\/api\/v1\/audit-events\/global\/\?"/);
  assert.match(api, /export function verifyGlobalAuditChain/);
  assert.match(api, /"\/api\/v1\/audit-events\/global\/verify\/"/);
});

test("audit view renders the global chain only after an authorised fetch", () => {
  assert.match(view, /getGlobalAuditEvents\(\)/);
  assert.match(view, /verifyGlobalAuditChain\(\)/);
  assert.match(view, /globalAuditAvailable/);
  assert.match(view, /Platform authentication chain/);
  assert.match(view, /cause instanceof ApiError && cause.status === 403/);
});
