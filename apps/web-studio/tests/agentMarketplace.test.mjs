import test from "node:test";
import assert from "node:assert/strict";
import {
  mergeMarketplace,
  declaredCapabilities,
  visibleCapabilities,
  summarizeInvocation,
  statusTransition,
  isCredentialReference,
  credentialHint,
  installPayload,
  validateInstall,
  filterCatalog,
  catalogStatusLabel,
  catalogInstallPayload,
  validateCatalogInstall,
  protocolLabel,
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

const makeCatalogEntry = (overrides = {}) => ({
  plugin_key: "market-agent-a",
  name: "Market Agent A",
  description: "Summarizes market notes.",
  version: "1.2.0",
  protocol: "http_json",
  enabled: true,
  auth_required: false,
  capabilities: [{ name: "summarize", description: "Summarize a document.", risk_level: "low" }],
  installed: false,
  installation_status: "not_installed",
  installation_id: null,
  project_id: null,
  ...overrides,
});

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

test("isCredentialReference accepts URI references and rejects plaintext input", () => {
  assert.equal(isCredentialReference("env://PLUGIN_SECRET_TOKEN"), true);
  assert.equal(isCredentialReference("vault://team/agent/token"), true);
  assert.equal(isCredentialReference("env://PLUGIN_SECRET_MARKET_AGENT_TOKEN"), true);
  assert.equal(isCredentialReference("sk-abc123secret"), false);
  assert.equal(isCredentialReference("Bearer abc123"), false);
  assert.equal(isCredentialReference("env://has space"), false);
  assert.equal(isCredentialReference("env://line\nbreak"), false);
  assert.equal(isCredentialReference("plain-string"), false);
  assert.equal(isCredentialReference(""), false);
  assert.equal(isCredentialReference("ENV://UPPER_SCHEME"), false);
  assert.equal(isCredentialReference("env://" + "a".repeat(201)), false);
});

test("credentialHint reports auth state without exposing the secret", () => {
  const authDefinition = makeDefinition({ auth_required: true });
  assert.equal(credentialHint(authDefinition, false), "Credential reference required");
  assert.equal(
    credentialHint(authDefinition, true),
    "Credential configured (value is not stored by the platform)",
  );
  assert.equal(credentialHint(makeDefinition({ auth_required: false }), false), "");
});

test("installPayload carries credential_ref only for auth-required plugins", () => {
  const authDefinition = makeDefinition({ id: "def-auth", auth_required: true });
  assert.deepEqual(installPayload(authDefinition, "  env://PLUGIN_SECRET_TOKEN  "), {
    plugin_id: "def-auth",
    granted_capabilities: [],
    credential_ref: "env://PLUGIN_SECRET_TOKEN",
  });
  const openDefinition = makeDefinition({ id: "def-open", auth_required: false });
  assert.deepEqual(installPayload(openDefinition, "env://SHOULD_NOT_SEND"), {
    plugin_id: "def-open",
    granted_capabilities: [],
  });
});

test("validateInstall blocks missing or plaintext credentials for auth-required plugins", () => {
  const authDefinition = makeDefinition({ auth_required: true });
  const missing = validateInstall(authDefinition, "");
  assert.equal(missing.ok, false);
  assert.match(missing.errors[0], /credential reference is required/i);
  assert.equal(validateInstall(authDefinition, "sk-abc123").ok, false);
  assert.equal(validateInstall(authDefinition, "Bearer token").ok, false);
  assert.equal(validateInstall(authDefinition, "env://has space").ok, false);
  assert.equal(validateInstall(authDefinition, "env://" + "a".repeat(201)).ok, false);
  assert.equal(validateInstall(authDefinition, "not-a-uri").ok, false);
  assert.equal(validateInstall(authDefinition, "env://PLUGIN_SECRET_TOKEN").ok, true);
});

test("validateInstall rejects references on plugins that do not accept credentials", () => {
  const openDefinition = makeDefinition({ auth_required: false });
  assert.equal(validateInstall(openDefinition, "").ok, true);
  const rejected = validateInstall(openDefinition, "env://SHOULD_NOT_SEND");
  assert.equal(rejected.ok, false);
  assert.match(rejected.errors[0], /does not accept a credential reference/i);
});

test("catalogStatusLabel maps installation states to badge labels and tones", () => {
  assert.deepEqual(catalogStatusLabel(makeCatalogEntry({ installation_status: "installed" })), {
    label: "Installed",
    type: "success",
  });
  assert.deepEqual(catalogStatusLabel(makeCatalogEntry({ installation_status: "disabled" })), {
    label: "Disabled",
    type: "warning",
  });
  assert.deepEqual(catalogStatusLabel(makeCatalogEntry({ installation_status: "uninstalled" })), {
    label: "Uninstalled",
    type: "info",
  });
  assert.deepEqual(catalogStatusLabel(makeCatalogEntry({ installation_status: "not_installed" })), {
    label: "Not installed",
    type: "info",
  });
});

test("filterCatalog matches name, description and capability names case-insensitively", () => {
  const entries = [
    makeCatalogEntry({ plugin_key: "a", name: "Weather Agent", description: "Reports weather forecasts." }),
    makeCatalogEntry({
      plugin_key: "b",
      name: "Docs Agent",
      description: "Handles market research summaries.",
    }),
    makeCatalogEntry({
      plugin_key: "c",
      name: "Charter Agent",
      description: "Files charter paperwork.",
      capabilities: [{ name: "draft_reply", description: "Draft a reply.", risk_level: "medium" }],
    }),
  ];
  assert.deepEqual(filterCatalog(entries, { query: "weather" }).map((item) => item.plugin_key), ["a"]);
  assert.deepEqual(filterCatalog(entries, { query: "MARKET" }).map((item) => item.plugin_key), ["b"]);
  assert.deepEqual(filterCatalog(entries, { query: "draft" }).map((item) => item.plugin_key), ["c"]);
  assert.deepEqual(filterCatalog(entries, { query: "nomatch" }), []);
});

test("filterCatalog applies exact protocol match and installed-only toggle", () => {
  const entries = [
    makeCatalogEntry({ plugin_key: "a", protocol: "http_json", installed: true, installation_status: "installed" }),
    makeCatalogEntry({ plugin_key: "b", protocol: "mcp", installed: false, installation_status: "not_installed" }),
    makeCatalogEntry({ plugin_key: "c", protocol: "a2a", installed: true, installation_status: "installed" }),
  ];
  assert.deepEqual(filterCatalog(entries, { protocol: "mcp" }).map((item) => item.plugin_key), ["b"]);
  assert.deepEqual(
    filterCatalog(entries, { installedOnly: true }).map((item) => item.plugin_key),
    ["a", "c"],
  );
  assert.deepEqual(
    filterCatalog(entries, { protocol: "a2a", installedOnly: true }).map((item) => item.plugin_key),
    ["c"],
  );
});

test("protocolLabel renders known protocols and passes unknown values through", () => {
  assert.equal(protocolLabel("http_json"), "HTTP JSON");
  assert.equal(protocolLabel("mcp"), "MCP");
  assert.equal(protocolLabel("a2a"), "A2A");
  assert.equal(protocolLabel("openai_compatible"), "OpenAI-compatible");
  assert.equal(protocolLabel("custom_grpc"), "custom_grpc");
});

test("catalogInstallPayload identifies the plugin by key and version, credential optional", () => {
  const entry = makeCatalogEntry({ plugin_key: "market-agent-a", version: "1.2.0" });
  assert.deepEqual(catalogInstallPayload(entry, ""), {
    plugin_key: "market-agent-a",
    plugin_version: "1.2.0",
    granted_capabilities: [],
  });
  assert.deepEqual(catalogInstallPayload(entry, "  env://PLUGIN_SECRET_TOKEN  "), {
    plugin_key: "market-agent-a",
    plugin_version: "1.2.0",
    granted_capabilities: [],
    credential_ref: "env://PLUGIN_SECRET_TOKEN",
  });
});

test("catalog install blocks an auth-required entry without a reference", () => {
  const entry = makeCatalogEntry({ auth_required: true });
  const validation = validateCatalogInstall(entry, "");
  assert.equal(validation.ok, false);
  assert.match(validation.errors[0], /credential reference is required/i);
  // The blocked install payload carries no credential_ref field at all.
  assert.deepEqual(catalogInstallPayload(entry, ""), {
    plugin_key: "market-agent-a",
    plugin_version: "1.2.0",
    granted_capabilities: [],
  });
});

test("catalog install rejects plaintext secrets instead of sending them to the API", () => {
  const entry = makeCatalogEntry({ auth_required: true });
  for (const secret of ["hunter2", "sk-xxx", "sk-live-abc123", "Bearer abc123"]) {
    const validation = validateCatalogInstall(entry, secret);
    assert.equal(validation.ok, false);
    assert.notEqual(validation.errors.length, 0);
  }
});

test("catalog install accepts env and vault URI references into the payload", () => {
  const entry = makeCatalogEntry({ auth_required: true });
  for (const reference of ["vault://platform/github", "env://PLUGIN_SECRET_FOO"]) {
    assert.equal(validateCatalogInstall(entry, reference).ok, true);
    assert.deepEqual(catalogInstallPayload(entry, reference), {
      plugin_key: "market-agent-a",
      plugin_version: "1.2.0",
      granted_capabilities: [],
      credential_ref: reference,
    });
  }
});

test("catalog install rejects references on entries that do not accept credentials", () => {
  const entry = makeCatalogEntry({ auth_required: false });
  assert.equal(validateCatalogInstall(entry, "").ok, true);
  const rejected = validateCatalogInstall(entry, "env://PLUGIN_SECRET_SHOULD_NOT_SEND");
  assert.equal(rejected.ok, false);
  assert.match(rejected.errors[0], /does not accept a credential reference/i);
});

test("protocolLabel renders all four supported protocols", () => {
  assert.equal(protocolLabel("http_json"), "HTTP JSON");
  assert.equal(protocolLabel("mcp"), "MCP");
  assert.equal(protocolLabel("a2a"), "A2A");
  assert.equal(protocolLabel("openai_compatible"), "OpenAI-compatible");
});

test("filterCatalog matches the openai_compatible protocol exactly", () => {
  const entries = [
    makeCatalogEntry({ plugin_key: "a", protocol: "http_json" }),
    makeCatalogEntry({ plugin_key: "b", protocol: "openai_compatible" }),
    makeCatalogEntry({ plugin_key: "c", protocol: "mcp" }),
    makeCatalogEntry({ plugin_key: "d", protocol: "a2a" }),
  ];
  assert.deepEqual(
    filterCatalog(entries, { protocol: "openai_compatible" }).map((item) => item.plugin_key),
    ["b"],
  );
  assert.deepEqual(
    filterCatalog(entries, { protocol: "a2a" }).map((item) => item.plugin_key),
    ["d"],
  );
});
