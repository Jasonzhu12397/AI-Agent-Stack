export function getKubernetesUpgradeFailure(error: Record<string, unknown> | null): string {
  if (!error) return "";
  const message = typeof error.message === "string" ? error.message.trim() : "";
  const code = typeof error.code === "string" ? error.code.trim() : "";
  if (message && code) return message + " (" + code + ")";
  return message || code;
}
