import test from "node:test";
import assert from "node:assert/strict";

import {
  buildBindings,
  describeError,
  findConflicts,
  normalizeCombination,
  pageLabels,
  validateCombination,
} from "../src/settings/shortcuts/logic.ts";
import { messages as shortcutMessages } from "../src/i18n/settings/shortcuts.zh.ts";

test("test_shortcut_normalization", () => {
  assert.equal(normalizeCombination("ctrl+k"), "Ctrl+K");
  assert.equal(normalizeCombination("Shift+Ctrl+K"), "Ctrl+Shift+K");
  assert.equal(normalizeCombination("shift + ctrl + k"), "Ctrl+Shift+K");
  assert.equal(normalizeCombination("cmd+enter"), "Meta+Enter");
  assert.equal(normalizeCombination("alt+f4"), "Alt+F4");
  assert.equal(normalizeCombination("Ctrl+Shift"), "");
  assert.equal(normalizeCombination("Ctrl+K+J"), "");
  assert.equal(normalizeCombination(""), "");
  assert.equal(normalizeCombination("Ctrl+NotAKey"), "");
});

test("test_shortcut_validation_requires_modifier", () => {
  assert.deepEqual(validateCombination("K"), { ok: false, code: "SHORTCUT_INVALID_COMBINATION" });
  assert.deepEqual(validateCombination("Ctrl+K"), { ok: true, code: "" });
  assert.deepEqual(validateCombination("F5"), { ok: true, code: "" });
});

test("test_shortcut_conflict_detection", () => {
  const conflicts = findConflicts({
    "editor.save": "Ctrl+S",
    "editor.export": "ctrl+s",
    "nav.search": "Ctrl+K",
  });
  assert.equal(conflicts.length, 2);
  assert.deepEqual(conflicts.map((entry) => entry.id), ["editor.export", "editor.save"]);
  assert.deepEqual(conflicts[0].conflictsWith, ["editor.save"]);
  assert.equal(conflicts[0].combination, "Ctrl+S");
});

test("test_shortcut_defaults_come_from_registry", () => {
  const registry = [
    { id: "editor.save", default_combination: "ctrl+s", scope: "global", description: "Save" },
    { id: "nav.search", default_combination: "Shift+Ctrl+K", scope: "global", description: "Search" },
  ];
  const defaults = buildBindings(registry, null);
  assert.deepEqual(defaults, { "editor.save": "Ctrl+S", "nav.search": "Ctrl+Shift+K" });
  // Overrides apply only to known ids and only when they normalise.
  const overridden = buildBindings(registry, { "editor.save": "ctrl+alt+s", unknown: "Ctrl+Z", "nav.search": "nonsense" });
  assert.equal(overridden["editor.save"], "Ctrl+Alt+S");
  assert.equal(overridden["nav.search"], "Ctrl+Shift+K");
  assert.equal(overridden.unknown, undefined);
});

test("test_shortcut_error_labels", () => {
  assert.match(describeError("SHORTCUT_CONFLICT"), /already uses this combination/);
  assert.match(describeError("SHORTCUT_INVALID_COMBINATION"), /not valid/);
  for (const label of pageLabels) {
    assert.ok(shortcutMessages[label], `missing zh translation for "${label}"`);
  }
});
