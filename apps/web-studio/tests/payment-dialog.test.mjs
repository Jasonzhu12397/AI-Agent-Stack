import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const dialog = readFileSync(
  new URL("../src/settings/plan/PaymentDialog.vue", import.meta.url),
  "utf8",
);
const api = readFileSync(new URL("../src/settings/plan/api.ts", import.meta.url), "utf8");
const page = readFileSync(
  new URL("../src/settings/plan/index.vue", import.meta.url),
  "utf8",
);

test("the checkout dialog polls the read-only status endpoint, never a mutating one", () => {
  assert.match(dialog, /fetchOrderStatus\(/);
  assert.doesNotMatch(dialog, /fetchOrderDetail/);
  assert.match(dialog, /POLL_INTERVAL_MS/);
  assert.match(dialog, /clearTimeout|stopPolling/);
});

test("paid state is only taken from the server response, not assumed locally", () => {
  assert.match(dialog, /latest\.status === "paid"/);
  assert.match(dialog, /emit\("settled", latest\)/);
  assert.doesNotMatch(dialog, /status\.value = "paid"/);
});

test("closing a trade goes through the provider-backed close endpoint", () => {
  assert.match(api, /export async function closeOrder/);
  assert.match(api, /billing\/orders\/\$\{orderId\}\/close\//);
  assert.match(dialog, /closeOrder\(/);
});

test("the dialog renders a scannable code from the persisted instruction", () => {
  assert.match(dialog, /payment\?\.code_url/);
  assert.match(dialog, /payment\?\.qr_code/);
  assert.match(dialog, /QRCode\.toDataURL/);
});

test("the plan page opens the dialog with the created order", () => {
  assert.match(page, /activeOrder\.value = order/);
  assert.match(page, /<PaymentDialog/);
  assert.match(page, /@settled="onOrderSettled"/);
});
