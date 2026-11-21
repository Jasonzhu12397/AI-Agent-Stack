/**
 * Verdicts for the runtime snapshots stored on a deployment.
 *
 * Both badges used to be driven by one boolean (`available` for the BMH
 * inventory, `eph_ready` for the preflight), so a run that reached the agent
 * but failed still rendered as green. The status, the error code and the
 * reason are all part of the verdict.
 */

export type BmhInventorySnapshot = {
  available: boolean;
  reason?: string | null;
  status?: string | null;
  error_code?: string | null;
  host_count?: number;
  matched?: number;
  unmatched?: string[];
};

export type BmhInventoryVerdict = {
  tone: "success" | "warning" | "info";
  label: string;
  detail: string;
};

export function bmhInventoryVerdict(
  snapshot: BmhInventorySnapshot | null | undefined,
): BmhInventoryVerdict {
  if (!snapshot || !snapshot.available) {
    return { tone: "info", label: "Inspection not run", detail: "" };
  }
  if (snapshot.status !== "succeeded") {
    const code = snapshot.error_code || snapshot.reason || snapshot.status || "unknown";
    return {
      tone: "warning",
      label: "Inspection failed",
      detail: String(code),
    };
  }
  const hostCount = Number(snapshot.host_count ?? 0);
  const matched = Number(snapshot.matched ?? 0);
  const unmatched = (snapshot.unmatched ?? []).length;
  const label = `${matched} / ${hostCount} host(s) matched`;
  if (hostCount === 0 || matched === 0 || unmatched > 0) {
    const detail =
      hostCount === 0
        ? "the inspection returned no baremetal host"
        : `${unmatched} baremetal host(s) had no matching machine`;
    return { tone: "warning", label, detail };
  }
  return { tone: "success", label, detail: "" };
}

export type EphPreflightSnapshot = {
  available: boolean;
  reason?: string | null;
  status?: string | null;
  error_code?: string | null;
  eph_ready?: boolean | null;
};

export type EphPreflightVerdict = {
  tone: "success" | "warning" | "info";
  label: string;
  detail: string;
};

export function ephPreflightVerdict(
  snapshot: EphPreflightSnapshot | null | undefined,
): EphPreflightVerdict {
  if (!snapshot || !snapshot.available) {
    return { tone: "info", label: "EPH preflight unavailable", detail: "" };
  }
  if (snapshot.status !== "succeeded") {
    return {
      tone: "warning",
      label: "EPH preflight failed",
      detail: String(
        snapshot.error_code || snapshot.reason || snapshot.status || "unknown",
      ),
    };
  }
  if (snapshot.eph_ready === true) {
    return { tone: "success", label: "EPH ready", detail: "" };
  }
  return { tone: "warning", label: "EPH not ready", detail: "" };
}
