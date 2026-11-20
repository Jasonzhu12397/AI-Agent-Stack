<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { buildIronicWorkload, ironicWorkloadConfigured } from "../domain/ironicWorkload.ts";
import {
  approveAgentCommand,
  cancelClusterDeployment,
  createAgentCommand,
  createClusterDeployment,
  ApiError,
  type NetworkReadinessReport,
  enrollAgentNode,
  getAgentCommands,
  getAgentNodes,
  getClusterDeployments,
  getInfraNetworkProfile,
  saveInfraNetworkProfile,
  runClusterDeploymentPostflight,
  getProjects,
  getTenants,
  revokeAgentNode,
  type AgentCommand,
  type AgentNode,
  type ClusterDeployment,
  type InfraNetwork,
  type InfraNetworkProfile,
  type Project,
  type Tenant,
} from "../services/api";

const tenants = ref<Tenant[]>([]);
const projects = ref<Project[]>([]);
const nodes = ref<AgentNode[]>([]);
const commands = ref<AgentCommand[]>([]);
const deployments = ref<ClusterDeployment[]>([]);
const selectedTenantId = ref("");
const selectedProjectId = ref("");
const selectedNodeId = ref("");
const nodeName = ref("eph-node-01");
const nodeType = ref<AgentNode["node_type"]>("eph_node");
const clusterName = ref("");
const buildClusterName = ref("");
const buildKubernetesVersion = ref("");
const buildControlPlaneReplicas = ref(3);
const buildWorkerReplicas = ref(3);
const buildNetworkProfile = ref("");
const infraNetworkProfile = ref<InfraNetworkProfile>({ networks: [] });
const infraNetworkStatus = ref("");
const infraNetworkSaving = ref(false);
const networkDraft = ref({
  name: "",
  role: "control" as InfraNetwork["role"],
  vlan: "",
  mtu: 1500,
  cidr: "",
  gateway: "",
  poolStart: "",
  poolEnd: "",
  prefix: 24,
  interface: "",
  bondName: "",
  bondInterfaces: "",
  pciAddresses: "",
  numVfs: 8,
  hostRoles: ["control_plane", "worker"] as InfraNetwork["host_roles"],
});
const NETWORK_ROLES = [
  { value: "control", label: "Control / management" },
  { value: "data", label: "Data plane" },
  { value: "storage-internal", label: "Ceph internal replication" },
  { value: "storage-external", label: "Ceph external public" },
  { value: "sriov", label: "SR-IOV / DPDK" },
  { value: "provisioning", label: "Metal3 provisioning" },
];
const HOST_ROLES = [
  { value: "control_plane", label: "Control plane" },
  { value: "worker", label: "Worker" },
  { value: "storage", label: "Storage" },
];
const buildIronicEnabled = ref(false);
const buildIronicCredentialsName = ref("");
const buildIronicVersion = ref("");
const buildIronicImage = ref("");
const buildIronicRamdiskDownloaderImage = ref("");
const buildIronicKeepalivedImage = ref("");
const buildIronicInterface = ref("");
const buildIronicIpAddress = ref("");
const buildStorageEnabled = ref(false);
const buildRookVersion = ref("v1.17.4");
const buildCephImage = ref("quay.io/ceph/ceph:v19.2.2");
const buildStorageInternalName = ref("ceph-cluster-net");
const buildStorageInternalInterface = ref("");
const buildStorageInternalCidr = ref("");
const buildStorageExternalName = ref("ceph-public-net");
const buildStorageExternalInterface = ref("");
const buildStorageExternalCidr = ref("");
const buildStorageNodes = ref(
  '[{"name": "worker-0", "devices": ["/dev/nvme0n1"]}]',
);
const commandType = ref("node.health");
const commandPayload = ref("{}");
const enrollmentToken = ref("");
const enrollmentNodeId = ref("");
const deploymentTargetNodeIds = ref<Record<string, string>>({});
const error = ref("");
const networkReadiness = ref<NetworkReadinessReport | null>(null);
const info = ref("");
const loading = ref(false);
const submitting = ref(false);
let refreshTimer: number | undefined;

const availableTargetNodes = computed(() =>
  nodes.value.filter((node) => node.node_type === "target_cluster" && node.status === "online"),
);
const selectedNode = computed(
  () => nodes.value.find((node) => node.id === selectedNodeId.value) ?? null,
);
const availableCommands = computed(() => {
  if (selectedNode.value?.node_type === "target_cluster") {
    return [
      "node.health",
      "cluster.health",
      "cluster.network.inspect",
      "cluster.observe",
      "cluster.diagnose",
      "cluster.postflight",
      "k8s.resource.read",
    ];
  }
  return [
    "node.health",
    "host.preflight",
    "cluster.plan",
    "cluster.network.inspect",
    "capi.cluster.observe",
    "metal3.host.inspect",
    "cluster.provider.discover",
    "cluster.manifest.validate",
  ];
});

