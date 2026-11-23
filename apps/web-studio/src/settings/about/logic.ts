export type AboutInfo = {
  version: string;
  build: string;
  licenses: string;
  release_notes_url: string;
};

const UNKNOWN = "unknown";

function scalar(raw: unknown): string {
  if (raw === null || raw === undefined) return UNKNOWN;
  if (typeof raw === "string" && raw.trim() === "") return UNKNOWN;
  return String(raw).trim();
}

/**
 * Normalizes the `/meta/about/` payload. The server payload is authoritative:
 * a missing or blank field falls back to "unknown" here. The frontend must
 * never substitute a hard-coded package version when the server field is absent.
 */
export function normalizeAbout(raw: unknown): AboutInfo {
  if (!raw || typeof raw !== "object") {
    return { version: UNKNOWN, build: UNKNOWN, licenses: UNKNOWN, release_notes_url: "" };
  }
  const value = raw as Record<string, unknown>;
  const releaseNotes =
    typeof value.release_notes_url === "string" && value.release_notes_url.trim() !== ""
      ? value.release_notes_url.trim()
      : "";
  return {
    version: scalar(value.version),
    build: scalar(value.build),
    licenses: scalar(value.licenses),
    release_notes_url: releaseNotes,
  };
}

export function describeError(code: string): string {
  switch (code) {
    case "META_UNAVAILABLE":
      return "Version metadata is unavailable in this deployment.";
    case "HTTP_401":
      return "Your session has expired. Sign in again.";
    case "HTTP_403":
      return "Your role does not allow viewing system information.";
    default:
      return code ? `Request failed (${code}).` : "Request failed.";
  }
}

export const pageLabels = [
  "About",
  "Version",
  "Build",
  "Licenses",
  "Release notes",
  "System information",
  "Version metadata comes from the server; values shown as unknown were not reported by the backend.",
  "About information could not be loaded.",
];
