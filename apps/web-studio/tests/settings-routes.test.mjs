import test from "node:test";
import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { settingsRouteDefs, settingsRoutes } from "../src/settings/routes.ts";
import { SETTINGS_SLUGS, SETTINGS_GROUPS } from "../src/settings/shared/scope.ts";

const settingsRoot = join(dirname(fileURLToPath(import.meta.url)), "..", "src", "settings");

test("settings route inventory matches the frozen 14 slugs in order", () => {
  assert.equal(settingsRouteDefs.length, SETTINGS_SLUGS.length);
  assert.deepEqual(
    settingsRouteDefs.map((def) => def.slug),
    [...SETTINGS_SLUGS],
  );
});

test("settings route paths and names are unique", () => {
  const paths = settingsRouteDefs.map((def) => def.path);
  const names = settingsRouteDefs.map((def) => def.name);
  assert.equal(new Set(paths).size, paths.length);
  assert.equal(new Set(names).size, names.length);
  for (const def of settingsRouteDefs) {
    assert.equal(def.path, `/settings/${def.slug}`);
    assert.equal(def.name, `settings.${def.slug}`);
  }
});

test("group assignment and order follow SETTINGS_GROUPS", () => {
  const expected = [];
  for (const group of SETTINGS_GROUPS) {
    for (const slug of group.slugs) expected.push({ slug, group: group.group });
  }
  assert.deepEqual(
    settingsRouteDefs.map((def) => ({ slug: def.slug, group: def.group })),
    expected,
  );
});

test("each route carries complete auth, group and section meta", () => {
  for (const def of settingsRouteDefs) {
    assert.equal(def.meta.requiresAuth, true, `${def.slug} requiresAuth`);
    assert.ok(def.meta.group === "account" || def.meta.group === "general", `${def.slug} group`);
    assert.equal(def.meta.section, def.slug, `${def.slug} section`);
  }
});

test("every lazy component target file exists on disk", () => {
  const missing = settingsRouteDefs
    .filter((def) => !existsSync(join(settingsRoot, def.file)))
    .map((def) => def.file);
  assert.deepEqual(missing, [], `missing lazy component targets: ${missing.join(", ")}`);
});

test("settingsRoutes registers the parent layout with one child per definition", () => {
  assert.equal(settingsRoutes.length, 1);
  const parent = settingsRoutes[0];
  assert.equal(parent.path, "/settings");
  assert.equal(parent.meta?.requiresAuth, true);
  assert.ok(typeof parent.component === "function");
  assert.equal(parent.children.length, settingsRouteDefs.length);
  for (const [index, child] of parent.children.entries()) {
    const def = settingsRouteDefs[index];
    assert.equal(child.name, def.name);
    assert.equal(child.path, def.slug);
    assert.deepEqual(child.meta, def.meta);
    assert.ok(typeof child.component === "function");
  }
});
