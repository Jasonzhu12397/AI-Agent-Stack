import test from "node:test";
import assert from "node:assert/strict";
import {
  buildIronicWorkload,
  ironicWorkloadConfigured,
  ironicWorkloadTemplate,
  provisioningNetworkDefaults,
} from "../src/domain/ironicWorkload.ts";

const complete = {
  enabled: true,
  credentialsName: "ironic-auth",
  version: "37.0",
  ironicImage: "registry.local/ironic:37.0",
  ramdiskDownloaderImage: "registry.local/ipa-downloader:latest",
  keepalivedImage: "registry.local/keepalived:2.2.4",
  interfaceName: "eno2",
  ipAddress: "10.130.60.84",
};

test("disabled Ironic workload contributes no cluster spec", () => {
  const result = buildIronicWorkload({ ...complete, enabled: false });
  assert.equal(result.spec, null);
  assert.deepEqual(result.missing, []);
});

test("complete Ironic workload renders the platform contract", () => {
  const result = buildIronicWorkload(complete);
  assert.deepEqual(result.missing, []);
  assert.deepEqual(result.spec, {
    api_credentials_name: "ironic-auth",
    version: "37.0",
    images: {
      ironic: "registry.local/ironic:37.0",
      deploy_ramdisk_downloader: "registry.local/ipa-downloader:latest",
      keepalived: "registry.local/keepalived:2.2.4",
    },
    networking: {
      interface: "eno2",
      ip_address: "10.130.60.84",
    },
  });
});

test("incomplete Ironic workload names every missing field", () => {
  const result = buildIronicWorkload({
    ...complete,
    credentialsName: "  ",
    keepalivedImage: "",
    ipAddress: "",
  });
  assert.equal(result.spec, null);
  assert.deepEqual(result.missing, [
    "API credentials name",
    "Keepalived image",
    "Provisioning IP address",
  ]);
});

test("deployment spec detection distinguishes configured from missing", () => {
  assert.equal(ironicWorkloadConfigured({}), false);
  assert.equal(ironicWorkloadConfigured({ ironic: "registry.local/ironic:37.0" }), false);
  assert.equal(ironicWorkloadConfigured(null), false);
  assert.equal(ironicWorkloadConfigured({ ironic: { version: "37.0" } }), true);
});

test("provisioning network defaults prefer the bond and the declared gateway", () => {
  const profile = {
    networks: [
      { role: "control", interface: "eno1" },
      {
        role: "provisioning",
        enabled: true,
        interface: "eno4",
        gateway_ipv4: "10.40.0.1",
        bond: { name: "bond-prov", interfaces: ["eno4", "eno5"] },
      },
    ],
  };
  assert.deepEqual(provisioningNetworkDefaults(profile), {
    interfaceName: "bond-prov",
    ipAddress: "10.40.0.1",
  });
});

test("disabled or absent provisioning networks yield blank defaults", () => {
  assert.deepEqual(provisioningNetworkDefaults({ networks: [] }), {
    interfaceName: "",
    ipAddress: "",
  });
  assert.deepEqual(
    provisioningNetworkDefaults({
      networks: [{ role: "provisioning", enabled: false, interface: "eno4" }],
    }),
    { interfaceName: "", ipAddress: "" },
  );
});

test("template renders the exact payload or names what is missing", () => {
  assert.equal(ironicWorkloadTemplate({ ...complete, enabled: false }), "");
  assert.equal(
    JSON.parse(ironicWorkloadTemplate(complete)).ironic.networking.interface,
    "eno2",
  );
  assert.equal(
    ironicWorkloadTemplate({ ...complete, keepalivedImage: "" }),
    "Missing: Keepalived image",
  );
});
