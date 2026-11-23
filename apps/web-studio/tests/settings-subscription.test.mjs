import test from "node:test";
import assert from "node:assert/strict";

import {
  describeError,
  formatAmount,
  formatResetAt,
  formatTokens,
  isPaid,
  mapQuotaError,
  orderStatusLabel,
  pageLabels,
  quotaMessage,
  quotaState,
  remainingQuota,
  usagePercent,
} from "../src/settings/subscription/logic.ts";
import { messages as subscriptionMessages } from "../src/i18n/settings/subscription.zh.ts";

test("test_order_status_never_infers_paid", () => {
  assert.equal(isPaid({ status: "paid" }), true);
  for (const status of ["created", "pending", "failed", "closed", "refunded"]) {
    assert.equal(isPaid({ status }), false, `${status} must never read as paid`);
  }
  assert.match(orderStatusLabel("created"), /unpaid/);
  assert.match(orderStatusLabel("pending"), /Awaiting payment/);
  assert.equal(orderStatusLabel("paid"), "Paid");
  assert.equal(orderStatusLabel("refunded"), "Refunded");
});

test("test_usage_authoritative_passthrough_and_percent_guards", () => {
  assert.equal(usagePercent(500, 1000), 50);
  assert.equal(usagePercent(1000, 1000), 100);
  assert.equal(usagePercent(2000, 1000), 100);
  assert.equal(usagePercent(5, 0), 0);
  assert.equal(usagePercent(Number.NaN, 10), 0);
  assert.equal(formatTokens(1234567), "1,234,567");
  assert.equal(formatTokens(-1), "—");
});

test("test_amount_uses_integer_cents", () => {
  assert.equal(formatAmount(1299, "CNY"), "¥12.99");
  assert.equal(formatAmount(0, "CNY"), "¥0.00");
  assert.equal(formatAmount(1999, "USD"), "USD 19.99");
});

test("test_quota_state_threshold_boundaries", () => {
  assert.equal(quotaState(79.9, 100), "ok");
  assert.equal(quotaState(80, 100), "warning");
  assert.equal(quotaState(99.9, 100), "warning");
  assert.equal(quotaState(100, 100), "exhausted");
  assert.equal(quotaState(120, 100), "exhausted");
  assert.equal(quotaState(0, 100), "ok");
  assert.equal(quotaState(5, 0), "ok");
  assert.equal(quotaState(Number.NaN, 10), "ok");
});

test("test_remaining_quota_is_never_negative", () => {
  assert.equal(remainingQuota(50, 100), 50);
  assert.equal(remainingQuota(150, 100), 0);
  assert.equal(remainingQuota(50, 100, 10), 10);
  assert.equal(remainingQuota(50, 100, -5), 50);
  assert.equal(remainingQuota(50, 100, null), 50);
  assert.equal(remainingQuota(Number.NaN, 100), 100);
});

test("test_quota_message_warning_and_exhausted_copy", () => {
  const warning = quotaMessage("warning", "daily", { percent: 80 });
  assert.match(warning, /已使用 80%/);
  assert.match(warning, /接近/);
  assert.match(warning, /上限/);

  const resetAt = "2026-11-24T00:00:00-06:00";
  const exhausted = quotaMessage("exhausted", "monthly", { resetAt });
  assert.match(exhausted, /额度已用尽/);
  assert.match(exhausted, /升级套餐后继续/);
  assert.ok(exhausted.includes(formatResetAt(resetAt)), "exhausted copy must carry reset time");

  const exhaustedNoReset = quotaMessage("exhausted", "daily");
  assert.match(exhaustedNoReset, /额度已用尽/);
  assert.doesNotMatch(exhaustedNoReset, /重置/);
});

test("test_map_quota_error_blocks_budget_exceeded_by_period", () => {
  const daily = mapQuotaError("TENANT_TOKEN_BUDGET_EXCEEDED", "daily", "2026-11-24T00:00:00-06:00");
  assert.match(daily, /每日/);
  assert.match(daily, /阻断/);
  const monthly = mapQuotaError("TENANT_TOKEN_BUDGET_EXCEEDED", "monthly");
  assert.match(monthly, /每月/);
  assert.match(monthly, /阻断/);
  // Non-budget codes keep the generic mapping; a 429 must not read as empty data.
  assert.match(mapQuotaError("PAYMENT_PROVIDER_NOT_CONFIGURED"), /no payment provider is configured/);
  assert.match(mapQuotaError("HTTP_403"), /role does not allow billing changes/);
});

test("test_reset_time_format_guards", () => {
  assert.equal(formatResetAt(null), "—");
  assert.equal(formatResetAt(undefined), "—");
  assert.equal(formatResetAt(""), "—");
  assert.equal(formatResetAt("not-a-date"), "—");
  assert.match(formatResetAt("2026-11-24T00:00:00-06:00"), /\d{4}-\d{2}-\d{2} \d{2}:\d{2}/);
});

test("test_subscription_error_code_mapping", () => {
  assert.match(describeError("PAYMENT_PROVIDER_NOT_CONFIGURED"), /no payment provider is configured/);
  assert.match(describeError("TENANT_TOKEN_BUDGET_EXCEEDED"), /fail instead of overspending/);
  assert.match(describeError("HTTP_403"), /role does not allow billing changes/);
});

test("test_page_labels_have_chinese", () => {
  for (const label of pageLabels) {
    assert.ok(subscriptionMessages[label], `missing zh translation for "${label}"`);
  }
});
