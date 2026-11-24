// Pure domain helpers for the Agent Marketplace. This module has no Vue or
// fetch dependency; it only transforms records that mirror the frozen backend
// contract. Fields the backend may omit are treated as optional.

export type AgentProtocol =
  | "http_json"
  | "mcp"
  | "a2a"
  | "openai_compatible"
  | "open_data";

export type InstallationStatus = "installed" | "disabled" | "uninstalled";

export type InstallStatus = "not_installed" | InstallationStatus;

export type InvocationStatus = "success" | "failed" | "rejected";

export type LifecycleAction = "install" | "enable" | "disable" | "uninstall";

export interface AgentManifestCapability {
  name: string;
  description: string;
  input_schema: Record<string, unknown>;
  output_schema: Record<string, unknown>;
  risk_level: string;
  timeout_seconds: number | null;
}

export interface AgentPluginDefinitionRecord {
  id: string;
  tenant_id: string;
  plugin_key: string;
  name: string;
  description: string;
  version: string;
  protocol: AgentProtocol;
  endpoint_url: string;
  auth_required: boolean;
  capabilities: string[];
  input_schema: Record<string, unknown> | null;
  output_schema: Record<string, unknown> | null;
  timeout_seconds: number | null;
  max_concurrency: number | null;
  enabled: boolean;
  created_at: string;
  updated_at: string;
  manifest?: {
    capabilities?: Array<{
      name?: string;
      description?: string;
      input_schema?: Record<string, unknown>;
      output_schema?: Record<string, unknown>;
      risk_level?: string;
      timeout_seconds?: number;
    }>;
  } | null;
}

export interface AgentPluginInstallationRecord {
  id: string;
  tenant_id: string;
  project_id: string;
  plugin: AgentPluginDefinitionRecord | null;
  plugin_key?: string;
  plugin_version: string;
  status: InstallationStatus;
  granted_capabilities: string[];
  credential_configured: boolean;
  created_at: string;
  updated_at: string;
}

export interface AgentInvocationRecord {
  status: InvocationStatus;
  error_code: string;
  created_at: string;
}

export interface InstalledCapability {
  name: string;
  plugin_key: string;
  plugin_version: string;
  protocol: string;
  risk_level: string;
  installation_id: string;
  input_schema: Record<string, unknown>;
  last_invocation: AgentInvocationRecord | null;
}

export interface AgentCapabilityResponse {
  capabilities?: InstalledCapability[];
}

export interface MarketplaceEntry {
  definition: AgentPluginDefinitionRecord;
  installation: AgentPluginInstallationRecord | null;
  installStatus: InstallStatus;
  canInstall: boolean;
  canEnable: boolean;
  canDisable: boolean;
  canUninstall: boolean;
}

export interface InvocationSummary {
  label: string;
  tone: "neutral" | "success" | "danger";
  errorCode: string;
}

function installationPluginKey(installation: AgentPluginInstallationRecord): string {
  return installation.plugin_key ?? installation.plugin?.plugin_key ?? "";
}

function latestByPluginKey(
  installations: AgentPluginInstallationRecord[],
): Map<string, AgentPluginInstallationRecord> {
  const latest = new Map<string, AgentPluginInstallationRecord>();
  for (const installation of installations) {
    const key = installationPluginKey(installation);
    if (!key) continue;
    const existing = latest.get(key);
    if (!existing || installation.updated_at >= existing.updated_at) {
      latest.set(key, installation);
    }
  }
  return latest;
}

function actionBits(status: InstallStatus): Pick<
  MarketplaceEntry,
  "canInstall" | "canEnable" | "canDisable" | "canUninstall"
> {
  switch (status) {
    case "installed":
      return { canInstall: false, canEnable: false, canDisable: true, canUninstall: true };
    case "disabled":
      return { canInstall: false, canEnable: true, canDisable: false, canUninstall: true };
    case "uninstalled":
      // Uninstall is a terminal state: the plugin may only be reinstalled,
      // it cannot be enabled or disabled again.
      return { canInstall: true, canEnable: false, canDisable: false, canUninstall: false };
    case "not_installed":
    default:
      return { canInstall: true, canEnable: false, canDisable: false, canUninstall: false };
  }
}

