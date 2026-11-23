import test from "node:test";
import assert from "node:assert/strict";

import {
  CATEGORIES,
  CHANNELS,
  NOTIFICATION_DEFAULTS,
  describeError,
  digestLabel,
  foldDelivery,
  normalizeMatrix,
  pageLabels,
  toggleChannel,
} from "../src/settings/notifications/logic.ts";
import { messages as notificationMessages } from "../src/i18n/settings/notifications.zh.ts";

test("test_notification_matrix_defaults", () => {
  assert.deepEqual(normalizeMatrix(null), {
    run_failed: { in_app: false, email: false, webhook: false },
    run_succeeded: { in_app: false, email: false, webhook: false },
    approval_required: { in_app: false, email: false, webhook: false },
    budget_threshold: { in_app: false, email: false, webhook: false },
    connector_health: { in_app: false, email: false, webhook: false },
  });
  for (const category of CATEGORIES) {
    for (const channel of CHANNELS) {
      assert.equal(typeof NOTIFICATION_DEFAULTS.matrix[category][channel], "boolean");
    }
  }
});

test("test_notification_matrix_from_partial_values", () => {
  const matrix = normalizeMatrix({ run_failed: { email: true }, connector_health: { webhook: "yes" } });
  assert.equal(matrix.run_failed.email, true);
  assert.equal(matrix.run_failed.in_app, false);
  assert.equal(matrix.connector_health.webhook, false);
});

test("test_delivery_preferences_only_narrow", () => {
  const tenant = normalizeMatrix({ run_failed: { in_app: true, email: true, webhook: true } });
  const narrowed = foldDelivery(tenant, { email: false });
  assert.equal(narrowed.run_failed.email, false);
  assert.equal(narrowed.run_failed.in_app, true);
  assert.equal(narrowed.run_failed.webhook, true);
  // A user preference cannot switch on what the tenant turned off.
  const widened = foldDelivery(normalizeMatrix(null), { in_app: true });
  assert.equal(widened.run_failed.in_app, false);
});

test("test_toggle_channel_is_pure", () => {
  const original = normalizeMatrix(null);
  const toggled = toggleChannel(original, "run_failed", "email");
  assert.equal(toggled.run_failed.email, true);
  assert.equal(original.run_failed.email, false);
  assert.notEqual(toggled, original);
});

test("test_digest_and_error_labels", () => {
  assert.equal(digestLabel("immediate"), "Immediate delivery");
  assert.equal(digestLabel("daily"), "Daily digest");
  assert.match(describeError("SETTINGS_UNKNOWN_KEY"), /no longer exists/);
  assert.match(describeError("HTTP_403"), /administrators/);
});

test("test_page_labels_have_chinese", () => {
  for (const label of pageLabels) {
    assert.ok(notificationMessages[label], `missing zh translation for "${label}"`);
  }
});
