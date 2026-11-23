import test from "node:test";
import assert from "node:assert/strict";

import {
  buildArchiveQuery,
  deleteGate,
  describeError,
  isAlreadyRestored,
  pageLabels,
  restoreResultMeta,
} from "../src/settings/archive/logic.ts";
import { messages as archiveMessages } from "../src/i18n/settings/archive.zh.ts";

test("test_archive_filters_and_pagination_params", () => {
  assert.deepEqual(buildArchiveQuery({}), { page: "1" });
  assert.deepEqual(buildArchiveQuery({ type: "agent", q: "demo", page: 3 }), {
    type: "agent",
    q: "demo",
    page: "3",
  });
  // Empty filters and page 0 fall back to defaults; blanks are never sent to the server.
  assert.deepEqual(buildArchiveQuery({ type: "", q: "   ", page: 0 }), { page: "1" });
});

test("test_restore_idempotent_state", () => {
  assert.equal(isAlreadyRestored("already_restored"), true);
  assert.equal(isAlreadyRestored("archived"), false);
  const meta = restoreResultMeta("already_restored");
  assert.equal(meta.alreadyRestored, true);
  assert.match(meta.label, /already/i);
  assert.equal(restoreResultMeta("archived").alreadyRestored, false);
});

test("test_delete_requires_confirmation", () => {
  const unconfirmed = deleteGate(false);
  assert.equal(unconfirmed.ok, false);
  assert.match(unconfirmed.reason, /irrecoverable|unrecoverable|不可恢复/i);
  assert.deepEqual(deleteGate(true), { ok: true, reason: "" });
});

test("test_archive_error_code_mapping", () => {
  assert.match(describeError("ARCHIVE_ITEM_NOT_FOUND"), /no longer/);
  assert.match(describeError("ARCHIVE_PERMANENT_DELETE_DENIED"), /administrator|permission/i);
  assert.equal(describeError("SOMETHING_NEW"), "Request failed (SOMETHING_NEW).");
});

test("test_page_labels_have_chinese", () => {
  for (const label of pageLabels) {
    assert.ok(archiveMessages[label], `missing zh translation for "${label}"`);
  }
});
