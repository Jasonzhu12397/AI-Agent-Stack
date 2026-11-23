import test from "node:test";
import assert from "node:assert/strict";

import { describeError, normalizeAbout, pageLabels } from "../src/settings/about/logic.ts";
import { messages as aboutMessages } from "../src/i18n/settings/about.zh.ts";

test("test_about_unknown_version_fallback", () => {
  const missing = normalizeAbout(null);
  assert.equal(missing.version, "unknown");
  assert.equal(missing.build, "unknown");
  assert.equal(missing.licenses, "unknown");
  assert.equal(missing.release_notes_url, "");

  const empty = normalizeAbout({});
  assert.equal(empty.version, "unknown");
  assert.equal(empty.build, "unknown");
  assert.equal(empty.licenses, "unknown");

  const blanks = normalizeAbout({ version: "", build: null, licenses: undefined });
  assert.equal(blanks.version, "unknown");
  assert.equal(blanks.build, "unknown");
  assert.equal(blanks.licenses, "unknown");

  const partial = normalizeAbout({ version: "2.4.1" });
  assert.equal(partial.version, "2.4.1");
  assert.equal(partial.build, "unknown");

  const full = normalizeAbout({
    version: "1.1.0",
    build: "b42ac13",
    licenses: "Apache-2.0",
    release_notes_url: "https://example.com/releases",
  });
  assert.equal(full.version, "1.1.0");
  assert.equal(full.build, "b42ac13");
  assert.equal(full.licenses, "Apache-2.0");
  assert.equal(full.release_notes_url, "https://example.com/releases");
});

test("test_about_error_code_mapping", () => {
  assert.match(describeError("HTTP_401"), /session/i);
  assert.match(describeError("META_UNAVAILABLE"), /unavailable/i);
  assert.equal(describeError("NO_SUCH_CODE"), "Request failed (NO_SUCH_CODE).");
});

test("test_page_labels_have_chinese", () => {
  for (const label of pageLabels) {
    assert.ok(aboutMessages[label], `missing zh translation for "${label}"`);
  }
});