function defaultPayload(type: string): string {
  const values: Record<string, Record<string, unknown>> = {
    "node.health": {},
    "host.preflight": {},
    "cluster.health": {},
    "cluster.network.inspect": {},
    "cluster.observe": {},
    "cluster.diagnose": { namespace: "default" },
    "cluster.postflight": {},
    "k8s.resource.read": { resource: "nodes" },
    "capi.cluster.observe": {},
    "metal3.host.inspect": {},
    "cluster.provider.discover": {},
    "cluster.manifest.validate": {
      manifests: [
        {
          apiVersion: "cluster.x-k8s.io/v1beta1",
          kind: "Cluster",
          metadata: { name: "edge-lab", namespace: "default" },
          spec: {},
        },
      ],
    },
    "cluster.plan": {
      cluster_spec: {
        cluster_name: "edge-lab",
        kubernetes_version: "v1.32.2",
        control_plane_replicas: 3,
        worker_replicas: 4,
      },
    },
    "cluster.provision": {
      cluster_spec: {
        cluster_name: "edge-lab",
        kubernetes_version: "v1.32.2",
        control_plane_replicas: 3,
        worker_replicas: 4,
      },
    },
    "target-agent.bootstrap": { target_cluster_name: "edge-lab" },
  };
  return JSON.stringify(values[type] ?? {}, null, 2);
}

async function loadTenants(): Promise<void> {
  loading.value = true;
  try {
    const response = await getTenants();
    tenants.value = response.results;
    if (!selectedTenantId.value && tenants.value.length) {
      selectedTenantId.value = tenants.value[0].id;
    }
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to load tenants.";
  } finally {
    loading.value = false;
  }
}

async function loadProjects(tenantId: string): Promise<void> {
  projects.value = [];
  nodes.value = [];
  commands.value = [];
  selectedProjectId.value = "";
  selectedNodeId.value = "";
  if (!tenantId) return;
  try {
    const response = await getProjects(tenantId);
    projects.value = response.results;
    if (projects.value.length) selectedProjectId.value = projects.value[0].id;
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to load projects.";
  }
}

async function loadNodes(): Promise<void> {
  if (!selectedTenantId.value || !selectedProjectId.value) {
    nodes.value = [];
    commands.value = [];
    deployments.value = [];
    return;
  }
  try {
    const response = await getAgentNodes(selectedTenantId.value, selectedProjectId.value);
    nodes.value = response.results;
    if (!nodes.value.some((node) => node.id === selectedNodeId.value)) {
      selectedNodeId.value = nodes.value[0]?.id ?? "";
    }
    await loadDeployments();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to load node agents.";
  }
}

async function loadInfraNetworks(): Promise<void> {
  if (!selectedTenantId.value || !selectedProjectId.value) return;
  try {
    const response = await getInfraNetworkProfile(
      selectedTenantId.value,
      selectedProjectId.value,
    );
    const profile = response.profile;
    if ("networks" in profile && Array.isArray(profile.networks)) {
      infraNetworkProfile.value = {
        networks: profile.networks,
        dns_servers: profile.dns_servers ?? [],
      };
    } else {
      infraNetworkProfile.value = { networks: [] };
    }
  } catch (cause) {
    infraNetworkStatus.value =
      cause instanceof Error ? cause.message : "Unable to load infrastructure networks.";
  }
}

function addInfraNetwork(): void {
  infraNetworkStatus.value = "";
  const draft = networkDraft.value;
  if (!draft.name.trim()) {
    infraNetworkStatus.value = "Network name is required.";
    return;
  }
  if (!draft.cidr.trim() && draft.role !== "sriov") {
    infraNetworkStatus.value = "A CIDR is required for this network role.";
    return;
  }
  if (!draft.interface.trim() && !draft.bondName.trim()) {
    infraNetworkStatus.value = "Provide a NIC interface or a bond definition.";
    return;
  }
  const network: InfraNetwork = {
    name: draft.name.trim(),
    role: draft.role,
    enabled: true,
    vlan: draft.vlan ? Number(draft.vlan) : null,
    mtu: draft.mtu,
    cidr: draft.cidr.trim() || null,
    gateway_ipv4: draft.gateway.trim() || null,
    host_roles: [...draft.hostRoles],
    interface: draft.interface.trim() || null,
    pci_addresses: draft.pciAddresses
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean),
    num_vfs: draft.numVfs,
    bond: draft.bondName.trim()
      ? {
          name: draft.bondName.trim(),
          interfaces: draft.bondInterfaces
            .split(",")
            .map((item) => item.trim())
            .filter(Boolean),
          mode: "active-backup",
        }
      : null,
  };
  if (draft.poolStart.trim() && draft.poolEnd.trim()) {
    network.ip_pools = [
      {
        start: draft.poolStart.trim(),
        end: draft.poolEnd.trim(),
        prefix: draft.prefix,
      },
    ];
  }
  infraNetworkProfile.value = {
    ...infraNetworkProfile.value,
    networks: [...infraNetworkProfile.value.networks, network],
  };
}

function removeInfraNetwork(index: number): void {
  infraNetworkProfile.value = {
    ...infraNetworkProfile.value,
    networks: infraNetworkProfile.value.networks.filter((_, itemIndex) => itemIndex !== index),
  };
}

async function saveInfraNetworks(): Promise<void> {
  if (!selectedTenantId.value || !selectedProjectId.value) return;
  infraNetworkSaving.value = true;
  infraNetworkStatus.value = "";
  try {
    const response = await saveInfraNetworkProfile(
      selectedTenantId.value,
      selectedProjectId.value,
      infraNetworkProfile.value,
    );
    infraNetworkProfile.value = response.profile;
    infraNetworkStatus.value =
      "Network profile validated. Metal3 networkData and IPPools were generated for control plane, workers and storage.";
  } catch (cause) {
    infraNetworkStatus.value =
      cause instanceof Error ? cause.message : "Unable to save infrastructure networks.";
  } finally {
    infraNetworkSaving.value = false;
  }
}

