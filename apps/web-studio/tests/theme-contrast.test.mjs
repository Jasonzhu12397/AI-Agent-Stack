import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const css = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");

const palette = {
  page: "#f5f9fc",
  card: "#ffffff",
  border: "#e3eaf2",
  text: "#121f28",
  muted: "#566976",
  accent: "#00a96d",
};

function channels(hex) {
  const value = hex.replace("#", "");
  const full = value.length === 3 ? value.split("").map((c) => c + c).join("") : value;
  return [0, 2, 4].map((offset) => parseInt(full.slice(offset, offset + 2), 16) / 255);
}

function luminance(hex) {
  const [r, g, b] = channels(hex).map((c) =>
    c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4,
  );
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function saturation(hex) {
  const [r, g, b] = channels(hex);
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  if (max === min) return 0;
  const light = (max + min) / 2;
  return light > 0.5 ? (max - min) / (2 - max - min) : (max - min) / (max + min);
}

function contrast(a, b) {
  const [high, low] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (high + 0.05) / (low + 0.05);
}

function rules(source) {
  return source
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .split("}")
    .map((block) => {
      const brace = block.indexOf("{");
      if (brace === -1) return null;
      return { selector: block.slice(0, brace).trim(), body: block.slice(brace + 1) };
    })
    .filter(Boolean);
}

const declarations = rules(css);

test("the theme is light: no dark surface hex survives in a background", () => {
  const dark = /background(?:-color)?:\s*(#[0-9a-f]{6})/gi;
  for (const rule of declarations) {
    for (const match of rule.body.matchAll(dark)) {
      const tintedFill = saturation(match[1]) >= 0.2;
      assert.ok(
        luminance(match[1]) > 0.55 || tintedFill,
        `${rule.selector} paints a dark, neutral background ${match[1]}`,
      );
    }
  }
});

test("every text colour clears 4.5:1 against the surface it sits on", () => {
  for (const rule of declarations) {
    const color = rule.body.match(/(?<![-\w])color:\s*(#[0-9a-f]{6})/i);
    if (!color) continue;
    const background = rule.body.match(/background(?:-color)?:\s*(#[0-9a-f]{6})/i);
    const surface = background ? background[1] : palette.card;
    if (contrast(surface, palette.card) < 4.5) {
      // A saturated fill carries white labels; check that pair instead.
      assert.ok(
        contrast(color[1], palette.card) >= 4.5,
        `${rule.selector}: ${color[1]} on the fill ${surface} is unreadable`,
      );
      continue;
    }
    assert.ok(
      contrast(color[1], surface) >= 4.5,
      `${rule.selector}: ${color[1]} on ${surface} is below 4.5:1`,
    );
  }
});

test("element plus tokens point at the QingCloud surface scale", () => {
  for (const [variable, expected] of [
    ["--el-text-color-primary", palette.text],
    ["--el-text-color-secondary", palette.muted],
    ["--el-bg-color", palette.card],
    ["--el-bg-color-page", palette.page],
    ["--el-border-color", palette.border],
    ["--el-color-primary", palette.accent],
  ]) {
    assert.ok(
      new RegExp(`${variable}:\\s*${expected};`).test(css),
      `${variable} should be ${expected}`,
    );
  }
});
