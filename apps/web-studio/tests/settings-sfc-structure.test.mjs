import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";

const settingsDir = new URL("../src/settings/", import.meta.url);

function pageFiles() {
  return readdirSync(settingsDir)
    .filter((name) => statSync(new URL(name, settingsDir)).isDirectory())
    .map((name) => new URL(`${name}/index.vue`, settingsDir))
    .filter((url) => {
      try {
        statSync(url);
        return true;
      } catch {
        return false;
      }
    });
}

test("no settings page leaks a style block into its template", () => {
  for (const url of pageFiles()) {
    const source = readFileSync(url, "utf8");
    const styleAt = source.indexOf("<style");
    if (styleAt === -1) continue;
    const lastTemplateEnd = source.lastIndexOf("</template>");
    assert.ok(
      styleAt > lastTemplateEnd,
      `${url.pathname} declares <style> before the template ends; the CSS would render as page text`,
    );
  }
});
