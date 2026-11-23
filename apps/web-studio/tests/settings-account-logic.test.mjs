import test from "node:test";
import assert from "node:assert/strict";
import {
  validateProfile,
  profilePayload,
  summarizeIdentities,
  sessionCanRevoke,
  fieldErrorsFromError,
  stateFromError,
  errorMessageKey,
} from "../src/settings/account/logic.ts";
import { ApiError } from "../src/services/http.ts";

const validForm = {
  display_name: "Ada Lovelace",
  email: "ada@example.com",
  timezone: "America/Chicago",
  locale: "en",
};

test("validateProfile accepts a complete valid profile", () => {
  assert.deepEqual(validateProfile(validForm), {});
});

test("validateProfile reports missing and malformed fields", () => {
  const errors = validateProfile({
    display_name: "   ",
    email: "not-an-email",
    timezone: "",
    locale: "fr",
  });
  assert.equal(errors.display_name, "DISPLAY_NAME_REQUIRED");
  assert.equal(errors.email, "EMAIL_INVALID");
  assert.equal(errors.timezone, "TIMEZONE_REQUIRED");
  assert.equal(errors.locale, "LOCALE_INVALID");
});

test("profilePayload trims whitespace and keeps supported locale", () => {
  const payload = profilePayload({
    display_name: "  Ada ",
    email: "  ada@example.com ",
    timezone: "  UTC ",
    locale: "zh",
  });
  assert.deepEqual(payload, {
    display_name: "Ada",
    email: "ada@example.com",
    timezone: "UTC",
    locale: "zh",
  });
});

test("summarizeIdentities collects methods, password flag and bound count", () => {
  const summary = summarizeIdentities([
    { provider: "password", has_password: true },
    { provider: "oidc", uid: "sub-123" },
  ]);
  assert.equal(summary.boundCount, 2);
  assert.equal(summary.hasPassword, true);
  assert.deepEqual(summary.methods, ["password", "oidc"]);
});

test("sessionCanRevoke hides the revoke action for the current or revoked session", () => {
  assert.equal(sessionCanRevoke({ key: "s1", current: false, revoked: false }), true);
  assert.equal(sessionCanRevoke({ key: "s2", current: true }), false);
  assert.equal(sessionCanRevoke({ key: "s3", current: false, revoked: true }), false);
});

test("fieldErrorsFromError flattens server details into field -> reasons", () => {
  const error = new ApiError(400, "SETTINGS_INVALID_VALUE", "Validation failed", {
    email: ["Enter a valid email address."],
    timezone: "Timezone is required.",
  });
  assert.deepEqual(fieldErrorsFromError(error), {
    email: ["Enter a valid email address."],
    timezone: ["Timezone is required."],
  });
});

test("stateFromError maps 401/403 to denied and other failures to error", () => {
  assert.equal(stateFromError(new ApiError(401, "UNAUTHENTICATED", "nope")).kind, "denied");
  assert.equal(stateFromError(new ApiError(403, "FORBIDDEN", "nope")).kind, "denied");
  assert.equal(stateFromError(new ApiError(500, "HTTP_500", "boom")).kind, "error");
  assert.equal(stateFromError(new Error("network")).kind, "error");
});

test("errorMessageKey maps known codes and falls back to the generic save failure", () => {
  assert.equal(errorMessageKey("EMAIL_INVALID"), "Invalid email address.");
  assert.equal(errorMessageKey("SOMETHING_UNKNOWN"), "Profile could not be saved.");
});
