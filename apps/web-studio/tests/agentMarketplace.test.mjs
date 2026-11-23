import test from "node:test";
import assert from "node:assert/strict";
import {
  mergeMarketplace,
  declaredCapabilities,
  visibleCapabilities,
  summarizeInvocation,
  statusTransition,
} from "../src/domain/agentMarketplace.ts";

const makeDefinition = (overrides = {}) => ({
  id: "def-" + Math.random(),
  tenant_id: "tenant-1",
  plugin_key: "plugin-a",
  name: "Plugin A",
  description: "Registered agent plugin.",
  version: "1.0.0",
  protocol: "http_json",
  endpoint_url: "https://example.invalid",
  auth_required: false,
  capabilities: ["summarize"],
  input_schema: null,
  output_schema: null,
  timeout_seconds: null,
  max_concurrency: null,
  enabled: true,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  ...overrides,
});

const makeInstallation = (pluginKey, overrides = {}) => ({
  id: "inst-" + Math.random(),
  tenant_id: "tenant-1",
  project_id: "project-1",
  plugin: makeDefinition({ plugin_key: pluginKey }),
  plugin_key: pluginKey,
  plugin_version: "1.0.0",
  status: "installed",
  granted_capabilities: [],
  credential_configured: false,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  ...overrides,
});

const findEntry = (entries, pluginKey) =>
  entries.find((entry) => entry.definition.plugin_key === pluginKey);

test("mergeMarketplace marks a plugin without installation as not installed", () => {
  const entries = mergeMarketplace([makeDefinition({ plugin_key: "plugin-a" })], []);
  assert.equal(entries.length, 1);
  const entry = entries[0];
  assert.equal(entry.installStatus, "not_installed");
  assert.equal(entry.installation, null);
  assert.equal(entry.canInstall, true);
  assert.equal(entry.canEnable, false);
  assert.equal(entry.canDisable, false);
  assert.equal(entry.canUninstall, false);
});

test("mergeMarketplace exposes disable and uninstall for an installed plugin", () => {
  const installation = makeInstallation("plugin-a", { status: "installed" });
  const entries = mergeMarketplace([makeDefinition({ plugin_key: "plugin-a" })], [installation]);
  const entry = findEntry(entries, "plugin-a");
  assert.equal(entry.installStatus, "installed");
  assert.equal(entry.canInstall, false);
  assert.equal(entry.canEnable, false);
  assert.equal(entry.canDisable, true);
  assert.equal(entry.canUninstall, true);
});

test("mergeMarketplace exposes enable and uninstall for a disabled plugin", () => {
  const installation = makeInstallation("plugin-a", { status: "disabled" });
  const entries = mergeMarketplace([makeDefinition({ plugin_key: "plugin-a" })], [installation]);
  const entry = findEntry(entries, "plugin-a");
  assert.equal(entry.installStatus, "disabled");
  assert.equal(entry.canInstall, false);
  assert.equal(entry.canEnable, true);
  assert.equal(entry.canDisable, false);
  assert.equal(entry.canUninstall, true);
});

test("uninstalled is a terminal state that only allows reinstall", () => {
  const installation = makeInstallation("plugin-a", { status: "uninstalled" });
  const entries = mergeMarketplace([makeDefinition({ plugin_key: "plugin-a" })], [installation]);
  const entry = findEntry(entries, "plugin-a");
  assert.equal(entry.installStatus, "uninstalled");
  assert.equal(entry.canInstall, true);
  assert.equal(entry.canEnable, false);
  assert.equal(entry.canDisable, false);
  assert.equal(entry.canUninstall, false);
  assert.equal(statusTransition("uninstalled", "enable"), null);
});

test("mergeMarketplace keeps the newest installation record per plugin key", () => {
  const older = makeInstallation("plugin-a", {
    id: "inst-older",
    status: "installed",
    updated_at: "2026-01-01T00:00:00Z",
  });
  const newer = makeInstallation("plugin-a", {
    id: "inst-newer",
    status: "disabled",
    updated_at: "2026-02-01T00:00:00Z",
  });
  const entries = mergeMarketplace([makeDefinition({ plugin_key: "plugin-a" })], [older, newer]);
  const entry = findEntry(entries, "plugin-a");
  assert.equal(entry.installation.id, "inst-newer");
  assert.equal(entry.installStatus, "disabled");
});

test("declaredCapabilities falls back to the capabilities string list without a manifest", () => {
  const definition = makeDefinition({ capabilities: ["summarize", "draft_reply"] });
  const capabilities = declaredCapabilities(definition);
  assert.deepEqual(
    capabilities.map((item) => item.name),
    ["summarize", "draft_reply"],
  );
  for (const capability of capabilities) {
    assert.equal(capability.description, "");
    assert.equal(capability.risk_level, "");
    assert.equal(capability.timeout_seconds, null);
    assert.deepEqual(capability.input_schema, {});
  }
});

