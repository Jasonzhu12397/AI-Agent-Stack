import test from "node:test";
import assert from "node:assert/strict";

import {
  DATA_CONTROLS_DEFAULTS,
  EXPORT_IDEMPOTENCY_PREFIX,
  buildIdempotencyKey,
  canDownloadExport,
  describeError,
  exportGate,
  exportScopeWarning,
  exportStatusMeta,
  normalizeDataControls,
  pageLabels,
} from "../src/settings/data-controls/logic.ts";
import { messages as dcMessages } from "../src/i18n/settings/data-controls.zh.ts";

test("test_export_requires_confirmation_and_idempotency_key", () => {
  const missingKey = exportGate({ confirmed: true, idempotencyKey: "" });
  assert.equal(missingKey.ok, false);
  assert.ok(missingKey.missing.includes("idempotency_key"));

  const notConfirmed = exportGate({ confirmed: false, idempotencyKey: "dc-export-abc" });
  assert.equal(notConfirmed.ok, false);
  assert.ok(notConfirmed.missing.includes("confirm"));

  const ready = exportGate({ confirmed: true, idempotencyKey: "dc-export-abc" });
  assert.equal(ready.ok, true);
  assert.deepEqual(ready.missing, []);
});

test("test_export_not_downloadable_before_success", () => {
  for (const status of ["pending", "running", "failed", "expired"]) {
    assert.equal(
      canDownloadExport({ status, download_url: "https://example.test/export.zip" }),
      false,
      `must not expose a download for ${status}`,
    );
  }
  // A succeeded job without a URL still offers no download link.
  assert.equal(canDownloadExport({ status: "succeeded", download_url: "" }), false);
  assert.equal(canDownloadExport({ status: "succeeded", download_url: "https://example.test/export.zip" }), true);
});

test("test_retention_scope_text_includes_irreversible_warning", () => {
  const warning = exportScopeWarning(30);
  assert.match(warning, /30/);
  assert.match(warning, /tenant/i);
  assert.match(warning, /irreversible/i);
  assert.match(warning, /retention/i);
});

test("test_idempotency_key_keeps_deterministic_prefix_and_one_shot_suffix", () => {
  const key = buildIdempotencyKey(EXPORT_IDEMPOTENCY_PREFIX, "click-1");
  assert.equal(key, "dc-export:click-1");
  const again = buildIdempotencyKey(EXPORT_IDEMPOTENCY_PREFIX, "click-2");
  assert.notEqual(again, key);
});

test("test_data_controls_normalize_falls_back_to_defaults", () => {
  const normalized = normalizeDataControls({ retention_days: 90 });
  assert.equal(normalized.retention_days, 90);
  assert.equal(normalized.export_format, DATA_CONTROLS_DEFAULTS.export_format);
  assert.equal(normalized.allow_export, DATA_CONTROLS_DEFAULTS.allow_export);
  assert.deepEqual(normalizeDataControls(null), DATA_CONTROLS_DEFAULTS);
});

test("test_export_status_mapping_covers_all_job_states", () => {
  for (const status of ["pending", "running", "succeeded", "failed", "expired"]) {
    const meta = exportStatusMeta(status);
    assert.ok(meta.label, `missing label for ${status}`);
    assert.ok(meta.tagType, `missing tag type for ${status}`);
  }
  assert.equal(exportStatusMeta("succeeded").tagType, "success");
});

test("test_data_controls_error_code_mapping", () => {
  assert.match(describeError("SETTINGS_UNKNOWN_KEY"), /no longer exists/);
  assert.match(describeError("SETTINGS_INVALID_VALUE"), /rejected/);
  assert.match(describeError("HTTP_403"), /administrator/);
  assert.equal(describeError("SOMETHING_NEW"), "Request failed (SOMETHING_NEW).");
});

test("test_page_labels_have_chinese", () => {
  for (const label of pageLabels) {
    assert.ok(dcMessages[label], `missing zh translation for "${label}"`);
  }
});
