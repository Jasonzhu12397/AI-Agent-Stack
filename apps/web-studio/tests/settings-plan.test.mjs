import test from "node:test";
import assert from "node:assert/strict";

import {
  PAYMENT_UNAVAILABLE,
  REGION_UNSUPPORTED,
  canPurchase,
  describeError,
  formatPrice,
  intervalLabel,
  pageLabels,
  paymentAvailability,
  quotaSummary,
} from "../src/settings/plan/logic.ts";
import { messages as planMessages } from "../src/i18n/settings/plan.zh.ts";

test("test_plan_unavailable_without_provider", () => {
  assert.deepEqual(paymentAvailability(undefined), { available: false, reasonCode: PAYMENT_UNAVAILABLE });
  assert.deepEqual(paymentAvailability({ available: false, channels: [], reason_code: null }), {
    available: false,
    reasonCode: PAYMENT_UNAVAILABLE,
  });
  assert.deepEqual(
    canPurchase({ purchasable: true, reason_code: null }, { available: false, channels: [], reason_code: null }),
    { ok: false, reasonCode: PAYMENT_UNAVAILABLE },
  );
});

test("test_plan_region_rejected_not_purchasable", () => {
  const capability = { available: true, channels: ["wechat", "alipay"], reason_code: null };
  const overseasPlan = { purchasable: false, reason_code: REGION_UNSUPPORTED };
  assert.deepEqual(canPurchase(overseasPlan, capability), { ok: false, reasonCode: REGION_UNSUPPORTED });
  // A plan the server flags with a reason must never be purchasable, even if purchasable=true.
  assert.deepEqual(canPurchase({ purchasable: true, reason_code: REGION_UNSUPPORTED }, capability), {
    ok: false,
    reasonCode: REGION_UNSUPPORTED,
  });
  assert.deepEqual(canPurchase({ purchasable: true, reason_code: null }, capability), { ok: true, reasonCode: null });
});

test("test_payment_channels_exclude_foreign_cards", () => {
  assert.deepEqual(paymentAvailability({ available: true, channels: ["wechat", "alipay"], reason_code: null }), {
    available: true,
    reasonCode: "",
  });
  assert.deepEqual(paymentAvailability({ available: true, channels: ["visa", "mastercard"], reason_code: null }), {
    available: false,
    reasonCode: PAYMENT_UNAVAILABLE,
  });
  assert.deepEqual(paymentAvailability({ available: true, channels: ["wechat", "visa"], reason_code: null }), {
    available: true,
    reasonCode: "",
  });
});

test("test_price_formatting_is_integer_exact", () => {
  assert.equal(formatPrice(1299), "¥12.99");
  assert.equal(formatPrice(0), "¥0.00");
  assert.equal(formatPrice(100000), "¥1000.00");
  assert.equal(formatPrice(7), "¥0.07");
  assert.equal(formatPrice(1999, "USD"), "USD 19.99");
  assert.equal(formatPrice(Number.NaN), "CNY —");
});

test("test_quota_and_interval_labels", () => {
  assert.equal(intervalLabel("yearly"), "per year");
  assert.equal(intervalLabel("monthly"), "per month");
  assert.match(quotaSummary({ token_quota_daily: 100000, token_quota_monthly: 1000000 }), /100,000 tokens \/ day/);
});

test("test_plan_error_code_mapping", () => {
  assert.match(describeError(PAYMENT_UNAVAILABLE), /Payment is unavailable/);
  assert.match(describeError(REGION_UNSUPPORTED), /cannot be purchased in this region/);
  assert.match(describeError("PAYMENT_CHANNEL_NOT_SUPPORTED"), /WeChat Pay and Alipay/);
  assert.match(describeError("HTTP_401"), /session has expired/);
  assert.equal(describeError("SOMETHING_NEW"), "Request failed (SOMETHING_NEW).");
});

test("test_page_labels_have_chinese", () => {
  for (const label of pageLabels) {
    assert.ok(planMessages[label], `missing zh translation for "${label}"`);
  }
});