test("declaredCapabilities prefers detailed manifest capability entries", () => {
  const definition = makeDefinition({
    capabilities: ["legacy-only"],
    manifest: {
      capabilities: [
        {
          name: "summarize",
          description: "Summarize a document.",
          input_schema: { type: "object", properties: { text: { type: "string" } } },
          output_schema: { type: "object" },
          risk_level: "low",
          timeout_seconds: 10,
        },
      ],
    },
  });
  const capabilities = declaredCapabilities(definition);
  assert.equal(capabilities.length, 1);
  assert.equal(capabilities[0].name, "summarize");
  assert.equal(capabilities[0].description, "Summarize a document.");
  assert.equal(capabilities[0].risk_level, "low");
  assert.equal(capabilities[0].timeout_seconds, 10);
  assert.deepEqual(capabilities[0].input_schema, {
    type: "object",
    properties: { text: { type: "string" } },
  });
});

test("declaredCapabilities falls back when manifest.capabilities is an empty array", () => {
  const definition = makeDefinition({
    capabilities: ["fallback"],
    manifest: { capabilities: [] },
  });
  const capabilities = declaredCapabilities(definition);
  assert.deepEqual(capabilities.map((item) => item.name), ["fallback"]);
});

test("visibleCapabilities keeps only capabilities from installed installations", () => {
  const installed = makeInstallation("plugin-a", { id: "inst-installed", status: "installed" });
  const disabled = makeInstallation("plugin-b", { id: "inst-disabled", status: "disabled" });
  const uninstalled = makeInstallation("plugin-c", { id: "inst-uninstalled", status: "uninstalled" });
  const response = {
    capabilities: [
      {
        name: "summarize",
        plugin_key: "plugin-a",
        plugin_version: "1.0.0",
        protocol: "http_json",
        risk_level: "low",
        installation_id: "inst-installed",
        input_schema: {},
        last_invocation: null,
      },
      {
        name: "draft",
        plugin_key: "plugin-b",
        plugin_version: "1.0.0",
        protocol: "mcp",
        risk_level: "medium",
        installation_id: "inst-disabled",
        input_schema: {},
        last_invocation: null,
      },
      {
        name: "archive",
        plugin_key: "plugin-c",
        plugin_version: "1.0.0",
        protocol: "a2a",
        risk_level: "high",
        installation_id: "inst-uninstalled",
        input_schema: {},
        last_invocation: null,
      },
      {
        name: "orphan",
        plugin_key: "plugin-d",
        plugin_version: "1.0.0",
        protocol: "http_json",
        risk_level: "low",
        installation_id: "inst-missing",
        input_schema: {},
        last_invocation: null,
      },
    ],
  };
  const visible = visibleCapabilities(response, [installed, disabled, uninstalled]);
  assert.equal(visible.length, 1);
  assert.equal(visible[0].name, "summarize");
});

test("visibleCapabilities drops entries whose plugin key does not match the installation", () => {
  const installed = makeInstallation("plugin-a", { id: "inst-installed", status: "installed" });
  const response = {
    capabilities: [
      {
        name: "mismatch",
        plugin_key: "plugin-other",
        plugin_version: "1.0.0",
        protocol: "http_json",
        risk_level: "low",
        installation_id: "inst-installed",
        input_schema: {},
        last_invocation: null,
      },
    ],
  };
  assert.equal(visibleCapabilities(response, [installed]).length, 0);
});

test("summarizeInvocation maps success, failed, rejected and null invocations", () => {
  assert.deepEqual(summarizeInvocation(null), {
    label: "No invocations yet",
    tone: "neutral",
    errorCode: "",
  });
  assert.deepEqual(
    summarizeInvocation({ status: "success", error_code: "", created_at: "2026-01-01T00:00:00Z" }),
    { label: "Success", tone: "success", errorCode: "" },
  );
  assert.deepEqual(
    summarizeInvocation({ status: "failed", error_code: "E_TIMEOUT", created_at: "2026-01-01T00:00:00Z" }),
    { label: "Failed", tone: "danger", errorCode: "E_TIMEOUT" },
  );
  assert.deepEqual(
    summarizeInvocation({ status: "rejected", error_code: "E_POLICY", created_at: "2026-01-01T00:00:00Z" }),
    { label: "Rejected", tone: "danger", errorCode: "E_POLICY" },
  );
});

test("statusTransition allows the legal lifecycle transitions", () => {
  assert.equal(statusTransition("not_installed", "install"), "installed");
  assert.equal(statusTransition("uninstalled", "install"), "installed");
  assert.equal(statusTransition("disabled", "enable"), "installed");
  assert.equal(statusTransition("installed", "disable"), "disabled");
  assert.equal(statusTransition("installed", "uninstall"), "uninstalled");
});

test("statusTransition rejects illegal lifecycle transitions", () => {
  assert.equal(statusTransition("installed", "enable"), null);
  assert.equal(statusTransition("disabled", "disable"), null);
  assert.equal(statusTransition("disabled", "uninstall"), null);
  assert.equal(statusTransition("not_installed", "enable"), null);
  assert.equal(statusTransition("not_installed", "disable"), null);
  assert.equal(statusTransition("not_installed", "uninstall"), null);
  assert.equal(statusTransition("uninstalled", "enable"), null);
  assert.equal(statusTransition("uninstalled", "disable"), null);
  assert.equal(statusTransition("uninstalled", "uninstall"), null);
});
