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

export function getKubernetesUpgradePreflight(
  result: Record<string, unknown> | null,
): { checks: KubernetesUpgradePreflightCheck[]; blocked: boolean } | null {
  const preflight = result?.preflight;
  if (!preflight || typeof preflight !== "object" || Array.isArray(preflight)) return null;
  const record = preflight as Record<string, unknown>;
  const rawChecks = record.checks;
  const checks: KubernetesUpgradePreflightCheck[] = Array.isArray(rawChecks)
    ? rawChecks
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
  return { checks, blocked: record.blocked === true };
}
