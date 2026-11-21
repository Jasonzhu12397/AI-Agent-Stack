export type IronicWorkloadValues = {
  enabled: boolean;
  credentialsName: string;
  version: string;
  ironicImage: string;
  ramdiskDownloaderImage: string;
  keepalivedImage: string;
  interfaceName: string;
  ipAddress: string;
};

export type IronicWorkloadResult = {
  spec: Record<string, unknown> | null;
  missing: string[];
};

const REQUIRED_LABELS: Array<[keyof Omit<IronicWorkloadValues, "enabled">, string]> = [
  ["credentialsName", "API credentials name"],
  ["version", "Ironic version"],
  ["ironicImage", "Ironic image"],
  ["ramdiskDownloaderImage", "Deploy ramdisk downloader image"],
  ["keepalivedImage", "Keepalived image"],
  ["interfaceName", "Provisioning interface"],
  ["ipAddress", "Provisioning IP address"],
];

export function buildIronicWorkload(values: IronicWorkloadValues): IronicWorkloadResult {
  if (!values.enabled) return { spec: null, missing: [] };
  const missing: string[] = [];
  for (const [key, label] of REQUIRED_LABELS) {
    if (!values[key] || !values[key].trim()) missing.push(label);
  }
  if (missing.length) return { spec: null, missing };
  return {
    missing: [],
    spec: {
      api_credentials_name: values.credentialsName.trim(),
      version: values.version.trim(),
      images: {
        ironic: values.ironicImage.trim(),
        deploy_ramdisk_downloader: values.ramdiskDownloaderImage.trim(),
        keepalived: values.keepalivedImage.trim(),
      },
      networking: {
        interface: values.interfaceName.trim(),
        ip_address: values.ipAddress.trim(),
      },
    },
  };
}

export function ironicWorkloadConfigured(spec: unknown): boolean {
  if (!spec || typeof spec !== "object" || Array.isArray(spec)) return false;
  const ironic = (spec as Record<string, unknown>).ironic;
  return Boolean(ironic) && typeof ironic === "object" && !Array.isArray(ironic);
}

export function ironicWorkloadTemplate(values: IronicWorkloadValues): string {
  if (!values.enabled) return "";
  const result = buildIronicWorkload(values);
  if (!result.spec) {
    return "Missing: " + result.missing.join(", ");
  }
  return JSON.stringify({ ironic: result.spec }, null, 2);
}
