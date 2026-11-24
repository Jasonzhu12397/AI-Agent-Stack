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
  assert.match(chat, /\.msg \{[\s\S]*max-width: min\(760px, 86%\)/, "messages need a readable measure");
  assert.match(chat, /\.bubble-body \{[\s\S]*font-size: 14px/, "bubble copy must match chat-client sizing");
});

test("the workflow canvas fills the shell instead of a measured offset", () => {
  const studio = read("components/WorkflowStudioView.vue");
  const css = read("styles.css");
  const app = read("App.vue");
  const workspace = read("components/WorkspaceView.vue");
  // The shell owns the viewport height, so the canvas needs no pixel math.
  assert.match(app, /app-shell/, "the shell must mark the body as viewport sized");
  assert.match(css, /#app \{ height: 100%; \}/, "the mount root must pass the height down");
  assert.match(css, /\.shell \{[\s\S]*flex: 1; min-height: 0/, "the shell must fill the viewport column");
  assert.match(workspace, /workspace-workbench/, "the workflow route must declare the workbench layout");
  assert.match(css, /\.workflow-canvas-viewport \{[\s\S]*flex: 1/, "the canvas viewport takes the remainder");
  // One transform drives zoom and pan; scrollbars must not come back.
  assert.match(studio, /class="workflow-canvas-viewport"/);
  assert.match(studio, /class="workflow-canvas-plane"/);
  assert.match(studio, /translate\(\$\{view\.value\.x\}px/);
  assert.doesNotMatch(studio, /fitStudio|--studio-canvas-height/, "no measured canvas offset");
  assert.doesNotMatch(css, /--studio-canvas-height/, "no measured canvas offset in css");
});

test("the chat surface drops the sidebar and hides the rail on a phone", () => {
  const css = read("styles.css");
  const chat = read("components/ControlAgentChatView.vue");
  const workspace = read("components/WorkspaceView.vue");
  assert.match(workspace, /grid-chat/, "the chat route uses a single column grid");
  assert.match(css, /\.workspace-chat \{ padding: 10px 0 0;/);
  assert.match(chat, /@media \(max-width: 1200px\) \{\n  \.bar-rail/, "the thread rail collapses into a drawer");
  assert.match(chat, /\.thread-rail\.open \{[\s\S]*position: absolute/, "the drawer overlays the transcript");
  assert.match(chat, /@media \(max-width: 900px\)/, "the composer must adapt below 900px");
  assert.match(css, /\.main-nav \{\n    flex-wrap: nowrap;/, "phone navigation scrolls instead of stacking");
});
