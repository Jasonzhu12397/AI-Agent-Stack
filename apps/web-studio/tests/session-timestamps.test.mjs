import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const accountLogic = readFileSync(
  new URL("../src/settings/account/logic.ts", import.meta.url),
  "utf8",
);
const privacyLogic = readFileSync(
  new URL("../src/settings/privacy/logic.ts", import.meta.url),
  "utf8",
);
const privacyApi = readFileSync(
  new URL("../src/settings/privacy/api.ts", import.meta.url),
  "utf8",
);
const accountPage = readFileSync(
  new URL("../src/settings/account/index.vue", import.meta.url),
  "utf8",
);

test("the account page maps server session rows instead of reading undefined fields", () => {
  assert.match(accountLogic, /export function mapSessionRow/);
  assert.match(accountLogic, /key: row\.session_key/);
  assert.match(accountPage, /mapSessionPayload\(payload\)/);
  assert.doesNotMatch(accountPage, /session\.last_ip/);
});

test("account sessions render real last activity when the server has it", () => {
  assert.match(accountPage, /session\.last_activity/);
  assert.match(accountLogic, /last_activity: row\.last_activity/);
});

test("privacy sessions map the twelve-field server row, not a stale shape", () => {
  assert.match(privacyLogic, /created_at_source: "session_metadata" \| "derived_from_expire_date"/);
  assert.match(privacyLogic, /mapPrivacySessions/);
  assert.match(privacyApi, /mapPrivacySessions\(page\.results/);
});

test("privacy sessions table marks derived timestamps and unavailable activity", () => {
  assert.match(readFileSync(new URL("../src/settings/privacy/index.vue", import.meta.url), "utf8"), /derived_from_expire_date/);
  assert.match(readFileSync(new URL("../src/settings/privacy/index.vue", import.meta.url), "utf8"), /Last active/);
});
