import test from "node:test";
import assert from "node:assert/strict";

import {
  FEEDBACK_CATEGORIES,
  FEEDBACK_SEVERITIES,
  HELP_LINKS,
  describeError,
  fieldErrorsFromDetails,
  pageLabels,
  validateFeedbackForm,
} from "../src/settings/help/logic.ts";
import { messages as helpMessages } from "../src/i18n/settings/help.zh.ts";

test("test_feedback_form_validation", () => {
  const valid = validateFeedbackForm({
    category: "bug",
    severity: "high",
    body: "The provisioning panel freezes after a bare-metal node finishes introspection.",
  });
  assert.equal(valid.ok, true);
  assert.equal(valid.code, "");
  assert.deepEqual(valid.fieldErrors, {});

  const empty = validateFeedbackForm({ category: "", severity: "", body: "" });
  assert.equal(empty.ok, false);
  assert.ok(empty.fieldErrors.category);
  assert.ok(empty.fieldErrors.severity);
  assert.ok(empty.fieldErrors.body);

  const badCategory = validateFeedbackForm({
    category: "prank",
    severity: "high",
    body: "The provisioning panel freezes after a bare-metal node finishes introspection.",
  });
  assert.equal(badCategory.ok, false);
  assert.equal(badCategory.code, "FEEDBACK_INVALID_CATEGORY");

  const badSeverity = validateFeedbackForm({
    category: "bug",
    severity: "critical",
    body: "The provisioning panel freezes after a bare-metal node finishes introspection.",
  });
  assert.equal(badSeverity.ok, false);
  assert.equal(badSeverity.code, "FEEDBACK_INVALID_SEVERITY");

  const shortBody = validateFeedbackForm({ category: "question", severity: "low", body: "nope" });
  assert.equal(shortBody.ok, false);
  assert.equal(shortBody.code, "FEEDBACK_EMPTY_BODY");
});

test("test_feedback_field_errors_from_server_details", () => {
  const details = { body: ["must be at least 10 characters"], category: ["unknown category"] };
  const fieldErrors = fieldErrorsFromDetails(details);
  assert.equal(fieldErrors.body, "must be at least 10 characters");
  assert.equal(fieldErrors.category, "unknown category");
  const scalar = fieldErrorsFromDetails({ severity: "severity is required" });
  assert.equal(scalar.severity, "severity is required");
  assert.deepEqual(fieldErrorsFromDetails(undefined), {});
});

test("test_help_links_are_absolute_and_real", () => {
  assert.ok(HELP_LINKS.length > 0, "at least one help resource must be listed");
  const seen = new Set();
  for (const link of HELP_LINKS) {
    assert.ok(link.label && link.label.trim(), "link label must not be empty");
    assert.ok(link.href && link.href.trim(), "link href must not be empty");
    assert.ok(!link.href.startsWith("#"), "anchor placeholder links are forbidden");
    assert.match(link.href, /^https:\/\//, "help links must be absolute https URLs");
    const url = new URL(link.href);
    assert.ok(url.hostname, "link must point at a real host");
    assert.ok(!seen.has(link.href), "duplicate help link");
    seen.add(link.href);
  }
  for (const category of FEEDBACK_CATEGORIES) assert.ok(category);
  for (const severity of FEEDBACK_SEVERITIES) assert.ok(severity);
});

test("test_help_error_code_mapping", () => {
  assert.match(describeError("FEEDBACK_RATE_LIMITED"), /rate|limit/i);
  assert.match(describeError("HTTP_401"), /session/i);
  assert.equal(describeError("NO_SUCH_CODE"), "Request failed (NO_SUCH_CODE).");
});

test("test_page_labels_have_chinese", () => {
  for (const label of pageLabels) {
    assert.ok(helpMessages[label], `missing zh translation for "${label}"`);
  }
});
