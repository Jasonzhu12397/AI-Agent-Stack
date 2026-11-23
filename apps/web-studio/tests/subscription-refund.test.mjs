import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const page = readFileSync(
  new URL("../src/settings/subscription/index.vue", import.meta.url),
  "utf8",
);
const api = readFileSync(
  new URL("../src/settings/subscription/api.ts", import.meta.url),
  "utf8",
);
const logic = readFileSync(
  new URL("../src/settings/subscription/logic.ts", import.meta.url),
  "utf8",
);

test("refunds go through the provider-backed refund endpoint with amount and reason", () => {
  assert.match(api, /export async function refundOrder/);
  assert.match(api, /billing\/orders\/\$\{orderId\}\/refund\//);
  assert.match(api, /amount_cents: amountCents/);
  assert.match(api, /reason/);
});

test("the subscription page exposes refund and close actions on the order table", () => {
  assert.match(page, /refundOrder\(/);
  assert.match(page, /closeOrder\(/);
  assert.match(page, /canRefund/);
  assert.match(page, /canClose/);
});

test("the refund dialog is bounded by the remaining refundable amount", () => {
  assert.match(page, /const refundRemaining = computed/);
  assert.match(page, /refunded_cents/);
  assert.match(page, /cents > remaining/);
  assert.match(page, /:max="refundRemaining \/ 100"/);
});

test("order rendering understands partial refunds", () => {
  assert.match(logic, /"partial_refunded"/);
  assert.match(logic, /refunded_cents: number/);
  assert.match(logic, /Partially refunded/);
});

test("refund failures surface the server error codes instead of a generic message", () => {
  assert.match(logic, /REFUND_AMOUNT_EXCEEDED/);
  assert.match(logic, /REFUND_AMOUNT_INVALID/);
  assert.match(logic, /PAYMENT_PROVIDER_RESPONSE_UNTRUSTED/);
});