// Merge catalog definitions with project installations, grouped by plugin_key.
// When several installation records exist for the same plugin, the one with the
// newest updated_at wins.
export function mergeMarketplace(
  definitions: AgentPluginDefinitionRecord[],
  installations: AgentPluginInstallationRecord[],
): MarketplaceEntry[] {
  const latest = latestByPluginKey(installations);
  return definitions.map((definition) => {
    const installation = latest.get(definition.plugin_key) ?? null;
    const installStatus: InstallStatus = installation?.status ?? "not_installed";
    return {
      definition,
      installation,
      installStatus,
      ...actionBits(installStatus),
    };
  });
}

// Normalize declared capabilities. Prefer the detailed manifest.capabilities
// entries; fall back to the legacy capabilities string list when the manifest
// or its capabilities array is missing or empty.
export function declaredCapabilities(
  definition: AgentPluginDefinitionRecord,
): AgentManifestCapability[] {
  const manifestCapabilities = definition.manifest?.capabilities;
  if (Array.isArray(manifestCapabilities) && manifestCapabilities.length > 0) {
    return manifestCapabilities.map((capability) => ({
      name: typeof capability.name === "string" ? capability.name : "",
      description: typeof capability.description === "string" ? capability.description : "",
      input_schema: capability.input_schema ?? {},
      output_schema: capability.output_schema ?? {},
      risk_level: typeof capability.risk_level === "string" ? capability.risk_level : "",
      timeout_seconds:
        typeof capability.timeout_seconds === "number" ? capability.timeout_seconds : null,
    }));
  }
  return (definition.capabilities ?? []).map((name) => ({
    name,
    description: "",
    input_schema: {},
    output_schema: {},
    risk_level: "",
    timeout_seconds: null,
  }));
}

// Front-end defense in depth: keep only capabilities whose installation still
// exists, is installed, and has not been uninstalled. The backend already
// filters this way, but the UI must not trust that alone.
export function visibleCapabilities(
  response: AgentCapabilityResponse | InstalledCapability[],
  installations: AgentPluginInstallationRecord[],
): InstalledCapability[] {
  const records = Array.isArray(response) ? response : response.capabilities ?? [];
  const byInstallationId = new Map(installations.map((item) => [item.id, item]));
  return records.filter((capability) => {
    const installation = byInstallationId.get(capability.installation_id);
    if (!installation) return false;
    if (installation.status !== "installed") return false;
    if (installationPluginKey(installation) !== capability.plugin_key) return false;
    return true;
  });
}

// Present the most recent invocation with a display label, tone and optional
// error code. A null invocation means the capability has never been called.
export function summarizeInvocation(
  invocation: AgentInvocationRecord | null,
): InvocationSummary {
  if (!invocation) {
    return { label: "No invocations yet", tone: "neutral", errorCode: "" };
  }
  switch (invocation.status) {
    case "success":
      return { label: "Success", tone: "success", errorCode: "" };
    case "failed":
      return {
        label: "Failed",
        tone: "danger",
        errorCode: invocation.error_code ?? "",
      };
    case "rejected":
      return {
        label: "Rejected",
        tone: "danger",
        errorCode: invocation.error_code ?? "",
      };
    default:
      return { label: "Unknown", tone: "neutral", errorCode: invocation.error_code ?? "" };
  }
}

// Resolve the target status for a lifecycle action. Returns null when the
// transition is illegal. Legal transitions:
//   not_installed -> install -> installed
//   uninstalled   -> install -> installed (reinstall)
//   disabled      -> enable   -> installed
//   installed     -> disable  -> disabled
//   installed     -> uninstall -> uninstalled
export function statusTransition(
  current: InstallStatus,
  action: LifecycleAction,
): InstallationStatus | null {
  if (action === "install" && (current === "not_installed" || current === "uninstalled")) {
    return "installed";
  }
  if (action === "enable" && current === "disabled") return "installed";
  if (action === "disable" && current === "installed") return "disabled";
  if (action === "uninstall" && current === "installed") return "uninstalled";
  return null;
}

// A credential reference is a URI pointing at a secret held outside the
// platform, for example env://PLUGIN_SECRET_TOKEN or
// vault://merchant/agent#token. The platform never stores or echoes the
// resolved secret value.
//
// The bound matches the backend `credential_ref` column (200 characters) and
// the resolver's syntax: a scheme, then a non-empty body with no whitespace.
// The backend accepts the vault `#field` fragment, so the browser must not
// reject it first.
export const CREDENTIAL_REFERENCE_MAX_LENGTH = 200;

const CREDENTIAL_SCHEME_PATTERN = /^[a-z][a-z0-9+.-]*$/;

