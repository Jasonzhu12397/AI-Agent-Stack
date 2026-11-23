import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";

// A settings template that references a class with no definition still
// renders, but the layout silently collapses and Element Plus light defaults
// leak through. This test keeps the stylesheet and the templates in step.

const SRC = path.resolve("src");
const CSS_FILES = ["src/styles.css"];

function collectFiles(root, extension) {
  const found = [];
  const walk = (dir) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) walk(full);
      else if (entry.name.endsWith(extension)) found.push(full);
    }
  };
  walk(root);
  return found;
}

function classesUsedInTemplates() {
  const used = new Set();
  for (const file of collectFiles(path.join(SRC, "settings"), ".vue")) {
    const text = fs.readFileSync(file, "utf8");
    for (const match of text.matchAll(/class="([^"]*)"/g)) {
      for (const token of match[1].split(/\s+/)) {
        if (token.startsWith("settings-")) used.add(token);
      }
    }
  }
  return used;
}

function classesDefined() {
  const defined = new Set();
  for (const file of CSS_FILES) {
    const text = fs.readFileSync(path.join(SRC, "..", file), "utf8");
    for (const match of text.matchAll(/\.(settings-[A-Za-z0-9_-]+)/g)) defined.add(match[1]);
  }
  return defined;
}

test("every settings class used by a template is defined in the stylesheet", () => {
  const used = classesUsedInTemplates();
  const defined = classesDefined();
  const missing = [...used].filter((name) => !defined.has(name)).sort();
  assert.deepEqual(missing, [], `undefined settings classes: ${missing.join(", ")}`);
});

test("the Element Plus token layer overrides the light text colour", () => {
  const css = fs.readFileSync(path.resolve("src/styles.css"), "utf8");
  const match = css.match(/--el-text-color-primary:\s*([^;]+);/);
  assert.ok(match, "the dark token layer is expected at :root");
  const value = match[1].trim().toLowerCase();
  assert.notEqual(value, "#303133", "the library default must not survive");
  assert.match(value, /^#[0-9a-f]{6}/);
});