async function loadDeployments(): Promise<void> {
  if (!selectedTenantId.value || !selectedProjectId.value) {
    deployments.value = [];
    return;
  }
  try {
    const response = await getClusterDeployments(
      selectedTenantId.value,
      selectedProjectId.value,
    );
    deployments.value = response.results;
    for (const deployment of deployments.value) {
      if (!deploymentTargetNodeIds.value[deployment.id]) {
        const matchingNode = availableTargetNodes.value.find(
          (node) => !node.cluster_name || node.cluster_name === deployment.cluster_name,
        );
        if (matchingNode) deploymentTargetNodeIds.value[deployment.id] = matchingNode.id;
      }
    }
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to load cluster plans.";
  }
}

async function loadCommands(): Promise<void> {
  if (!selectedNodeId.value) {
    commands.value = [];
    return;
  }
  try {
    const response = await getAgentCommands(selectedNodeId.value);
    commands.value = response.results;
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to load agent commands.";
  }
}

async function enrollNode(): Promise<void> {
  if (!selectedTenantId.value || !selectedProjectId.value) return;
  submitting.value = true;
  error.value = "";
  info.value = "";
  enrollmentToken.value = "";
  try {
    const enrolled = await enrollAgentNode(selectedTenantId.value, selectedProjectId.value, {
      name: nodeName.value.trim(),
      node_type: nodeType.value,
      cluster_name: clusterName.value.trim(),
    });
    enrollmentToken.value = enrolled.enrollment_token;
    enrollmentNodeId.value = enrolled.id;
    selectedNodeId.value = enrolled.id;
    info.value = "Node enrolled. Copy the token now; it will not be shown again.";
    await loadNodes();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to enroll node.";
  } finally {
    submitting.value = false;
  }
}

async function copyEnrollmentToken(): Promise<void> {
  if (!enrollmentToken.value) return;
  try {
    await navigator.clipboard.writeText(enrollmentToken.value);
    info.value = "Enrollment token copied.";
  } catch {
    error.value = "Clipboard access was denied. Select and copy the token manually.";
  }
}

async function generateClusterPlan(): Promise<void> {
  if (!selectedNode.value || selectedNode.value.node_type !== "eph_node") return;
  if (!buildClusterName.value.trim() || !buildKubernetesVersion.value.trim()) {
    error.value = "Cluster name and Kubernetes version are required.";
    return;
  }
  submitting.value = true;
  error.value = "";
  info.value = "";
  try {
    const clusterSpec: Record<string, unknown> = {
      cluster_name: buildClusterName.value.trim(),
      kubernetes_version: buildKubernetesVersion.value.trim(),
      control_plane_replicas: buildControlPlaneReplicas.value,
      worker_replicas: buildWorkerReplicas.value,
    };
    if (infraNetworkProfile.value.networks.length) {
      clusterSpec.infra_network_profile = {
        networks: infraNetworkProfile.value.networks,
        dns_servers: infraNetworkProfile.value.dns_servers ?? [],
      };
    }
    if (buildStorageEnabled.value) {
      let storageNodes: unknown;
      try {
        storageNodes = JSON.parse(buildStorageNodes.value);
      } catch {
        throw new Error("Storage nodes must be valid JSON.");
      }
      if (!Array.isArray(storageNodes) || storageNodes.length === 0) {
        throw new Error("Storage nodes must be a non-empty JSON array.");
      }
      if (
        !buildStorageInternalInterface.value.trim() ||
        !buildStorageInternalCidr.value.trim() ||
        !buildStorageExternalInterface.value.trim() ||
        !buildStorageExternalCidr.value.trim()
      ) {
        throw new Error("Both storage networks require an interface and CIDR.");
      }
      clusterSpec.storage_addon = {
        enabled: true,
        type: "rook-ceph",
        rook_version: buildRookVersion.value.trim(),
        ceph_image: buildCephImage.value.trim(),
        network: {
          internal: {
            name: buildStorageInternalName.value.trim(),
            interface: buildStorageInternalInterface.value.trim(),
            cidr: buildStorageInternalCidr.value.trim(),
            mtu: 9000,
            ipam: "whereabouts",
          },
          external: {
            name: buildStorageExternalName.value.trim(),
            interface: buildStorageExternalInterface.value.trim(),
            cidr: buildStorageExternalCidr.value.trim(),
            mtu: 9000,
            ipam: "whereabouts",
          },
        },
        storage: { nodes: storageNodes, replication: 3, failure_domain: "host" },
        mon: { count: 3 },
        mgr: { count: 2 },
        storage_classes: { block: true, filesystem: true },
      };
    }
    const ironicWorkload = buildIronicWorkload({
      enabled: buildIronicEnabled.value,
      credentialsName: buildIronicCredentialsName.value,
      version: buildIronicVersion.value,
      ironicImage: buildIronicImage.value,
      ramdiskDownloaderImage: buildIronicRamdiskDownloaderImage.value,
      keepalivedImage: buildIronicKeepalivedImage.value,
      interfaceName: buildIronicInterface.value,
      ipAddress: buildIronicIpAddress.value,
    });
    if (ironicWorkload.missing.length) {
      throw new Error(
        "Ironic workload requires: " + ironicWorkload.missing.join(", ") + ".",
      );
    }
    if (ironicWorkload.spec) {
      clusterSpec.ironic = ironicWorkload.spec;
    }
    if (buildNetworkProfile.value.trim()) {
      const networkProfile: unknown = JSON.parse(buildNetworkProfile.value);
      if (networkProfile === null || Array.isArray(networkProfile) || typeof networkProfile !== "object") {
        throw new Error("Network profile must be a JSON object.");
      }
      clusterSpec.network_profile = networkProfile;
    }
    const deployment = await createClusterDeployment(
      selectedTenantId.value,
      selectedProjectId.value,
      selectedNode.value.id,
      clusterSpec,
    );
    info.value =
      deployment.status === "plan_ready"
        ? "Cluster plan completed. Review the missing prerequisites before provisioning."
        : "Cluster delivery workflow created. EPH preflight and plan commands are queued.";
    await loadInfraNetworks();
    await loadDeployments();
    await loadCommands();
  } catch (cause) {
    if (cause instanceof ApiError) {
      const report = cause.data.network_readiness;
      networkReadiness.value =
        report && typeof report === "object" ? (report as NetworkReadinessReport) : null;
    }
    error.value = cause instanceof Error ? cause.message : "Unable to create cluster plan.";
  } finally {
    submitting.value = false;
  }
}

