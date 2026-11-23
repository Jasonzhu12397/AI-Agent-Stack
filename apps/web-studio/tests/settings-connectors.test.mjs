import test from "node:test";
import assert from "node:assert/strict";

import {
  healthLabel,
  installationIdempotencyKey,
  pageLabels,
  resolveLoadState,
  summarizeTestResult,
  validateCredentialRef,
} from "../src/settings/connectors/logic.ts";
import { messages as connectorMessages } from "../src/i18n/settings/connectors.zh.ts";

test("test_credential_reference_validation_rejects_plaintext", () => {
  assert.deepEqual(validateCredentialRef("env://DEPLOYMENT_API_KEY"), { ok: true, code: "" });
  assert.deepEqual(validateCredentialRef("vault://secrets/connector/github"), { ok: true, code: "" });
  assert.deepEqual(validateCredentialRef("vault://team-a/weather/open-meteo"), { ok: true, code: "" });

  const plaintext = [
    "",
    "sk-abc123secret",
    "Bearer abc123456789",
    "https://api.example.com/token",
    "aws://secret/key",
    "plain token without scheme",
    "env:MISSING_SLASH",
  ];
  for (const bad of plaintext) {
    const result = validateCredentialRef(bad);
    assert.equal(result.ok, false, `expected plaintext/unknown ref to be rejected: "${bad}"`);
    assert.notEqual(result.code, "");
  }
});

test("test_connector_test_failure_shows_code_not_connected", () => {
  const failed = summarizeTestResult({ ok: false, code: "HEALTH_CHECK_FAILED", message: "host unreachable" });
  assert.equal(failed.status, "failed");
  assert.equal(failed.code, "HEALTH_CHECK_FAILED");
  assert.ok(!/connected|healthy/i.test(failed.summary), "failure summary must not claim connected/healthy");

  const failedNoCode = summarizeTestResult({ ok: false });
  assert.equal(failedNoCode.status, "failed");
  assert.equal(failedNoCode.code, "TEST_FAILED");

  const success = summarizeTestResult({ ok: true, summary: "200 OK in 120ms" });
  assert.equal(success.status, "success");
  assert.equal(success.summary, "200 OK in 120ms");

  const bare = summarizeTestResult({});
  assert.equal(bare.status, "failed");
  assert.notEqual(bare.code, "");
});

test("test_connector_health_label_mapping", () => {
  assert.equal(healthLabel("healthy"), "Healthy");
  assert.equal(healthLabel("unhealthy"), "Unhealthy");
  assert.equal(healthLabel("disabled"), "Disabled");
  assert.equal(healthLabel("unknown"), "Unknown");
  assert.equal(healthLabel(""), "Unknown");
  assert.equal(healthLabel("flapping"), "Unknown");
});

test("test_install_idempotency_key_is_deterministic", () => {
  assert.equal(installationIdempotencyKey("github"), installationIdempotencyKey("github"));
  assert.match(installationIdempotencyKey("github"), /^connector-install:github$/);
  assert.notEqual(installationIdempotencyKey("github"), installationIdempotencyKey("jira"));
});

test("test_load_state_maps_auth_failure_to_denied", () => {
  assert.equal(resolveLoadState(401), "denied");
  assert.equal(resolveLoadState(403), "denied");
  assert.equal(resolveLoadState(400), "error");
  assert.equal(resolveLoadState(500), "error");
  assert.equal(resolveLoadState(503), "error");
});

test("test_page_labels_have_chinese", () => {
  for (const label of pageLabels) {
    assert.ok(connectorMessages[label], `missing zh translation for "${label}"`);
  }
});
