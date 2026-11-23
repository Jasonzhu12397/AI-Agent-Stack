import test from "node:test";
import assert from "node:assert/strict";

import {
  describeError,
  formatAmount,
  formatTokens,
  isPaid,
  orderStatusLabel,
  pageLabels,
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