async function dispatchCommand(): Promise<void> {
  if (!selectedNodeId.value) return;
  submitting.value = true;
  error.value = "";
  info.value = "";
  try {
    const payload: unknown = JSON.parse(commandPayload.value);
    if (payload === null || Array.isArray(payload) || typeof payload !== "object") {
      throw new Error("Command payload must be a JSON object.");
    }
    const created = await createAgentCommand(
      selectedNodeId.value,
      commandType.value,
      payload as Record<string, unknown>,
    );
    info.value =
      created.status === "waiting_approval"
        ? "Command queued for independent administrator approval."
        : "Command dispatched to the node queue.";
    await loadCommands();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to dispatch command.";
  } finally {
    submitting.value = false;
  }
}

async function runTargetPostflight(deployment: ClusterDeployment): Promise<void> {
  if (!window.confirm("Confirm that CAPI / Metal3 / Ironic provisioning has completed and every worker is ready before running target-side checks?")) return;
  const targetNodeId = deploymentTargetNodeIds.value[deployment.id];
  if (!targetNodeId) {
    error.value = "Select an online Target Cluster Agent first.";
    return;
  }
  submitting.value = true;
  error.value = "";
  info.value = "";
  try {
    await runClusterDeploymentPostflight(deployment.id, targetNodeId);
    selectedNodeId.value = targetNodeId;
    info.value = "Read-only target cluster postflight command dispatched.";
    await loadDeployments();
    await loadCommands();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to dispatch target postflight.";
  } finally {
    submitting.value = false;
  }
}

async function cancelDeployment(deployment: ClusterDeployment): Promise<void> {
  if (!window.confirm("Cancel this cluster plan workflow?")) return;
  try {
    await cancelClusterDeployment(deployment.id);
    info.value = "Cluster plan workflow cancelled.";
    await loadDeployments();
    await loadCommands();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to cancel cluster plan.";
  }
}

function missingRequirements(deployment: ClusterDeployment): string[] {
  const value = deployment.plan_result?.missing_requirements;
  return Array.isArray(value) ? value.map(String) : [];
}

async function approveCommand(command: AgentCommand): Promise<void> {
  submitting.value = true;
  error.value = "";
  try {
    await approveAgentCommand(command.id, command.plan_hash);
    info.value = "Command approved. The node agent can now claim it.";
    await loadCommands();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Approval failed.";
  } finally {
    submitting.value = false;
  }
}

async function revokeNode(node: AgentNode): Promise<void> {
  if (!window.confirm("Revoke this node credential and cancel queued commands?")) return;
  try {
    await revokeAgentNode(node.id);
    info.value = "Node credential revoked.";
    await loadNodes();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to revoke node.";
  }
}

function statusType(status: string): "success" | "warning" | "danger" | "info" {
  if (status === "online" || status === "succeeded") return "success";
  if (status === "running" || status === "queued") return "warning";
  if (status === "failed" || status === "revoked") return "danger";
  return "info";
}

function formatTime(value: string | null): string {
  return value ? new Date(value).toLocaleString() : "Never";
}

watch(nodeType, (value) => {
  nodeName.value = value === "eph_node" ? "eph-node-01" : "target-cluster-01";
});
watch(selectedTenantId, (tenantId) => void loadProjects(tenantId));
watch(selectedProjectId, () => {
  void loadNodes();
  void loadInfraNetworks();
});
watch(selectedNodeId, () => {
  commandType.value = availableCommands.value[0] ?? "node.health";
  commandPayload.value = defaultPayload(commandType.value);
  void loadCommands();
});
watch(commandType, (value) => {
  commandPayload.value = defaultPayload(value);
});

onMounted(() => {
  void loadTenants();
  refreshTimer = window.setInterval(() => {
    void loadNodes();
    void loadCommands();
  }, 5000);
});
onUnmounted(() => {
  if (refreshTimer !== undefined) window.clearInterval(refreshTimer);
});
</script>

