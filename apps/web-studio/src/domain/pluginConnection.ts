export type PluginConnectionAuthType = "none" | "credential_ref" | "oauth_ref";

const CREDENTIAL_REFERENCE_PATTERN =
  /^[a-z][a-z0-9+.-]*:\/\/[A-Za-z0-9._/-]{1,180}$/;

export function validatePluginConnectionInput(
  name: string,
  authType: PluginConnectionAuthType,
  credentialRef: string,
): string | null {
  const normalizedName = name.trim();
  const normalizedReference = credentialRef.trim();

  if (!normalizedName || normalizedName.length > 120) {
    return "Connection name is required and must not exceed 120 characters.";
  }
  if (authType !== "none" && !CREDENTIAL_REFERENCE_PATTERN.test(normalizedReference)) {
    return "A valid non-secret URI such as vault://path/to/credential is required.";
  }
  if (authType === "none" && normalizedReference) {
    return "A credential reference is not allowed when authentication is set to none.";
  }
  return null;
}
