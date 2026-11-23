import test from "node:test";
import assert from "node:assert/strict";

import {
  DELETION_SCOPES,
  PRIVACY_DEPENDENCY_UNAVAILABLE,
  deletionRequestGate,
  deletionScopeLabel,
  describeError,
  isValidScope,
  pageLabels,
  revokeGrantGate,
  sessionAvailability,
} from "../src/settings/privacy/logic.ts";
import { messages as privacyMessages } from "../src/i18n/settings/privacy.zh.ts";

test("test_privacy_dependency_unavailable_state", () => {
  const unavailable = sessionAvailability({ status: 503, code: PRIVACY_DEPENDENCY_UNAVAILABLE });
  assert.equal(unavailable.available, false);
  assert.equal(unavailable.reasonCode, PRIVACY_DEPENDENCY_UNAVAILABLE);

  const plain503 = sessionAvailability({ status: 503, code: "HTTP_503" });
  assert.equal(plain503.available, false);
  assert.equal(plain503.reasonCode, "HTTP_503");

  const healthy = sessionAvailability({ status: 200, code: "" });
  assert.equal(healthy.available, true);
  assert.equal(healthy.reasonCode, "");
});

test("test_deletion_request_requires_confirm_and_scope", () => {
  const badScope = deletionRequestGate({ scope: "messages", confirm: true, idempotencyKey: "priv-del-1" });
  assert.equal(badScope.ok, false);
  assert.ok(badScope.missing.includes("scope"));

  const notConfirmed = deletionRequestGate({ scope: "exports", confirm: false, idempotencyKey: "priv-del-1" });
  assert.equal(notConfirmed.ok, false);
  assert.ok(notConfirmed.missing.includes("confirm"));

  const missingKey = deletionRequestGate({ scope: "exports", confirm: true, idempotencyKey: "" });
  assert.equal(missingKey.ok, false);
  assert.ok(missingKey.missing.includes("idempotency_key"));

  const ready = deletionRequestGate({
    scope: "archived_content",
    confirm: true,
    idempotencyKey: "priv-del-1",
  });
  assert.equal(ready.ok, true);
  assert.deepEqual(ready.missing, []);
});

test("test_deletion_scope_is_from_fixed_enum", () => {
  assert.deepEqual([...DELETION_SCOPES], ["archived_content", "exports"]);
  assert.equal(isValidScope("exports"), true);
  assert.equal(isValidScope("messages"), false);
  assert.match(deletionScopeLabel("archived_content"), /archived/i);
  assert.match(deletionScopeLabel("exports"), /export/i);
});

test("test_revoke_grant_requires_confirmation", () => {
  assert.equal(revokeGrantGate(false).ok, false);
  assert.equal(revokeGrantGate(true).ok, true);
});

test("test_privacy_error_code_mapping", () => {
  assert.match(describeError(PRIVACY_DEPENDENCY_UNAVAILABLE), /unavailable|dependency/i);
  assert.match(describeError("PRIVACY_GRANT_NOT_FOUND"), /no longer/);
  assert.equal(describeError("SOMETHING_NEW"), "Request failed (SOMETHING_NEW).");
});

test("test_page_labels_have_chinese", () => {
  for (const label of pageLabels) {
    assert.ok(privacyMessages[label], `missing zh translation for "${label}"`);
  }
});