<template>
  <section class="infra-agents">
    <div class="infra-heading">
      <div>
        <p class="eyebrow">REMOTE INFRASTRUCTURE CONTROL</p>
        <h2>EPH &amp; Target Cluster Agents</h2>
        <p class="infra-description">
          One remote control plane, two independently enrolled execution agents. EPH is a temporary RAM-booted
          bare-metal node running single-node Kubernetes; after bootstrap, that same host is rebooted and reclaimed as a target worker.
        </p>
      </div>
      <el-tag type="success" effect="plain">OUTBOUND CONTROL CHANNEL</el-tag>
    </div>

    <el-alert v-if="error" :title="error" type="error" show-icon :closable="true" @close="error = ''" />
    <el-card v-if="networkReadiness" shadow="never" class="readiness-card">
      <template #header>Pre-install network readiness</template>
      <el-descriptions :column="3" size="small" border>
        <el-descriptions-item label="Control plane">
          {{ networkReadiness.control_plane_ready }} / {{ networkReadiness.required_control_plane }}
        </el-descriptions-item>
        <el-descriptions-item label="Worker">
          {{ networkReadiness.worker_ready }} / {{ networkReadiness.required_worker }}
        </el-descriptions-item>
        <el-descriptions-item label="Observed machines">
          {{ networkReadiness.observed_machines }}
        </el-descriptions-item>
        <el-descriptions-item
          v-if="networkReadiness.required_sriov_workers"
          label="SR-IOV workers"
        >
          {{ networkReadiness.sriov_ready }} / {{ networkReadiness.required_sriov_workers }}
        </el-descriptions-item>
      </el-descriptions>
      <ul v-if="networkReadiness.sriov_findings.length" class="readiness-blocked">
        <li v-for="(finding, index) in networkReadiness.sriov_findings" :key="index">
          {{ finding.name }} / {{ finding.policy }}:
          {{ finding.code }} at PCI {{ finding.pci_address }}
          <span v-if="finding.expected !== undefined">
            (expected {{ finding.expected }}, observed {{ finding.observed }})
          </span>
        </li>
      </ul>
      <ul v-if="networkReadiness.machines_with_findings.length" class="readiness-blocked">
        <li v-for="machine in networkReadiness.machines_with_findings" :key="machine.machine_id">
          <strong>{{ machine.name }}</strong>
          <div v-for="(role, roleName) in machine.roles" :key="roleName" class="readiness-role">
            <span class="readiness-role-name">{{ roleName }}</span>
            <span v-if="role.ok" class="readiness-pass">pass</span>
            <ul v-else>
              <li v-for="(finding, index) in role.findings" :key="index">
                {{ finding.network ?? "unknown network" }} /
                {{ finding.interface ?? "unknown interface" }}:
                {{ finding.code }}
                <span v-if="finding.expected !== null">
                  (expected {{ finding.expected }}, observed {{ finding.observed }})
                </span>
              </li>
            </ul>
          </div>
        </li>
      </ul>
      <p v-else class="readiness-hint">
        No machine currently passes both role checks. Run network collection again.
      </p>
    </el-card>
    <el-alert v-if="info" :title="info" type="success" show-icon :closable="true" @close="info = ''" />

    <div class="infra-scope">
      <label>Organization
        <el-select v-model="selectedTenantId" placeholder="Select organization">
          <el-option v-for="tenant in tenants" :key="tenant.id" :label="tenant.name" :value="tenant.id" />
        </el-select>
      </label>
      <label>Project
        <el-select v-model="selectedProjectId" placeholder="Select project">
          <el-option v-for="project in projects" :key="project.id" :label="project.name" :value="project.id" />
        </el-select>
      </label>
    </div>

    <div class="infra-architecture">
      <article class="infra-role-card eph">
        <span class="role-index">01 · BOOTSTRAP PLANE</span>
        <h3>EPH Node Agent</h3>
        <p>Runs in the EPH node's temporary single-node Kubernetes Pod: host preflight, CAPI / Metal3 discovery and bootstrap coordination.</p>
        <el-tag type="warning" effect="plain">RAM boot · temporary · reclaimed as target worker</el-tag>
      </article>
      <div class="infra-link"><span>Remote command channel</span><b>⇄</b></div>
      <article class="infra-role-card target">
        <span class="role-index">02 · TARGET PLANE</span>
        <h3>Target Cluster Agent</h3>
        <p>Bootstrapped after API readiness; performs Kubernetes health, node readiness, events and scoped read-only inspection.</p>
        <el-tag type="success" effect="plain">Joins after provisioning</el-tag>
      </article>
    </div>

    <div class="infra-columns">
      <section class="infra-panel">
        <div class="infra-panel-heading">
          <div><p class="eyebrow">NODE REGISTRY</p><h3>Enroll an Agent</h3></div>
          <el-tag effect="plain">{{ nodes.length }} registered</el-tag>
        </div>
        <div class="infra-form">
          <label>Agent type
            <el-select v-model="nodeType">
              <el-option label="EPH Agent (temporary RAM-boot node)" value="eph_node" />
              <el-option label="Target Cluster Agent" value="target_cluster" />
            </el-select>
          </label>
          <label>Node name
            <el-input v-model="nodeName" placeholder="eph-node-01" />
          </label>
          <label>Target cluster name (optional)
            <el-input v-model="clusterName" placeholder="edge-lab" />
          </label>
          <el-button type="primary" :loading="submitting" :disabled="!selectedProjectId || !nodeName.trim()" @click="enrollNode">
            Enroll node and issue one-time token
          </el-button>
        </div>

        <div v-if="enrollmentToken" class="enrollment-secret">
          <strong>One-time enrollment token</strong>
          <p>Save this value in a secret manager on the agent host. The API stores only its hash.</p>
          <el-input :model-value="enrollmentToken" readonly />
          <el-button size="small" @click="copyEnrollmentToken">Copy token</el-button>
          <code>AGENT_NODE_ID={{ enrollmentNodeId }}</code>
        </div>

        <div class="infra-node-list">
          <button
            v-for="node in nodes"
            :key="node.id"
            class="infra-node"
            :class="{ selected: selectedNodeId === node.id }"
            @click="selectedNodeId = node.id"
          >
            <span class="node-symbol">{{ node.node_type === "eph_node" ? "E" : "T" }}</span>
            <span class="node-main">
              <strong>{{ node.name }}</strong>
              <small>{{ node.node_type === "eph_node" ? "temporary RAM-boot EPH · later target worker" : "target cluster agent" }} · {{ node.cluster_name || "cluster not assigned" }}</small>
              <small>Last heartbeat: {{ formatTime(node.last_seen_at) }}</small>
            </span>
            <el-tag :type="statusType(node.status)" size="small">{{ node.status }}</el-tag>
          </button>
          <el-empty v-if="!loading && !nodes.length" description="No agents enrolled in this project" :image-size="72" />
        </div>
      </section>

      <section class="infra-panel">
        <div class="infra-panel-heading">
          <div><p class="eyebrow">INFRASTRUCTURE NETWORKS</p><h3>Network profile, cloud-init and Metal3 IP pools</h3></div>
          <el-button type="primary" :loading="infraNetworkSaving" @click="saveInfraNetworks">
            Validate and save profile
          </el-button>
        </div>
        <p class="infra-description">
          Define control, data, storage and SR-IOV segments once. The backend validates VLAN/CIDR/IP-pool isolation and generates
          role-specific cloud-init networkData plus Metal3 IPPools for BMH provisioning. Bonds are passed to the OS by interface
          and to the SR-IOV device plugin by PCI address.
          The EPH control stack installs Metal3 IPAM automatically, and the Target Agent installs the SR-IOV Network Operator before
          applying each SriovNetworkNodePolicy. The host-side check compares this profile with the network data reported by the OS
          (interfaces, bonds, MTU, VLAN and PCI); switch configuration is out of scope.
        </p>
        <el-alert
          v-if="infraNetworkStatus"
          :title="infraNetworkStatus"
          :type="infraNetworkStatus.startsWith('Network profile validated') ? 'success' : 'warning'"
          show-icon
          :closable="false"
        />
        <div class="infra-form">
          <div class="replica-fields">
            <label>Network name
              <el-input v-model="networkDraft.name" placeholder="ccd-data" />
            </label>
            <label>Network role
              <el-select v-model="networkDraft.role">
                <el-option v-for="role in NETWORK_ROLES" :key="role.value" :label="role.label" :value="role.value" />
              </el-select>
            </label>
            <label>VLAN
              <el-input v-model="networkDraft.vlan" placeholder="optional, 1-4094" />
            </label>
            <label>MTU
              <el-input-number v-model="networkDraft.mtu" :min="576" :max="9216" />
            </label>
          </div>
          <div class="replica-fields">
            <label>IPv4 CIDR
              <el-input v-model="networkDraft.cidr" placeholder="10.0.61.0/24" />
            </label>
            <label>Gateway
              <el-input v-model="networkDraft.gateway" placeholder="10.0.61.1" />
            </label>
            <label>IP pool start
              <el-input v-model="networkDraft.poolStart" placeholder="10.0.61.10" />
            </label>
            <label>IP pool end
              <el-input v-model="networkDraft.poolEnd" placeholder="10.0.61.250" />
            </label>
            <label>Prefix
              <el-input-number v-model="networkDraft.prefix" :min="8" :max="32" />
            </label>
          </div>
          <div class="replica-fields">
            <label>NIC interface (when not bonded)
              <el-input v-model="networkDraft.interface" placeholder="eno5" />
            </label>
            <label>Bond name
              <el-input v-model="networkDraft.bondName" placeholder="bond_data" />
            </label>
            <label>Bond member NICs (comma separated)
              <el-input v-model="networkDraft.bondInterfaces" placeholder="ens1f0,ens1f1" />
            </label>
            <label>SR-IOV PCI addresses (comma separated)
              <el-input v-model="networkDraft.pciAddresses" placeholder="0000:af:00.1,0000:af:00.2" />
            </label>
            <label>SR-IOV VFs per PF
              <el-input-number v-model="networkDraft.numVfs" :min="1" :max="128" />
            </label>
            <label>Host roles
              <el-select v-model="networkDraft.hostRoles" multiple collapse-tags>
                <el-option v-for="role in HOST_ROLES" :key="role.value" :label="role.label" :value="role.value" />
              </el-select>
            </label>
          </div>
          <el-button plain @click="addInfraNetwork">Add network to profile</el-button>
        </div>
        <el-table :data="infraNetworkProfile.networks" size="small" class="task-table">
          <el-table-column prop="name" label="Network" min-width="140" />
          <el-table-column prop="role" label="Role" min-width="150" />
          <el-table-column label="VLAN" width="80">
            <template #default="{ row }">{{ row.vlan ?? "untagged" }}</template>
          </el-table-column>
          <el-table-column prop="cidr" label="CIDR" min-width="140" />
          <el-table-column label="Link" min-width="150">
            <template #default="{ row }">{{ row.bond?.name || row.interface || "-" }}</template>
          </el-table-column>
          <el-table-column label="Host roles" min-width="180">
            <template #default="{ row }">{{ row.host_roles?.join(", ") }}</template>
          </el-table-column>
          <el-table-column label="" width="90">
            <template #default="{ $index }">
              <el-button size="small" text type="danger" @click="removeInfraNetwork($index)">Remove</el-button>
            </template>
          </el-table-column>
        </el-table>
        <el-empty
          v-if="!infraNetworkProfile.networks.length"
          description="No infrastructure network profile saved for this project"
          :image-size="72"
        />
      </section>

      <section class="infra-panel">
        <div class="infra-panel-heading">
          <div><p class="eyebrow">COMMAND DISPATCH</p><h3>Remote Agent Console</h3></div>
          <el-tag v-if="selectedNode" :type="statusType(selectedNode.status)">{{ selectedNode.status }}</el-tag>
        </div>
        <el-empty v-if="!selectedNode" description="Enroll or select an agent to dispatch a command" :image-size="82" />
        <template v-else>
          <div class="selected-node-summary">
            <strong>{{ selectedNode.name }}</strong>
            <span>{{ selectedNode.node_type }} · {{ selectedNode.agent_version || "version not reported" }}</span>
          </div>
          <div v-if="selectedNode.node_type === 'eph_node'" class="infra-form cluster-build-form">
            <div>
              <strong>Metal3 / CAPI cluster plan</strong>
              <p>Plan target cluster delivery from the temporary RAM-booted EPH node. The EPH host is expected to reboot and join the final cluster as a worker. This stage is dry-run only.</p>
            </div>
            <label>Target cluster name
              <el-input v-model="buildClusterName" placeholder="edge-lab" />
            </label>
            <label>Kubernetes version
              <el-input v-model="buildKubernetesVersion" placeholder="v1.x.y" />
            </label>
            <el-divider content-position="left">Ironic workload CR</el-divider>
            <p>
              The Ironic Standalone Operator is installed offline, but its workload CR is
              environment specific. Without it the EPH control stack cannot become ready;
              the EPH preflight reports the missing contract as a warning.
            </p>
            <el-switch v-model="buildIronicEnabled" active-text="Configure the Ironic workload CR" />
            <template v-if="buildIronicEnabled">
              <div class="replica-fields">
                <label>API credentials Secret name
                  <el-input v-model="buildIronicCredentialsName" placeholder="ironic-auth" />
                </label>
                <label>Ironic version
                  <el-input v-model="buildIronicVersion" placeholder="37.0" />
                </label>
              </div>
              <label>Ironic image
                <el-input v-model="buildIronicImage" placeholder="registry.local/ironic:37.0" />
              </label>
              <label>Deploy ramdisk downloader image
                <el-input v-model="buildIronicRamdiskDownloaderImage" placeholder="registry.local/ipa-downloader:latest" />
              </label>
              <label>Keepalived image
                <el-input v-model="buildIronicKeepalivedImage" placeholder="registry.local/keepalived:2.2.4" />
              </label>
              <div class="replica-fields">
                <label>Provisioning interface
                  <el-input v-model="buildIronicInterface" placeholder="eno2" />
                </label>
                <label>Provisioning IP address
                  <el-input v-model="buildIronicIpAddress" placeholder="10.130.60.84" />
                </label>
              </div>
            </template>
            <label>Network profile override (optional JSON)
              <el-input
                v-model="buildNetworkProfile"
                type="textarea"
                :rows="6"
                spellcheck="false"
                placeholder="Leave empty to use the saved project infrastructure network profile."
              />
            </label>
            <el-divider content-position="left">Rook-Ceph storage addon</el-divider>
            <p>
              Control traffic stays on the control network. CNF/application data uses the data or
              SR-IOV network. Ceph replication uses the internal storage network and Ceph public
              traffic uses the external storage network; all four CIDRs must not overlap.
            </p>
            <el-switch v-model="buildStorageEnabled" active-text="Deploy Rook-Ceph" />
            <template v-if="buildStorageEnabled">
              <div class="replica-fields">
                <label>Rook version
                  <el-input v-model="buildRookVersion" placeholder="v1.17.4" />
                </label>
                <label>Ceph image
                  <el-input v-model="buildCephImage" placeholder="quay.io/ceph/ceph:v19.2.2" />
                </label>
              </div>
              <div class="replica-fields">
                <label>Internal network name
                  <el-input v-model="buildStorageInternalName" />
                </label>
                <label>Internal interface
                  <el-input v-model="buildStorageInternalInterface" placeholder="eno5" />
                </label>
                <label>Internal CIDR
                  <el-input v-model="buildStorageInternalCidr" placeholder="192.168.60.0/24" />
                </label>
              </div>
              <div class="replica-fields">
                <label>External network name
                  <el-input v-model="buildStorageExternalName" />
                </label>
                <label>External interface
                  <el-input v-model="buildStorageExternalInterface" placeholder="eno6" />
                </label>
                <label>External CIDR
                  <el-input v-model="buildStorageExternalCidr" placeholder="192.168.61.0/24" />
                </label>
              </div>
              <label>Storage nodes JSON
                <el-input
                  v-model="buildStorageNodes"
                  type="textarea"
                  :rows="4"
                  spellcheck="false"
                  placeholder='[{"name": "worker-0", "devices": ["/dev/nvme0n1"]}]'
                />
              </label>
            </template>
            <div class="replica-fields">
              <label>Control plane replicas
                <el-input-number v-model="buildControlPlaneReplicas" :min="1" :max="9" />
              </label>
              <label>Worker replicas
                <el-input-number v-model="buildWorkerReplicas" :min="0" :max="1000" />
              </label>
            </div>
            <el-button
              type="primary"
              plain
              :loading="submitting"
              :disabled="!buildClusterName.trim() || !buildKubernetesVersion.trim()"
              @click="generateClusterPlan"
            >
              Generate cluster plan
            </el-button>
          </div>
          <div class="infra-form">
            <label>Typed command
              <el-select v-model="commandType">
                <el-option v-for="item in availableCommands" :key="item" :label="item" :value="item" />
              </el-select>
            </label>
            <label>JSON payload
              <el-input v-model="commandPayload" type="textarea" :rows="8" spellcheck="false" />
            </label>
            <el-button type="primary" :loading="submitting" :disabled="selectedNode.status === 'revoked'" @click="dispatchCommand">
              Dispatch command
            </el-button>
          </div>
          <div class="deployment-list">
            <div class="infra-panel-heading">
              <h4>Cluster delivery plans</h4>
              <span>{{ deployments.length }} recent</span>
            </div>
            <article v-for="deployment in deployments" :key="deployment.id" class="deployment-card">
              <div class="command-topline">
                <strong>{{ deployment.cluster_name }} · {{ deployment.kubernetes_version }}</strong>
                <el-tag :type="statusType(deployment.status)" size="small">{{ deployment.status }}</el-tag>
              </div>
              <small>{{ formatTime(deployment.created_at) }} · EPH {{ deployment.eph_node_id.slice(0, 8) }}</small>
              <div v-if="deployment.plan_result" class="plan-result">
                <span>Plan hash: {{ deployment.plan_hash || "not generated" }}</span>
                <p v-if="deployment.plan_result.network_profile">Dual-network profile passed input validation. Runtime dataplane connectivity remains a separate check.</p>
                <el-tag
                  :type="ironicWorkloadConfigured(deployment.spec) ? 'success' : 'warning'"
                  size="small"
                >
                  {{ ironicWorkloadConfigured(deployment.spec) ? "Ironic CR configured" : "Ironic CR missing" }}
                </el-tag>
                <el-tag
                  :type="deployment.plan_result.adapter_enabled === true ? 'success' : 'warning'"
                  size="small"
                >
                  {{ deployment.plan_result.adapter_enabled === true ? "Adapter enabled" : "Apply adapter disabled" }}
                </el-tag>
                <ul v-if="missingRequirements(deployment).length">
                  <li v-for="item in missingRequirements(deployment)" :key="item">{{ item }}</li>
                </ul>
                <p v-else>Input prerequisites are complete. Compatibility and real provisioning are still separate gates.</p>
              </div>
              <div
                v-if="deployment.status === 'workers_ready' && deployment.plan_result?.adapter_enabled === true && deployment.plan_result?.provisioning_completed === true"
                class="target-postflight-row"
              >
                <el-select
                  v-model="deploymentTargetNodeIds[deployment.id]"
                  size="small"
                  placeholder="Target Cluster Agent"
                >
                  <el-option
                    v-for="node in availableTargetNodes.filter((item) => !item.cluster_name || item.cluster_name === deployment.cluster_name)"
                    :key="node.id"
                    :label="node.name + ' · ' + (node.cluster_name || 'unassigned')"
                    :value="node.id"
                  />
                </el-select>
                <el-button
                  size="small"
                  type="success"
                  plain
                  :disabled="!deploymentTargetNodeIds[deployment.id]"
                  :loading="submitting"
                  @click="runTargetPostflight(deployment)"
                >Run target postflight</el-button>
              </div>
              <el-button
                v-if="['preflight_queued', 'planning', 'plan_ready'].includes(deployment.status)"
                size="small"
                text
                type="danger"
                @click="cancelDeployment(deployment)"
              >
                Cancel plan
              </el-button>
            </article>
            <el-empty v-if="!deployments.length" description="No cluster plan workflows created" :image-size="56" />
          </div>
          <div class="command-list">
            <div class="infra-panel-heading"><h4>Command history</h4><span>{{ commands.length }} recent</span></div>
            <article v-for="command in commands" :key="command.id" class="command-card">
              <div class="command-topline">
                <strong>{{ command.command_type }}</strong>
                <el-tag :type="statusType(command.status)" size="small">{{ command.status }}</el-tag>
              </div>
              <small>{{ formatTime(command.created_at) }} · attempts {{ command.attempts }}</small>
              <p v-if="command.error" class="command-error">{{ command.error.code || "Execution failed" }}</p>
              <pre v-if="command.result">{{ JSON.stringify(command.result, null, 2) }}</pre>
              <div v-if="command.status === 'waiting_approval'" class="approval-row">
                <code>{{ command.plan_hash }}</code>
                <el-button size="small" type="warning" :loading="submitting" @click="approveCommand(command)">
                  Approve plan hash
                </el-button>
              </div>
            </article>
            <el-empty v-if="!commands.length" description="No commands dispatched yet" :image-size="60" />
          </div>
        </template>
      </section>
    </div>

    <div class="infra-security-note">
      <strong>Execution safety boundary</strong>
      <p>
        This release supports enrollment, heartbeat, durable command polling, lease-protected results,
        approval-hash verification, host preflight, cluster-plan dry run and read-only Kubernetes API
        inspection. Cluster creation, raw manifest application, arbitrary shell, BMC power operations
        and target-agent bootstrap are not enabled until the CAPI / Metal3 / Ironic compatibility matrix
        and execution adapters are verified.
      </p>
    </div>
  </section>
</template>