// Returns true when the value looks like a scheme://path secret reference.
// Whitespace, line breaks, an empty body and an over-long value are rejected.
export function isCredentialReference(value: string): boolean {
  if (typeof value !== "string") return false;
  const trimmed = value.trim();
  if (!trimmed || /\s/.test(trimmed)) return false;
  if (trimmed.length > CREDENTIAL_REFERENCE_MAX_LENGTH) return false;
  const separator = trimmed.indexOf("://");
  if (separator <= 0) return false;
  const scheme = trimmed.slice(0, separator);
  const body = trimmed.slice(separator + 3);
  if (!CREDENTIAL_SCHEME_PATTERN.test(scheme)) return false;
  return body.length > 0;
}

// Heuristic guard against pasting a plaintext secret: whitespace, an
// overlong blob, or common token / authorization-header prefixes.
function looksLikePlaintextSecret(value: string): boolean {
  const trimmed = value.trim();
  if (/\s/.test(trimmed)) return true;
  if (trimmed.length > CREDENTIAL_REFERENCE_MAX_LENGTH) return true;
  if (/^sk-/i.test(trimmed)) return true;
  if (/^Bearer\s/i.test(trimmed)) return true;
  return false;
}

// Human-readable credential state for a plugin. Returns an empty string when
// the plugin does not require authentication.
export function credentialHint(
  definition: AgentPluginDefinitionRecord,
  credentialConfigured = false,
): string {
  if (!definition.auth_required) return "";
  return credentialConfigured
    ? "Credential configured (value is not stored by the platform)"
    : "Credential reference required";
}

export interface AgentInstallPayload {
  plugin_id: string;
  granted_capabilities: string[];
  credential_ref?: string;
}

// Build the install request body. Auth-required plugins carry the trimmed
// reference; plugins without authentication never send a credential_ref
// field, matching the backend contract.
export function installPayload(
  definition: AgentPluginDefinitionRecord,
  credentialRef: string,
): AgentInstallPayload {
  const payload: AgentInstallPayload = {
    plugin_id: definition.id,
    granted_capabilities: [],
  };
  if (definition.auth_required) {
    payload.credential_ref = credentialRef.trim();
  }
  return payload;
}

export interface InstallValidation {
  ok: boolean;
  errors: string[];
}

// Validate an install attempt before it reaches the API. Plaintext-looking
// secrets, missing references for auth-required plugins and references on
// plugins that do not accept them are all rejected with a readable message.
export function validateInstall(
  definition: AgentPluginDefinitionRecord,
  credentialRef: string,
): InstallValidation {
  const errors: string[] = [];
  const ref = (credentialRef ?? "").trim();
  if (definition.auth_required) {
    if (!ref) {
      errors.push("A credential reference is required for this plugin.");
    } else if (looksLikePlaintextSecret(ref)) {
      errors.push(
        "Enter a secret reference URI (for example env://PLUGIN_SECRET_TOKEN), not a plaintext credential.",
      );
    } else if (!isCredentialReference(ref)) {
      errors.push(
        "Credential reference must use scheme://path format (for example env://PLUGIN_SECRET_TOKEN).",
      );
    }
  } else if (ref) {
    errors.push("This plugin does not accept a credential reference.");
  }
  return { ok: errors.length === 0, errors };
}

// ---- Credential rotation ---------------------------------------------------

export interface CredentialRotationPayload {
  credential_ref: string;
}

// Build the rotation body for an existing installation. Rotation changes the
// reference only: the installation, its grants and its workflow bindings stay
// untouched, so an expired token costs one request instead of a reinstall.
export function credentialRotationPayload(credentialRef: string): CredentialRotationPayload {
  return { credential_ref: (credentialRef ?? "").trim() };
}

// Validate a rotation attempt in the browser with the same rules the install
// flow applies: a required, non-plaintext, well-formed reference.
export function validateCredentialRotation(credentialRef: string): InstallValidation {
  const errors: string[] = [];
  const ref = (credentialRef ?? "").trim();
  if (!ref) {
    errors.push("A new credential reference is required.");
  } else if (looksLikePlaintextSecret(ref)) {
    errors.push(
      "Enter a secret reference URI (for example env://PLUGIN_SECRET_TOKEN), not a plaintext credential.",
    );
  } else if (!isCredentialReference(ref)) {
    errors.push(
      "Credential reference must use scheme://path format (for example env://PLUGIN_SECRET_TOKEN).",
    );
  }
  return { ok: errors.length === 0, errors };
}

