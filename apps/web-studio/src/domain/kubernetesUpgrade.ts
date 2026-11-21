export function getKubernetesUpgradeFailure(error: Record<string, unknown> | null): string {
  if (!error) return "";
  const message = typeof error.message === "string" ? error.message.trim() : "";
  const code = typeof error.code === "string" ? error.code.trim() : "";
  if (message && code) return message + " (" + code + ")";
  return message || code;
}

export interface KubernetesUpgradePreflightCheck {
  name: string;
  ok: boolean;
  detail: string;
}

function toChecks(raw: unknown): KubernetesUpgradePreflightCheck[] {
  return Array.isArray(raw)
    ? raw
        .filter((check): check is Record<string, unknown> =>
          Boolean(check) && typeof check === "object" && !Array.isArray(check),
        )
        .map((check) => ({
          name: typeof check.name === "string" ? check.name : "",
          ok: check.ok === true,
          detail: typeof check.detail === "string" ? check.detail : "",
        }))
        .filter((check) => check.name !== "")
    : [];
}

export function getKubernetesUpgradeEtcdHealth(
  result: Record<string, unknown> | null,
): KubernetesUpgradePreflightCheck[] {
  return toChecks(result?.etcd_health);
}

export function getKubernetesUpgradePreflight(
  result: Record<string, unknown> | null,
): { checks: KubernetesUpgradePreflightCheck[]; blocked: boolean } | null {
  const preflight = result?.preflight;
  if (!preflight || typeof preflight !== "object" || Array.isArray(preflight)) return null;
  const record = preflight as Record<string, unknown>;
  return { checks: toChecks(record.checks), blocked: record.blocked === true };
}

export interface KubernetesRecoveryNode {
  name: string;
  role: string;
  version: string;
  onTarget: boolean;
}

export interface KubernetesRecoveryReport {
  targetVersion: string;
  observedAt: string;
  nodes: KubernetesRecoveryNode[];
  drift: number;
  etcdHealthy: boolean | null;
  etcdStacked: boolean;
  etcdMembers: { pod: string; node: string; healthy: boolean; detail: string }[];
  snapshots: { name: string; bytes: number; taken: string }[];
  warnings: string[];
}

function toRecords(raw: unknown): Record<string, unknown>[] {
  return Array.isArray(raw)
    ? raw.filter((item): item is Record<string, unknown> =>
        Boolean(item) && typeof item === "object" && !Array.isArray(item),
      )
    : [];
}

/** Read-only view of a cluster that may or may not have finished upgrading. */
export function getKubernetesRecoveryReport(
  result: Record<string, unknown> | null,
): KubernetesRecoveryReport | null {
  if (!result || !Array.isArray(result.nodes)) return null;
  const etcd = (result.etcd ?? {}) as Record<string, unknown>;
  const snapshots = (result.snapshots ?? {}) as Record<string, unknown>;
  return {
    targetVersion: typeof result.target_version === "string" ? result.target_version : "",
    observedAt: typeof result.observed_at === "string" ? result.observed_at : "",
    nodes: toRecords(result.nodes).map((node) => ({
      name: typeof node.name === "string" ? node.name : "",
      role: typeof node.role === "string" ? node.role : "",
      version: typeof node.version === "string" ? node.version : "",
      onTarget: node.on_target === true,
    })).filter((node) => node.name !== ""),
    drift: typeof result.drift === "number" ? result.drift : 0,
    etcdHealthy: etcd.healthy === true ? true : etcd.healthy === false ? false : null,
    etcdStacked: etcd.stacked === true,
    etcdMembers: toRecords(etcd.members).map((member) => ({
      pod: typeof member.pod === "string" ? member.pod : "",
      node: typeof member.node === "string" ? member.node : "",
      healthy: member.healthy === true,
      detail: typeof member.detail === "string" ? member.detail : "",
    })),
    snapshots: toRecords(snapshots.entries).map((entry) => ({
      name: typeof entry.name === "string" ? entry.name : "",
      bytes: typeof entry.bytes === "number" ? entry.bytes : 0,
      taken: typeof entry.taken === "string" ? entry.taken : "",
    })).filter((entry) => entry.name !== ""),
    warnings: Array.isArray(result.warnings)
      ? result.warnings.filter((warning): warning is string => typeof warning === "string")
      : [],
  };
}
