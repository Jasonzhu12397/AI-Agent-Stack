import test from "node:test";
import assert from "node:assert/strict";

import {
  APPEARANCE_DEFAULTS,
  APPEARANCE_STORAGE_KEY,
  applyAppearance,
  clampFontScale,
  normalizeAppearance,
  pageLabels,
  readCachedAppearance,
  resolveTheme,
  validateAppearance,
} from "../src/settings/appearance/logic.ts";
import { messages as appearanceMessages } from "../src/i18n/settings/appearance.zh.ts";

test("test_appearance_defaults_deterministic", () => {
  assert.deepEqual(normalizeAppearance(null), APPEARANCE_DEFAULTS);
  assert.deepEqual(normalizeAppearance(undefined), APPEARANCE_DEFAULTS);
  assert.deepEqual(normalizeAppearance("dark"), APPEARANCE_DEFAULTS);
  assert.deepEqual(normalizeAppearance({ theme: "neon", density: 3, font_scale: "big", reduced_motion: "yes" }), APPEARANCE_DEFAULTS);
  assert.deepEqual(normalizeAppearance({ theme: "dark", density: "compact", font_scale: 1.25, reduced_motion: true }), {
    theme: "dark",
    density: "compact",
    font_scale: 1.25,
    reduced_motion: true,
  });
});

test("test_appearance_storage_fallback", () => {
  assert.deepEqual(readCachedAppearance(null), APPEARANCE_DEFAULTS);
  assert.deepEqual(
    readCachedAppearance({
      getItem: () => {
        throw new Error("storage disabled");
      },
    }),
    APPEARANCE_DEFAULTS,
  );
  assert.deepEqual(readCachedAppearance({ getItem: () => "{not json" }), APPEARANCE_DEFAULTS);
  assert.deepEqual(readCachedAppearance({ getItem: () => null }), APPEARANCE_DEFAULTS);
  assert.deepEqual(
    readCachedAppearance({ getItem: (key) => (key === APPEARANCE_STORAGE_KEY ? '{"theme":"dark"}' : null) }),
    { ...APPEARANCE_DEFAULTS, theme: "dark" },
  );
});

test("test_font_scale_clamp_and_validation", () => {
  assert.equal(clampFontScale(0.1), 0.875);
  assert.equal(clampFontScale(9), 1.5);
  assert.equal(clampFontScale(Number.NaN), 1);
  assert.deepEqual(validateAppearance({ ...APPEARANCE_DEFAULTS, font_scale: 2.5 }).errors, {
    font_scale: "APPEARANCE_INVALID_FONT_SCALE",
  });
  assert.equal(validateAppearance({ ...APPEARANCE_DEFAULTS }).ok, true);
});

test("test_apply_appearance_writes_root_dataset", () => {
  const root = { dataset: {}, styles: {} };
  applyAppearance(
    { theme: "dark", density: "compact", font_scale: 1.25, reduced_motion: true },
    { dataset: root.dataset, style: { setProperty: (name, value) => (root.styles[name] = value) } },
  );
  assert.equal(root.dataset.theme, "dark");
  assert.equal(root.dataset.density, "compact");
  assert.equal(root.dataset.reducedMotion, "true");
  assert.equal(root.styles["--app-font-scale"], "1.25");
});

test("test_resolve_system_theme", () => {
  assert.equal(resolveTheme("system", true), "dark");
  assert.equal(resolveTheme("system", false), "light");
  assert.equal(resolveTheme("light", true), "light");
});

test("test_page_labels_have_chinese", () => {
  for (const label of pageLabels) {
    assert.ok(appearanceMessages[label], `missing zh translation for "${label}"`);
  }
});
