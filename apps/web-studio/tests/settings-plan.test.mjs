import test from "node:test";
import assert from "node:assert/strict";

import {
  PAYMENT_UNAVAILABLE,
  REGION_UNSUPPORTED,
  canPurchase,
  channelGapSummary,
  channelReadiness,
  describeError,
  formatPrice,
  intervalLabel,
  missingSlotsText,
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

test("test_channel_readiness_lists_supported_channels_including_absent_ones", () => {
  // The server may report only one channel; both must appear so an operator
  // sees that the other is missing rather than absent from the table.
  const oneChannel = {
    available: true,
    channels: ["wechat"],
    reason_code: null,
    diagnostics: [{ channel: "wechat", configured: true, missing: [], required: ["appid"] }],
  };
  assert.deepEqual(channelReadiness(oneChannel), [
    { channel: "wechat", configured: true, missing: [] },
    { channel: "alipay", configured: false, missing: [] },
  ]);
  assert.deepEqual(channelReadiness(undefined), [
    { channel: "wechat", configured: false, missing: [] },
    { channel: "alipay", configured: false, missing: [] },
  ]);
});

test("test_channel_readiness_ignores_unreported_and_non_string_slots", () => {
  const capability = {
    available: false,
    channels: [],
    reason_code: PAYMENT_UNAVAILABLE,
    diagnostics: [
      { channel: "wechat", configured: false, missing: ["appid", "", null] },
      { channel: "paypal", configured: true, missing: [] },
    ],
  };
  const readiness = channelReadiness(capability);
  assert.deepEqual(readiness[0], { channel: "wechat", configured: false, missing: ["appid"] });
  // A channel the product does not support never becomes a row.
  assert.equal(readiness.some((entry) => entry.channel === "paypal"), false);
  assert.deepEqual(readiness[1], { channel: "alipay", configured: false, missing: [] });
});

test("test_missing_slots_text_names_slots_without_a_reference_uri", () => {
  assert.equal(missingSlotsText({ channel: "wechat", configured: false, missing: ["appid", "mchid"] }), "appid, mchid");
  assert.equal(missingSlotsText({ channel: "alipay", configured: false, missing: [] }), "not configured");
  assert.equal(missingSlotsText({ channel: "wechat", configured: true, missing: [] }), "—");
  const rendered = ["env://WECHATPAY_APPID", "vault://merchant/wechatpay"].map((bad) =>
    missingSlotsText({ channel: "wechat", configured: false, missing: [bad] }),
  );
  for (const text of rendered) {
    assert.equal(text.includes("://"), false);
  }
});

test("test_channel_gap_summary_states_the_cause_per_channel", () => {
  assert.equal(channelGapSummary({ channel: "wechat", configured: true, missing: [] }), "wechat: configured");
  assert.equal(channelGapSummary({ channel: "alipay", configured: false, missing: [] }), "alipay: not configured");
  assert.equal(
    channelGapSummary({ channel: "alipay", configured: false, missing: ["app_id", "private_key"] }),
    "alipay: missing app_id, private_key",
  );
});
