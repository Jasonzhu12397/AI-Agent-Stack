import test from "node:test";
import assert from "node:assert/strict";
import {
  generalScopePath,
  foldEffective,
  compareEffective,
  validateGeneralValues,
  fieldErrorsFromError,
  stateFromError,
  errorMessageKey,
} from "../src/settings/general/logic.ts";
import { ApiError } from "../src/services/http.ts";

test("generalScopePath builds tenant and project endpoint paths", () => {
  assert.equal(
    generalScopePath("tenant", "t-1", "p-1"),
    "/api/v1/tenants/t-1/settings/general/",
  );
  assert.equal(
    generalScopePath("project", "t-1", "p-1"),
    "/api/v1/tenants/t-1/projects/p-1/settings/general/",
  );
});

test("foldEffective applies project over tenant over default", () => {
  const folded = foldEffective(
    { theme: "system", density: "comfortable", page_size: 20 },
    { tenant: { density: "compact" }, project: { theme: "dark" } },
  );
  assert.equal(folded.values.theme, "dark");
  assert.equal(folded.values.density, "compact");
  assert.equal(folded.values.page_size, 20);
  assert.equal(folded.sources.theme, "project");
  assert.equal(folded.sources.density, "tenant");
  assert.equal(folded.sources.page_size, "default");
});

test("compareEffective lists keys where folded and server effective diverge", () => {
  const folded = { values: { a: 1, b: "x" }, sources: {} };
  const mismatches = compareEffective(folded, { a: 1, b: "y" });
  assert.deepEqual(mismatches, ["b"]);
});

test("validateGeneralValues rejects unknown keys and type mismatches", () => {
  const defaults = { theme: "system", page_size: 20, compact: false };
  const errors = validateGeneralValues(
    { theme: "dark", page_size: "many", rogue: true },
    defaults,
  );
  assert.deepEqual(errors.page_size, ["SETTINGS_INVALID_VALUE"]);
  assert.deepEqual(errors.rogue, ["SETTINGS_UNKNOWN_KEY"]);
  assert.equal(errors.theme, undefined);
});

test("fieldErrorsFromError flattens server details into field reasons", () => {
  const error = new ApiError(400, "SETTINGS_INVALID_VALUE", "Bad", {
    theme: ["Unsupported theme."],
  });
  assert.deepEqual(fieldErrorsFromError(error), { theme: ["Unsupported theme."] });
});

test("stateFromError maps 401/403 to denied and other failures to error", () => {
  assert.equal(stateFromError(new ApiError(401, "UNAUTHENTICATED", "n")).kind, "denied");
  assert.equal(stateFromError(new ApiError(403, "FORBIDDEN", "n")).kind, "denied");
  assert.equal(stateFromError(new ApiError(503, "UNAVAILABLE", "n")).kind, "error");
});

test("errorMessageKey maps known codes and falls back to generic save failure", () => {
  assert.equal(errorMessageKey("SETTINGS_UNKNOWN_KEY"), "Unknown setting key.");
  assert.equal(errorMessageKey("SETTINGS_INVALID_VALUE"), "Invalid value for setting.");
  assert.equal(errorMessageKey("NOPE"), "Settings could not be saved.");
});