// ---- Browse catalog (marketplace catalog endpoint) -------------------------

export interface MarketplaceCatalogCapability {
  name: string;
  description: string;
  risk_level: string;
}

export interface MarketplaceCatalogEntry {
  plugin_key: string;
  name: string;
  description: string;
  version: string;
  protocol: string;
  enabled: boolean;
  auth_required: boolean;
  capabilities: MarketplaceCatalogCapability[];
  installed: boolean;
  installation_status: InstallStatus;
  installation_id: string | null;
  project_id: string | null;
}

export interface CatalogFilter {
  query?: string;
  protocol?: string;
  installedOnly?: boolean;
}

export interface CatalogInstallPayload {
  plugin_key: string;
  plugin_version: string;
  granted_capabilities: string[];
  credential_ref?: string;
}

// Human-readable protocol label. Unknown protocols are passed through verbatim.
export function protocolLabel(protocol: string): string {
  switch (protocol) {
    case "http_json":
      return "HTTP JSON";
    case "mcp":
      return "MCP";
    case "a2a":
      return "A2A";
    case "openai_compatible":
      return "OpenAI-compatible";
    default:
      return protocol;
  }
}

// Badge label and Element Plus tag type for a catalog entry's installation state.
export function catalogStatusLabel(
  entry: MarketplaceCatalogEntry,
): { label: string; type: "success" | "warning" | "danger" | "info" } {
  switch (entry.installation_status) {
    case "installed":
      return { label: "Installed", type: "success" };
    case "disabled":
      return { label: "Disabled", type: "warning" };
    case "uninstalled":
      return { label: "Uninstalled", type: "info" };
    case "not_installed":
    default:
      return { label: "Not installed", type: "info" };
  }
}

// Client-side second-pass filtering over the catalog response. The backend
// already filtered server-side; this keeps local search and UI toggles fast.
// The original backend ordering is preserved.
export function filterCatalog(
  entries: MarketplaceCatalogEntry[],
  filter: CatalogFilter = {},
): MarketplaceCatalogEntry[] {
  const query = (filter.query ?? "").trim().toLowerCase();
  const protocol = (filter.protocol ?? "").trim();
  return entries.filter((entry) => {
    if (filter.installedOnly && !entry.installed) return false;
    if (protocol && entry.protocol !== protocol) return false;
    if (query) {
      const haystack = [
        entry.name,
        entry.description,
        ...entry.capabilities.map((capability) => capability.name),
      ]
        .join(" ")
        .toLowerCase();
      if (!haystack.includes(query)) return false;
    }
    return true;
  });
}

// Build the install body for a catalog entry. Catalog entries carry no
// plugin_id, so the plugin is identified by plugin_key plus pinned version.
// A trimmed credential reference is attached only when provided; the
// auth-required gating rules from the definition flow still apply server-side.
export function catalogInstallPayload(
  entry: MarketplaceCatalogEntry,
  credentialRef: string,
): CatalogInstallPayload {
  const payload: CatalogInstallPayload = {
    plugin_key: entry.plugin_key,
    plugin_version: entry.version,
    granted_capabilities: [],
  };
  const ref = (credentialRef ?? "").trim();
  if (ref) {
    payload.credential_ref = ref;
  }
  return payload;
}

// Validate a catalog install attempt before it reaches the API. Auth-required
// entries need a URI reference; plaintext-looking secrets and references on
// entries that do not accept credentials are rejected with a readable message.
// Mirrors validateInstall for the definition-based flow.
export function validateCatalogInstall(
  entry: MarketplaceCatalogEntry,
  credentialRef: string,
): InstallValidation {
  const errors: string[] = [];
  const ref = (credentialRef ?? "").trim();
  if (entry.auth_required) {
    if (!ref) {
      errors.push("A credential reference is required for this plugin.");
    } else if (looksLikePlaintextSecret(ref)) {
      errors.push(
        "Enter a secret reference URI (for example env://PLUGIN_SECRET_TOKEN), not a plaintext credential.",
      );
    } else if (!isCredentialReference(ref)) {
      errors.push(
        "Credential reference must use scheme://path format (for example env://PLUGIN_SECRET_TOKEN).",
      );
    }
  } else if (ref) {
    errors.push("This plugin does not accept a credential reference.");
  }
  return { ok: errors.length === 0, errors };
}
