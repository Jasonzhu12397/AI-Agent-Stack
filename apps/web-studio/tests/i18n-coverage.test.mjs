import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = join(dirname(fileURLToPath(import.meta.url)), "..", "src");
const CJK = /[\u4e00-\u9fff]/;

/**
 * Language switch is implemented as a DOM translator keyed by the English
 * source string, so a template only localizes when its literal lives in the
 * dictionary. This guard fails the build when a surface ships either a hard
 * coded Chinese literal or an English literal that never made it into zh.ts.
 */
function walk(dir) {
  return readdirSync(dir).flatMap((name) => {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) return walk(full);
    return full.endsWith(".vue") ? [full] : [];
  });
}

function loadDictionary() {
  const merged = {};
  const files = [
    ...readdirSync(join(root, "i18n")).filter((name) => name.endsWith(".ts")).map((name) => join(root, "i18n", name)),
    ...readdirSync(join(root, "i18n", "settings"))
      .filter((name) => name.endsWith(".zh.ts"))
      .map((name) => join(root, "i18n", "settings", name)),
  ];
  for (const file of files) {
    const entry = /"((?:[^"\\]|\\.)*)":\s*"((?:[^"\\]|\\.)*)"/g;
    let match;
    while ((match = entry.exec(readFileSync(file, "utf8")))) {
      if (!(match[1] in merged)) merged[match[1]] = match[2];
    }
  }
  return merged;
}

function templateLiterals(source) {
  const start = source.indexOf("<template>");
  const end = source.lastIndexOf("</template>");
  if (start < 0 || end < 0) return [];
  const template = source.slice(start + 10, end).replace(/<!--[\s\S]*?-->/g, "");
  const literals = [];
  const node = />([^<>${}]{2,})</g;
  let match;
  while ((match = node.exec(template))) {
    const text = match[1].replace(/\s+/g, " ").trim();
    if (!text || !/[A-Za-z\u4e00-\u9fff]/.test(text)) continue;
    literals.push(text);
  }
  return literals;
}

// Sample values typed into inputs, not copy the translator is expected to own.
const SAMPLE_VALUES = new Set([
  "bond_data",
  "ccd-data",
  "ens1f0,ens1f1",
  "ironic-auth",
  "optional, 1-4094",
  "vault/bmc/master-0",
  "vault/ca/lab",
]);

const PROVIDER_LABELS = new Set(["中文"]);
const dictionary = loadDictionary();
const files = walk(root);

test("no vue template ships a hard coded Chinese literal", () => {
  const offenders = [];
  for (const file of files) {
    for (const literal of templateLiterals(readFileSync(file, "utf8"))) {
      if (CJK.test(literal) && !PROVIDER_LABELS.has(literal)) {
        offenders.push(`${file}: ${literal}`);
      }
    }
  }
  assert.deepEqual(offenders, [], `hard coded Chinese outside the dictionary:\n${offenders.join("\n")}`);
});

test("every English template literal has a Chinese translation", () => {
  const missing = new Set();
  for (const file of files) {
    for (const literal of templateLiterals(readFileSync(file, "utf8"))) {
      if (!CJK.test(literal) && !(literal in dictionary)) missing.add(literal);
    }
  }
  assert.deepEqual([...missing], [], `missing zh entries:\n${[...missing].join("\n")}`);
});

/** Placeholders, titles and aria labels only localize when their literal is in the dictionary. */
function humanLiteral(value) {
  if (!/[A-Za-z]/.test(value)) return false;
  if (["(", ")", "'", '"', "?", "+", "{", "}", "$", "||", "??", "=>", "."].some((token) => value.includes(token))) {
    return false;
  }
  if (/^[a-z][a-zA-Z0-9]*$/.test(value)) return false;
  if (/^(https?:|\/|env:|vault:)/.test(value)) return false;
  return true;
}

test("every translated attribute literal has a Chinese entry", () => {
  const missing = new Set();
  for (const file of files) {
    const source = readFileSync(file, "utf8");
    const start = source.indexOf("<template>");
    const end = source.lastIndexOf("</template>");
    if (start < 0 || end < 0) continue;
    const template = source.slice(start + 10, end);
    const attr = /(?:placeholder|aria-label|title|label|description)="([^"]{3,})"/g;
    let match;
    while ((match = attr.exec(template))) {
      const value = match[1];
      if (value.includes("{{")) continue;
      if (!humanLiteral(value) || SAMPLE_VALUES.has(value)) continue;
      if (!(value in dictionary)) missing.add(value);
    }
  }
  assert.deepEqual([...missing], [], `missing zh entries for attributes:\n${[...missing].join("\n")}`);
});

test("every t()/tf() key used at runtime has a Chinese translation", () => {
  const missing = new Set();
  const walkAll = (dir) =>
    readdirSync(dir).flatMap((name) => {
      const full = join(dir, name);
      if (statSync(full).isDirectory()) return walkAll(full);
      return /[.](vue|ts)$/.test(full) ? [full] : [];
    });
  for (const file of walkAll(root)) {
    if (file.includes("/i18n/")) continue;
    const source = readFileSync(file, "utf8");
    const call = /\b(?:tf|t)\(\s*("(?:[^"\\]|\\.)*")/g;
    let match;
    while ((match = call.exec(source))) {
      const key = JSON.parse(match[1]);
      if (key && !(key in dictionary)) missing.add(key);
    }
  }
  assert.deepEqual([...missing], [], `missing zh entries for runtime keys:\n${[...missing].join("\n")}`);
});

test("the chat composer localizes its interpolated labels through t()", () => {
  const chat = readFileSync(join(root, "components", "ControlAgentChatView.vue"), "utf8");
  for (const label of ["Project", "Skill", "Skills", "Plugin", "Plugins", "Authorize", "Ready", "No messages yet"]) {
    assert.match(chat, new RegExp(`t\\("${label}"\\)`), `${label} must go through t() to keep the switch total`);
  }
});

test("a plugin that still needs a credential offers authorization instead of selection", () => {
  const chat = readFileSync(join(root, "components", "ControlAgentChatView.vue"), "utf8");
  assert.match(chat, /function pluginNeedsAuth/, "pluginNeedsAuth helper is required");
  assert.match(
    chat,
    /pluginNeedsAuth\(plugin\) ?\? openPlugin\(plugin\.key\) : togglePlugin\(plugin\.id\)/,
    "unauthorized plugins must open their authorization page instead of being selectable",
  );
  assert.match(chat, /t\("Authorize"\)/, "unauthorized plugins must offer an authorize jump");
});

test("every model gateway error code has a Chinese translation", () => {
  const gateway = readFileSync(join(root, "..", "..", "platform-api", "core", "model_gateway.py"), "utf8");
  const codes = new Set();
  const raised = /ModelGatewayError\(\s*"([A-Z0-9_]+)"/g;
  let match;
  while ((match = raised.exec(gateway))) codes.add(match[1]);
  const table = readFileSync(join(root, "i18n", "serverErrors.ts"), "utf8");
  const missing = [...codes].filter((code) => !table.includes(`${code}:`));
  assert.deepEqual(missing, [], `backend error codes without a zh message:\n${missing.join("\n")}`);
});
