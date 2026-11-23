// Pure domain helpers for the Agent Marketplace. This module has no Vue or
// fetch dependency; it only transforms records that mirror the frozen backend
// contract. Fields the backend may omit are treated as optional.

export type AgentProtocol = "http_json" | "mcp" | "a2a";

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
