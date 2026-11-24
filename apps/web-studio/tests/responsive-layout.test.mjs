import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = join(dirname(fileURLToPath(import.meta.url)), "..", "src");
const read = (path) => readFileSync(join(root, path), "utf8");

/**
 * Layout regression guard. Both complaints that started this work were
 * sizing bugs, not styling opinions: a chat pinned into a 1400px strip with a
 * 350px subtraction leaves the composer under the fold on a 1080p screen, and a
 * two-class grid never collapses on a phone. These assertions pin the fixes so
 * a later tweak cannot silently undo them.
 */

test("working canvases take the full viewport width", () => {
  const app = read("App.vue");
  assert.match(app, /shell-wide/, "App shell must expose the wide variant");
  assert.match(app, /WIDE_SECTIONS/, "the wide variant must be route-driven");
  assert.match(read("styles.css"), /\.shell-wide \{ width: calc\(100% - 32px\); max-width: none; \}/);
});

test("the chat claims the measured remainder of the viewport", () => {
  const chat = read("components/ControlAgentChatView.vue");
  assert.match(chat, /chatHeight/, "chat must size itself from the measured top offset");
  assert.match(chat, /fitChat\(\)/, "chat must re-measure on mount");
  assert.match(chat, /100dvh - \$\{Math\.round\(top\)\}px/, "height must be derived from the real offset");
  assert.match(chat, /addEventListener\("resize", fitChat\)/, "chat must re-measure on resize");
  assert.doesNotMatch(chat, /height: calc\(100vh - 350px\)/, "the fixed 350px subtraction must be gone");
});

test("the chat is a reading column, not a stretched canvas", () => {
  const chat = read("components/ControlAgentChatView.vue");
  assert.match(chat, /\.chat-column \{[\s\S]*max-width: 1040px/, "messages need a readable measure");
  assert.match(chat, /\.bubble-body \{[\s\S]*font-size: 14px/, "bubble copy must match chat-client sizing");
});

test("the workflow canvas and inspector split the viewport", () => {
  const studio = read("components/WorkflowStudioView.vue");
  assert.match(studio, /--studio-canvas-height/, "canvas height must be a measured variable");
  assert.match(read("styles.css"), /height: var\(--studio-canvas-height/);
});

test("the chat grid collapses on a phone despite its two-class selector", () => {
  const css = read("styles.css");
  assert.match(css, /@media \(max-width: 900px\) \{\n  \.workspace-grid\.chat-layout \{ grid-template-columns: minmax\(0, 1fr\); \}/);
  const chat = read("components/ControlAgentChatView.vue");
  assert.match(chat, /@media \(max-width: 900px\)/, "the composer must adapt below 900px");
  assert.match(chat, /\.thread-rail \{[\s\S]*overflow-x: auto/, "the rail becomes a horizontal strip on a phone");
  assert.match(css, /\.main-nav \{\n    flex-wrap: nowrap;/, "phone navigation scrolls instead of stacking");
});
