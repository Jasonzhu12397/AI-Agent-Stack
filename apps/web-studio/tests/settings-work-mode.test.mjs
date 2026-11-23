import test from "node:test";
import assert from "node:assert/strict";

import {
  MAX_CONCURRENCY_LIMIT,
  WORK_MODE_DEFAULTS,
  describeError,
  normalizeWorkMode,
  pageLabels,
  sourceLabel,
  validateWorkMode,
} from "../src/settings/work-mode/logic.ts";
import { messages as workModeMessages } from "../src/i18n/settings/work-mode.zh.ts";

test("test_work_mode_defaults", () => {
  assert.deepEqual(normalizeWorkMode(null), WORK_MODE_DEFAULTS);
  assert.deepEqual(normalizeWorkMode({ max_concurrency: "many", default_execution_mode: "yolo" }), WORK_MODE_DEFAULTS);
});

test("test_work_mode_normalisation_clamps_and_filters", () => {
  const normalized = normalizeWorkMode({
    max_concurrency: 999,
    workspace_default_branch: "bad branch!",
    default_execution_mode: "auto",
    auto_continue: true,
  });
  assert.equal(normalized.max_concurrency, MAX_CONCURRENCY_LIMIT);
  assert.equal(normalized.workspace_default_branch, WORK_MODE_DEFAULTS.workspace_default_branch);
  assert.equal(normalized.default_execution_mode, "auto");
  assert.equal(normalized.auto_continue, true);
});

test("test_work_mode_validation_codes", () => {
  assert.deepEqual(validateWorkMode({ ...WORK_MODE_DEFAULTS, max_concurrency: 0 }).errors, {
    max_concurrency: "WORK_MODE_INVALID_CONCURRENCY",
  });
  assert.deepEqual(validateWorkMode({ ...WORK_MODE_DEFAULTS, workspace_default_branch: "not a branch" }).errors, {
    workspace_default_branch: "WORK_MODE_INVALID_BRANCH",
  });
  assert.deepEqual(
    validateWorkMode({ ...WORK_MODE_DEFAULTS, default_execution_mode: "auto", require_confirmation: false }).errors,
    { require_confirmation: "WORK_MODE_UNSAFE_AUTONOMY" },
  );
  assert.equal(validateWorkMode(WORK_MODE_DEFAULTS).ok, true);
});

test("test_work_mode_scope_labels", () => {
  assert.equal(sourceLabel("project"), "Project override");
  assert.equal(sourceLabel("tenant"), "Tenant setting");
  assert.equal(sourceLabel("user"), "User preference");
  assert.equal(sourceLabel(undefined), "Default");
  assert.match(describeError("WORK_MODE_UNSAFE_AUTONOMY"), /not allowed/);
  assert.match(describeError("HTTP_403"), /project administrators/);
});

test("test_page_labels_have_chinese", () => {
  for (const label of pageLabels) {
    assert.ok(workModeMessages[label], `missing zh translation for "${label}"`);
  }
});
