<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import {
  createAgentCommand,
  getAgentNodes,
  getFleetMachines,
  collectFleetNetwork,
  pxeBootFleetMachines,
  runFleetDiscovery,
  type FleetDiscoveryResult,
  ingestFleetBmhInventory,
  type BmhInventoryIngest,
  type PxeBootResult,
  getMachineFirmware,
  getFirmwareUpdateJob,
  submitFirmwareUpgrade,
  verifyFleetMachineNetwork,
  getProjectAgentCommands,
  getClusterDeployments,
  getClusterDeploymentEtcdBackup,
  type EtcdBackupState,
  getProjects,
  getTenants,
  getLabWorkflow,
  configureFleetMachineBmc,
  reviewFleetMachineLifecycle,
  type LabWorkflowReport,
  updateClusterSriovDevicePlugin,
  SUPPORTED_KUBERNETES_VERSIONS,
  type AgentCommand,
  type AgentNode,
  type FleetMachine,
  type FirmwareUpdateJob,
  type HostNetworkVerification,
  type MachineFirmware,
  type ClusterDeployment,
  type Project,
  type Tenant,
} from "../services/api";
import InfrastructureAgentsView from "./InfrastructureAgentsView.vue";
import SriovDevicePluginEditor from "./SriovDevicePluginEditor.vue";
import {
  getKubernetesRecoveryReport,
  getKubernetesUpgradeEtcdHealth,
  getKubernetesUpgradeFailure,
  getKubernetesUpgradePreflight,
} from "../domain/kubernetesUpgrade";

type FleetTab = "dashboard" | "fleet" | "eph" | "clusters" | "terminal" | "operations";

const tabs: { id: FleetTab; label: string }[] = [
  { id: "dashboard", label: "Overview" },
  { id: "fleet", label: "Fleet Inventory" },
  { id: "eph", label: "EPH Bootstrap" },
  { id: "clusters", label: "Target Clusters" },
  { id: "terminal", label: "Execution Terminal" },
  { id: "operations", label: "Operations Console" },
];

const activeTab = ref<FleetTab>("dashboard");
const tenants = ref<Tenant[]>([]);
const projects = ref<Project[]>([]);
const nodes = ref<AgentNode[]>([]);
const inventory = ref<FleetMachine[]>([]);
const inventoryCount = ref(0);
const inventorySearch = ref("");
const inventoryStatus = ref("");
const inventoryNodeType = ref("");
const collectBusy = ref(false);
const collectMessage = ref("");
const pxeBootBusy = ref(false);
const pxeBootMessage = ref("");
const pxeBootResults = ref<PxeBootResult | null>(null);
const bmhDialogVisible = ref(false);
const bmhDocumentText = ref("");
const bmhIngestBusy = ref(false);
const bmhIngestMessage = ref("");
const bmhIngestResult = ref<BmhInventoryIngest | null>(null);
const deployments = ref<ClusterDeployment[]>([]);
const commands = ref<AgentCommand[]>([]);
const commandCount = ref(0);
const commandOffset = ref(0);
const commandStatus = ref("");
const commandPageSize = 100;
const supportedKubernetesVersions = SUPPORTED_KUBERNETES_VERSIONS;
const selectedKubernetesVersions = ref<Record<string, string>>({});
// Simulation only describes the plan; execution really drains/applies/uncordons.
// Execution stays the deliberate choice: the default is the safe one, and it
// still needs approval before the Agent runs anything.
const selectedUpgradeModes = ref<Record<string, "simulation" | "execute">>({});
const upgradeModes = ["simulation", "execute"] as const;
// Backups run on the platform scheduler; this panel only reads what the
// Target Agent reported, so an operator can see the newest snapshot and its age.
const backupStates = ref<Record<string, EtcdBackupState>>({});
const backupBusy = ref<string | null>(null);
const backupMessages = ref<Record<string, string>>({});
const readinessBusy = ref<string | null>(null);
const readinessMessages = ref<Record<string, string>>({});
const recoveryBusy = ref<string | null>(null);
const recoveryMessages = ref<Record<string, string>>({});
const rollbackSnapshots = ref<Record<string, string>>({});
const rollbackBusy = ref<string | null>(null);
const rollbackMessages = ref<Record<string, string>>({});
const versionApplyBusy = ref<string | null>(null);
const versionApplyMessages = ref<Record<string, string>>({});
const networkDialogVisible = ref(false);
const networkMachine = ref<FleetMachine | null>(null);
const networkHostRole = ref<"control_plane" | "worker" | "storage">("worker");
const networkVerification = ref<HostNetworkVerification | null>(null);
const networkLoading = ref(false);
const networkObserveMessage = ref("");
const networkError = ref("");
const firmwareDialogVisible = ref(false);
const firmwareMachine = ref<FleetMachine | null>(null);
const firmwareState = ref<MachineFirmware | null>(null);
const firmwareLoading = ref(false);
const firmwareError = ref("");
const firmwareImageUrl = ref("");
const firmwareComponent = ref("");
const firmwareApplyTime = ref("Immediate");
const firmwareBusy = ref(false);
const firmwareMessage = ref("");
const firmwareJobs = ref<FirmwareUpdateJob[]>([]);
const selectedTenantId = ref("");
const selectedProjectId = ref("");
const loading = ref(false);
const refreshing = ref(false);
const error = ref("");
const selectedSriovDeploymentId = ref("");
const sriovSaving = ref(false);
const sriovSaveMessage = ref("");
const sriovEditor = ref<{ validate: () => boolean } | null>(null);
const sriovPools = ref<Record<string, any>>({
  pool4: {
    node_selectors: { type: "high-throughput" },
    tolerations: [{ effect: "NoSchedule", key: "high-throughput", operator: "Equal", value: "true" }],
    sriov_network_device_plugin_configmap_name: "sriov-dp-configmap",
    sriov_network_device_plugin_configmap_data: { resourceList: [] },
  },
});
let refreshTimer: number | undefined;
let refreshGeneration = 0;

const ephNodes = computed(() => nodes.value.filter((node) => node.node_type === "eph_node"));
const targetAgents = computed(() => nodes.value.filter((node) => node.node_type === "target_cluster"));
const onlineNodes = computed(() => nodes.value.filter((node) => node.status === "online"));
const failedDeployments = computed(() => deployments.value.filter((item) => item.status === "failed"));
const activeDeployments = computed(() =>
  deployments.value.filter((item) => !["succeeded", "failed", "cancelled"].includes(item.status)),
);
const recentCommands = computed(() =>
  [...commands.value].sort((left, right) => right.created_at.localeCompare(left.created_at)),
);
const kubernetesUpgradeCommands = computed(() =>
  recentCommands.value.filter((command) => command.command_type === "cluster.kubernetes.version.upgrade"),
);
const kubernetesRollbackCommands = computed(() =>
  recentCommands.value.filter((command) => command.command_type === "cluster.kubernetes.version.rollback"),
);
const latestActivity = computed(() => {
  const commandTimes = commands.value.map((item) => item.finished_at ?? item.started_at ?? item.created_at);
  const deploymentTimes = deployments.value.map((item) => item.updated_at);
  const ordered = [...commandTimes, ...deploymentTimes].sort();
  return ordered.length ? ordered[ordered.length - 1] : null;
});

async function loadTenants(): Promise<void> {
  loading.value = true;
  error.value = "";
  try {
    const response = await getTenants();
    tenants.value = response.results;
    if (!tenants.value.some((tenant) => tenant.id === selectedTenantId.value)) {
      selectedTenantId.value = tenants.value[0]?.id ?? "";
    }
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to load tenant scope.";
  } finally {
    loading.value = false;
  }
}

async function loadProjects(tenantId: string): Promise<void> {
  projects.value = [];
  nodes.value = [];
  deployments.value = [];
  commands.value = [];
  commandCount.value = 0;
  commandOffset.value = 0;
  selectedProjectId.value = "";
  if (!tenantId) return;
  try {
    const response = await getProjects(tenantId);
    projects.value = response.results;
    selectedProjectId.value = projects.value[0]?.id ?? "";
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to load project scope.";
  }
}

const bmcDialogVisible = ref(false);
const bmcDialogMachine = ref<FleetMachine | null>(null);
const bmcEndpoint = ref("");
const bmcCredentialRef = ref("");
const bmcTrustBundleRef = ref("");
const bmcBusy = ref(false);
const bmcMessage = ref("");
const lifecycleBusy = ref<string | null>(null);
const lifecycleMessage = ref("");

function openBmcDialog(machine: FleetMachine): void {
  bmcDialogMachine.value = machine;
  bmcEndpoint.value = "";
  bmcCredentialRef.value = "";
  bmcTrustBundleRef.value = "";
  bmcMessage.value = "";
  bmcDialogVisible.value = true;
}

async function saveBmcConfig(): Promise<void> {
  const machine = bmcDialogMachine.value;
  if (!machine || !machine.machine_id || !selectedTenantId.value || !selectedProjectId.value) return;
  const endpoint = bmcEndpoint.value.trim();
  const credentialRef = bmcCredentialRef.value.trim();
  if (!endpoint || !credentialRef) {
    bmcMessage.value = "BMC endpoint and credential reference are both required.";
    return;
  }
  bmcBusy.value = true;
  bmcMessage.value = "";
  try {
    await configureFleetMachineBmc(
      selectedTenantId.value,
      selectedProjectId.value,
      machine.machine_id,
      {
        endpoint,
        credential_ref: credentialRef,
        ...(bmcTrustBundleRef.value.trim() ? { trust_bundle_ref: bmcTrustBundleRef.value.trim() } : {}),
      },
    );
    bmcMessage.value = "BMC configuration saved.";
    bmcDialogVisible.value = false;
    await refreshData();
  } catch (cause) {
    bmcMessage.value =
      cause instanceof Error ? cause.message : "Unable to save the BMC configuration.";
  } finally {
    bmcBusy.value = false;
  }
}

async function moveMachineLifecycle(machine: FleetMachine, target: string): Promise<void> {
  if (!machine.machine_id || !selectedTenantId.value || !selectedProjectId.value) return;
  const reason = window.prompt(
    "Why is this machine moving to " + target + "? (10-500 characters)",
  );
  if (reason === null) return;
  if (reason.trim().length < 10) {
    lifecycleMessage.value = "A review reason of at least 10 characters is required.";
    return;
  }
  lifecycleBusy.value = machine.machine_id;
  lifecycleMessage.value = "";
  try {
    await reviewFleetMachineLifecycle(
      selectedTenantId.value,
      selectedProjectId.value,
      machine.machine_id,
      {
        expected_state: machine.lifecycle_state || "discovered",
        target_state: target,
        reason: reason.trim(),
      },
    );
    lifecycleMessage.value = "Lifecycle moved to " + target + ".";
    await refreshData();
  } catch (cause) {
    lifecycleMessage.value =
      cause instanceof Error ? cause.message : "Unable to review the machine lifecycle.";
  } finally {
    lifecycleBusy.value = null;
  }
}

const discoveryBusy = ref(false);
const discoveryMessage = ref("");
const discoveryResult = ref<FleetDiscoveryResult | null>(null);

async function runDiscovery(): Promise<void> {
  if (!selectedTenantId.value || !selectedProjectId.value) return;
  if (
    !window.confirm(
      "Boot every configured machine into the inspection image once, then queue metal3.host.inspect on the online EPH nodes?",
    )
  ) {
    return;
  }
  discoveryBusy.value = true;
  discoveryMessage.value = "";
  discoveryResult.value = null;
  try {
    const result = await runFleetDiscovery(
      selectedTenantId.value,
      selectedProjectId.value,
    );
    discoveryResult.value = result;
    const inspectText = result.inspect.queued.length
      ? `inspection queued on ${result.inspect.queued.length} EPH node(s)`
      : `inspection not queued (${result.inspect.reason || "no online EPH"})`;
    discoveryMessage.value = `Discovery: ${result.executed}/${result.machine_count} machine(s) booted; ${inspectText}.`;
  } catch (cause) {
    discoveryMessage.value =
      cause instanceof Error ? cause.message : "Unable to run the discovery chain.";
  } finally {
    discoveryBusy.value = false;
  }
}

const labWorkflow = ref<LabWorkflowReport | null>(null);
const labWorkflowError = ref("");

async function loadLabWorkflow(): Promise<void> {
  try {
    labWorkflow.value = await getLabWorkflow();
    labWorkflowError.value = "";
  } catch (cause) {
    labWorkflow.value = null;
    labWorkflowError.value =
      cause instanceof Error ? cause.message : "Unable to probe the lab workflow.";
  }
}

async function refreshData(): Promise<void> {
  const generation = ++refreshGeneration;
  if (!selectedTenantId.value || !selectedProjectId.value) {
    nodes.value = [];
    inventory.value = [];
    inventoryCount.value = 0;
    deployments.value = [];
    commands.value = [];
    commandCount.value = 0;
    refreshing.value = false;
    return;
  }
  refreshing.value = true;
  error.value = "";
  try {
    const [nodePage, deploymentPage, inventoryPage] = await Promise.all([
      getAgentNodes(selectedTenantId.value, selectedProjectId.value),
      getClusterDeployments(selectedTenantId.value, selectedProjectId.value),
      getFleetMachines(selectedTenantId.value, selectedProjectId.value, {
        limit: 200,
        search: inventorySearch.value,
        status: inventoryStatus.value,
        nodeType: inventoryNodeType.value,
      }),
    ]);
    if (generation !== refreshGeneration) return;
    const commandPage = await getProjectAgentCommands(
      selectedTenantId.value,
      selectedProjectId.value,
      {
        limit: commandPageSize,
        offset: commandOffset.value,
        status: commandStatus.value,
      },
    );
    if (generation !== refreshGeneration) return;
    nodes.value = nodePage.results;
    inventory.value = inventoryPage.results;
    inventoryCount.value = inventoryPage.count;
    deployments.value = deploymentPage.results;
    commands.value = commandPage.results;
    commandCount.value = commandPage.count;
  } catch (cause) {
    if (generation !== refreshGeneration) return;
    error.value = cause instanceof Error ? cause.message : "Unable to refresh Metal3 fleet data.";
  } finally {
    if (generation === refreshGeneration) refreshing.value = false;
  }
}

function setCommandStatus(value: string): void {
  commandStatus.value = value;
  commandOffset.value = 0;
  void refreshData();
}

function changeCommandPage(direction: -1 | 1): void {
  const nextOffset = commandOffset.value + direction * commandPageSize;
  if (nextOffset < 0 || nextOffset >= commandCount.value) return;
  commandOffset.value = nextOffset;
  void refreshData();
}

const sriovPciAddresses = computed(() => {
  const addresses = new Set<string>();
  const visit = (value: unknown): void => {
    if (typeof value === "string") {
      const matches = value.match(/(?:[0-9a-fA-F]{4}:)?[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]/g) ?? [];
      for (const match of matches) addresses.add(match.length === 7 ? "0000:" + match : match.toLowerCase());
    } else if (Array.isArray(value)) {
      value.forEach(visit);
    } else if (value && typeof value === "object") {
      Object.values(value as Record<string, unknown>).forEach(visit);
    }
  };
  inventory.value.forEach((machine) => visit(machine.hardware_inventory));
  return [...addresses].sort();
});

function loadSriovConfig(deploymentId: string): void {
  const deployment = deployments.value.find((item) => item.id === deploymentId);
  const workerConfig = deployment?.spec.worker_provisioning;
  const configured = workerConfig && typeof workerConfig === "object"
    ? (workerConfig as Record<string, unknown>).sriov_device_plugin
    : null;
  sriovPools.value = configured && typeof configured === "object" && !Array.isArray(configured)
    ? JSON.parse(JSON.stringify(configured)) as Record<string, any>
    : {
        pool4: {
          node_selectors: { type: "high-throughput" },
          tolerations: [{ effect: "NoSchedule", key: "high-throughput", operator: "Equal", value: "true" }],
          sriov_network_device_plugin_configmap_name: "sriov-dp-configmap",
          sriov_network_device_plugin_configmap_data: { resourceList: [] },
        },
      };
  sriovSaveMessage.value = "";
}

async function saveSriovConfig(): Promise<void> {
  if (!selectedSriovDeploymentId.value || !sriovEditor.value?.validate()) return;
  sriovSaving.value = true;
  sriovSaveMessage.value = "";
  try {
    const updated = await updateClusterSriovDevicePlugin(
      selectedSriovDeploymentId.value,
      sriovPools.value,
    );
    const index = deployments.value.findIndex((item) => item.id === updated.id);
    if (index >= 0) deployments.value[index] = updated;
    sriovSaveMessage.value = "SR-IOV pool configuration saved to the deployment. It will be included in the Target Agent worker provisioning command.";
  } catch (cause) {
    sriovSaveMessage.value = cause instanceof Error ? cause.message : "Unable to save SR-IOV configuration.";
  } finally {
    sriovSaving.value = false;
  }
}

async function runPxeBoot(execution: boolean): Promise<void> {
  if (!selectedTenantId.value || !selectedProjectId.value) return;
  if (execution && !window.confirm("Set a one-time PXE boot override and reset every BMC-configured machine?")) {
    return;
  }
  pxeBootBusy.value = true;
  pxeBootMessage.value = "";
  try {
    pxeBootResults.value = await pxeBootFleetMachines(
      selectedTenantId.value,
      selectedProjectId.value,
      execution,
    );
    const result = pxeBootResults.value;
    pxeBootMessage.value =
      (execution ? "Executed " : "Planned ") +
      result.machine_count +
      " machine(s).";
    await refreshData();
  } catch (cause) {
    pxeBootMessage.value =
      cause instanceof Error ? cause.message : "Unable to reach the BMC fleet.";
  } finally {
    pxeBootBusy.value = false;
  }
}

async function ingestBmhInventory(): Promise<void> {
  if (!selectedTenantId.value || !selectedProjectId.value) return;
  bmhIngestMessage.value = "";
  bmhIngestResult.value = null;
  let document: unknown;
  try {
    document = JSON.parse(bmhDocumentText.value);
  } catch {
    bmhIngestMessage.value = "Inspection output must be valid JSON.";
    return;
  }
  bmhIngestBusy.value = true;
  try {
    bmhIngestResult.value = await ingestFleetBmhInventory(
      selectedTenantId.value,
      selectedProjectId.value,
      document,
    );
    bmhIngestMessage.value =
      bmhIngestResult.value.matched +
      " of " +
      bmhIngestResult.value.host_count +
      " host(s) matched.";
    await refreshData();
  } catch (cause) {
    bmhIngestMessage.value =
      cause instanceof Error ? cause.message : "Unable to ingest inspection output.";
  } finally {
    bmhIngestBusy.value = false;
  }
}

async function collectAllNetworks(): Promise<void> {
  if (!selectedTenantId.value || !selectedProjectId.value) return;
  collectBusy.value = true;
  collectMessage.value = "";
  try {
    const result = await collectFleetNetwork(selectedTenantId.value, selectedProjectId.value);
    collectMessage.value =
      "Queued " + result.queued_count + " node(s), skipped " + result.skipped_count + ".";
    await refreshData();
  } catch (cause) {
    collectMessage.value = cause instanceof Error ? cause.message : "Unable to queue network collection.";
  } finally {
    collectBusy.value = false;
  }
}

function firmwareMachineId(machine: FleetMachine): string {
  return machine.identity.machine_id ?? "";
}

function networkMachineId(machine: FleetMachine): string {
  return machine.identity.machine_id || "";
}

async function openNetworkPanel(machine: FleetMachine): Promise<void> {
  networkMachine.value = machine;
  networkVerification.value = null;
  networkObserveMessage.value = "";
  networkError.value = "";
  networkHostRole.value = "worker";
  networkDialogVisible.value = true;
  await loadNetworkVerification();
}

async function loadNetworkVerification(): Promise<void> {
  const machineId = networkMachine.value ? networkMachineId(networkMachine.value) : "";
  if (!machineId || !selectedTenantId.value || !selectedProjectId.value) {
    networkError.value = "This machine has no persisted hardware record.";
    return;
  }
  networkLoading.value = true;
  networkError.value = "";
  try {
    networkVerification.value = await verifyFleetMachineNetwork(
      selectedTenantId.value,
      selectedProjectId.value,
      machineId,
      networkHostRole.value,
    );
  } catch (cause) {
    networkVerification.value = null;
    networkError.value = cause instanceof Error ? cause.message : "Unable to verify host network.";
  } finally {
    networkLoading.value = false;
  }
}

async function requestNetworkObservation(): Promise<void> {
  const machine = networkMachine.value;
  if (!machine) return;
  networkObserveMessage.value = "";
  networkError.value = "";
  try {
    await createAgentCommand(machine.identity.agent_node_id, "host.network.observe", {});
    networkObserveMessage.value = "Observation queued. Wait for the Agent to finish, then re-check.";
  } catch (cause) {
    networkError.value = cause instanceof Error ? cause.message : "Unable to queue network observation.";
  }
}

function networkFindingType(status: string): "success" | "danger" | "warning" {
  if (status === "pass") return "success";
  if (status === "fail") return "danger";
  return "warning";
}

async function openFirmwarePanel(machine: FleetMachine): Promise<void> {
  if (!selectedTenantId.value || !selectedProjectId.value) return;
  firmwareMachine.value = machine;
  firmwareState.value = null;
  firmwareJobs.value = [];
  firmwareError.value = "";
  firmwareMessage.value = "";
  firmwareImageUrl.value = "";
  firmwareComponent.value = "";
  firmwareApplyTime.value = "Immediate";
  firmwareDialogVisible.value = true;
  await loadFirmwareState();
}

async function loadFirmwareState(): Promise<void> {
  const machineId = firmwareMachine.value ? firmwareMachineId(firmwareMachine.value) : "";
  if (!machineId) {
    firmwareError.value = "This machine has no persisted hardware record or BMC configuration.";
    return;
  }
  firmwareLoading.value = true;
  firmwareError.value = "";
  try {
    firmwareState.value = await getMachineFirmware(
      selectedTenantId.value,
      selectedProjectId.value,
      machineId,
    );
    firmwareJobs.value = firmwareState.value.jobs;
  } catch (cause) {
    firmwareError.value = cause instanceof Error ? cause.message : "Unable to load BMC firmware inventory.";
  } finally {
    firmwareLoading.value = false;
  }
}

async function applyFirmwareUpgrade(): Promise<void> {
  const machineId = firmwareMachine.value ? firmwareMachineId(firmwareMachine.value) : "";
  if (!machineId || !firmwareImageUrl.value.trim()) {
    firmwareError.value = "An HTTPS firmware image URL is required.";
    return;
  }
  firmwareBusy.value = true;
  firmwareError.value = "";
  firmwareMessage.value = "";
  try {
    const job = await submitFirmwareUpgrade(
      selectedTenantId.value,
      selectedProjectId.value,
      machineId,
      {
        image_url: firmwareImageUrl.value.trim(),
        component: firmwareComponent.value.trim(),
        apply_time: firmwareApplyTime.value,
      },
    );
    firmwareJobs.value = [job, ...firmwareJobs.value.filter((item) => item.id !== job.id)];
    firmwareMessage.value = "Firmware update submitted to the BMC. The task monitor is polled below.";
  } catch (cause) {
    firmwareError.value = cause instanceof Error ? cause.message : "Unable to submit firmware upgrade.";
  } finally {
    firmwareBusy.value = false;
  }
}

async function refreshFirmwareJob(job: FirmwareUpdateJob): Promise<void> {
  const machineId = firmwareMachine.value ? firmwareMachineId(firmwareMachine.value) : "";
  if (!machineId) return;
  try {
    const updated = await getFirmwareUpdateJob(
      selectedTenantId.value,
      selectedProjectId.value,
      machineId,
      job.id,
    );
    firmwareJobs.value = firmwareJobs.value.map((item) =>
      item.id === updated.id ? updated : item,
    );
  } catch (cause) {
    firmwareError.value = cause instanceof Error ? cause.message : "Unable to refresh firmware task.";
  }
}

function firmwareStatusType(status: string): "success" | "warning" | "danger" | "info" {
  if (status === "succeeded") return "success";
  if (status === "failed") return "danger";
  if (status === "running") return "warning";
  return "info";
}

function statusType(status: string): "success" | "warning" | "danger" | "info" {
  if (["online", "succeeded", "healthy"].includes(status)) return "success";
  if (["running", "queued", "waiting_approval", "provisioning", "preflight_queued"].includes(status)) return "warning";
  if (["failed", "revoked", "offline"].includes(status)) return "danger";
  return "info";
}

function deploymentDualAgent(deployment: ClusterDeployment): Record<string, unknown> {
  const dualAgent = deployment.plan_result?.dual_agent;
  return dualAgent && typeof dualAgent === "object" && !Array.isArray(dualAgent)
    ? (dualAgent as Record<string, unknown>)
    : {};
}

function deploymentPhaseLabel(deployment: ClusterDeployment): string {
  const phase = deploymentDualAgent(deployment).phase;
  if (phase === "pivot_simulation_ready") return "Pivot simulation ready";
  if (phase === "pivot_waiting_approval") return "Awaiting Pivot approval";
  if (phase === "target_agent_verification_pending") return "Target Agent verification pending";
  if (phase === "target_agent_adopted") return "Target Agent adopted cluster";
  if (phase === "pivot_metal3_ironic_postflight_pending") return "Metal3/Ironic postflight pending";
  if (phase === "pivot_postflight_verified") return "Pivot postflight verified";
  if (typeof phase === "string" && phase) return phase.replaceAll("_", " ");
  return "Handoff phase not reported";
}

function deploymentOwnershipLabel(deployment: ClusterDeployment): string {
  const dualAgent = deploymentDualAgent(deployment);
  if (dualAgent.target_agent_owns_cluster === true) return "Target Agent owns cluster";
  if (dualAgent.pivot_simulated === true) return "Simulation only · ownership unchanged";
  return "Ownership not transferred";
}

function nextKubernetesMinorVersion(deployment: ClusterDeployment): string | null {
  const currentMinor = Number(deployment.kubernetes_version.match(/^v1\.(\d+)/)?.[1]);
  if (!Number.isInteger(currentMinor)) return null;
  return supportedKubernetesVersions.find((version) => Number(version.split(".")[1]) === currentMinor + 1) ?? null;
}

function upgradeReadinessCommand(deployment: ClusterDeployment): AgentCommand | null {
  if (!deployment.target_node_id) return null;
  return (
    recentCommands.value.find(
      (command) =>
        command.command_type === "cluster.kubernetes.upgrade.readiness" &&
        command.node_id === deployment.target_node_id,
    ) ?? null
  );
}

function upgradeReadinessOutput(deployment: ClusterDeployment): Record<string, unknown> | null {
  const command = upgradeReadinessCommand(deployment);
  return command && command.result ? command.result : null;
}

async function loadEtcdBackup(deployment: ClusterDeployment) {
  backupBusy.value = deployment.id;
  backupMessages.value[deployment.id] = "";
  try {
    backupStates.value[deployment.id] = await getClusterDeploymentEtcdBackup(deployment.id);
  } catch (cause) {
    backupMessages.value[deployment.id] =
      cause instanceof Error ? cause.message : "Unable to read the backup state.";
  } finally {
    backupBusy.value = null;
  }
}

function backupAgeLabel(state: EtcdBackupState | undefined): string {
  if (!state || state.newest_age_hours === null || state.newest_age_hours === undefined) {
    return "no snapshot yet";
  }
  if (state.newest_age_hours < 1) return "minutes old";
  if (state.newest_age_hours < 48) return state.newest_age_hours.toFixed(1) + "h old";
  return (state.newest_age_hours / 24).toFixed(1) + "d old";
}

async function checkUpgradeReadiness(deployment: ClusterDeployment): Promise<void> {
  if (!deployment.target_node_id) {
    readinessMessages.value[deployment.id] = "A Target Agent must be assigned first.";
    return;
  }
  const targetAgent = nodes.value.find((node) => node.id === deployment.target_node_id);
  if (!targetAgent || targetAgent.status !== "online") {
    readinessMessages.value[deployment.id] = "The Target Agent must be online to report readiness.";
    return;
  }
  readinessBusy.value = deployment.id;
  readinessMessages.value[deployment.id] = "";
  try {
    await createAgentCommand(targetAgent.id, "cluster.kubernetes.upgrade.readiness", {});
    await refreshData();
    readinessMessages.value[deployment.id] =
      "Readiness probe queued; the Agent answers on its next poll.";
  } catch (cause) {
    readinessMessages.value[deployment.id] =
      cause instanceof Error ? cause.message : "Unable to request a readiness probe.";
  } finally {
    readinessBusy.value = null;
  }
}

function upgradeMode(deployment: ClusterDeployment): "simulation" | "execute" {
  return selectedUpgradeModes.value[deployment.id] ?? "simulation";
}

function setUpgradeMode(deploymentId: string, mode: "simulation" | "execute"): void {
  selectedUpgradeModes.value[deploymentId] = mode;
}

function selectedKubernetesVersion(deployment: ClusterDeployment): string {
  return selectedKubernetesVersions.value[deployment.id] ?? nextKubernetesMinorVersion(deployment) ?? "";
}

function isNextKubernetesMinorVersion(deployment: ClusterDeployment, version: string): boolean {
  return nextKubernetesMinorVersion(deployment) === version;
}

function setSelectedKubernetesVersion(deploymentId: string, version: string): void {
  selectedKubernetesVersions.value[deploymentId] = version;
}

function targetAgentOnline(deployment: ClusterDeployment): boolean {
  return nodes.value.some((node) => node.id === deployment.target_node_id && node.node_type === "target_cluster" && node.status === "online");
}

async function applyKubernetesVersion(deployment: ClusterDeployment): Promise<void> {
  if (!deployment.target_node_id) {
    versionApplyMessages.value[deployment.id] = "A Target Agent must be assigned before requesting a version change.";
    return;
  }
  const targetAgent = nodes.value.find((node) => node.id === deployment.target_node_id);
  if (!targetAgent || targetAgent.node_type !== "target_cluster" || targetAgent.status !== "online") {
    versionApplyMessages.value[deployment.id] = "The Target Agent must be online before requesting a version change.";
    return;
  }
  const targetVersion = selectedKubernetesVersion(deployment);
  versionApplyBusy.value = deployment.id;
  versionApplyMessages.value[deployment.id] = "";
  try {
    const command = await createAgentCommand(targetAgent.id, "cluster.kubernetes.version.upgrade", {
      cluster_name: deployment.cluster_name,
      current_version: deployment.kubernetes_version,
      target_version: targetVersion,
      execution_mode: upgradeMode(deployment),
    });
    versionApplyMessages.value[deployment.id] =
      upgradeMode(deployment) === "execute"
        ? "Version " + targetVersion + " execution request created (" + command.status + "); approval is required before the cluster is touched."
        : "Version " + targetVersion + " simulation request created (" + command.status + "); independent approval is required.";
    await refreshData();
  } catch (cause) {
    versionApplyMessages.value[deployment.id] = cause instanceof Error ? cause.message : "Unable to request Kubernetes version change.";
  } finally {
    versionApplyBusy.value = null;
  }
}

function identityStatusType(status: string): "success" | "warning" | "danger" | "info" {
  if (status === "verified") return "success";
  if (status === "conflict") return "danger";
  if (status === "candidate") return "warning";
  return "info";
}

function identityStatusLabel(status: string): string {
  if (status === "verified") return "Verified";
  if (status === "conflict") return "Conflict";
  if (status === "candidate") return "Candidate";
  return "Unverified";
}

function formatTime(value: string | null): string {
  return value ? new Date(value).toLocaleString() : "—";
}

function nodeLabel(nodeId: string): string {
  return nodes.value.find((node) => node.id === nodeId)?.name ?? nodeId.slice(0, 8);
}

function commandDetail(command: AgentCommand): string {
  if (command.error) return JSON.stringify(command.error);
  if (command.result) return JSON.stringify(command.result);
  return JSON.stringify(command.payload);
}

function kubernetesUpgradeSteps(command: AgentCommand): Array<Record<string, unknown>> {
  const steps = command.result?.upgrade_steps;
  return Array.isArray(steps)
    ? steps.filter((step): step is Record<string, unknown> =>
        Boolean(step) && typeof step === "object" && !Array.isArray(step),
      )
    : [];
}

function rollbackSteps(command: AgentCommand): Array<Record<string, unknown>> {
  const steps = command.result?.steps;
  return Array.isArray(steps)
    ? steps.filter((step): step is Record<string, unknown> =>
        Boolean(step) && typeof step === "object" && !Array.isArray(step),
      )
    : [];
}

function rollbackPreflight(command: AgentCommand) {
  const preflight = command.result?.preflight;
  if (!preflight || typeof preflight !== "object" || Array.isArray(preflight)) return null;
  const checks = (preflight as Record<string, unknown>).checks;
  return Array.isArray(checks)
    ? (checks as Array<Record<string, unknown>>).filter(
        (check) => Boolean(check) && typeof check === "object" && typeof check.name === "string",
      )
    : null;
}

function rollbackBlocked(command: AgentCommand): boolean {
  const preflight = command.result?.preflight;
  return Boolean(preflight) && typeof preflight === "object" && (preflight as Record<string, unknown>).blocked === true;
}

function upgradeStepDetail(step: Record<string, unknown>): string {
  const exitCode = step.exit_code;
  const detail = typeof step.detail === "string" ? step.detail.trim() : "";
  const commands = Array.isArray(step.commands) ? step.commands.filter((item) => typeof item === "string") : [];
  const parts: string[] = [];
  if (typeof exitCode === "number" && exitCode !== 0) parts.push("exit " + exitCode);
  if (commands.length) parts.push(commands.join(" → "));
  if (detail) parts.push(detail.length > 240 ? detail.slice(0, 240) + "…" : detail);
  return parts.join(" · ");
}

function upgradeCommandMode(command: AgentCommand): "simulation" | "execute" {
  return command.payload.execution_mode === "execute" ? "execute" : "simulation";
}

function upgradePreflight(command: AgentCommand) {
  return getKubernetesUpgradePreflight(command.result);
}

function upgradePreflightFailedCount(command: AgentCommand): number {
  const preflight = upgradePreflight(command);
  return preflight ? preflight.checks.filter((check) => !check.ok).length : 0;
}

function recoveryCommands(nodeId: string): AgentCommand[] {
  return recentCommands.value.filter(
    (command) =>
      command.command_type === "cluster.kubernetes.recovery.report" && command.node_id === nodeId,
  );
}

function latestRecoveryCommand(nodeId: string): AgentCommand | null {
  return recoveryCommands(nodeId)[0] ?? null;
}

function recoveryReport(nodeId: string) {
  return getKubernetesRecoveryReport(latestRecoveryCommand(nodeId)?.result ?? null);
}

function recoveryTargetVersion(deployment: ClusterDeployment): string {
  const lastUpgrade = kubernetesUpgradeCommands.value.find(
    (command) => command.node_id === deployment.target_node_id,
  );
  const target = lastUpgrade?.payload.target_version;
  return typeof target === "string" && target ? target : deployment.kubernetes_version;
}

async function requestRecoveryReport(deployment: ClusterDeployment): Promise<void> {
  if (!deployment.target_node_id) {
    recoveryMessages.value[deployment.id] = "A Target Agent must be assigned first.";
    return;
  }
  const targetAgent = nodes.value.find((node) => node.id === deployment.target_node_id);
  if (!targetAgent || targetAgent.node_type !== "target_cluster" || targetAgent.status !== "online") {
    recoveryMessages.value[deployment.id] = "The Target Agent must be online to report recovery state.";
    return;
  }
  recoveryBusy.value = deployment.id;
  recoveryMessages.value[deployment.id] = "";
  try {
    await createAgentCommand(targetAgent.id, "cluster.kubernetes.recovery.report", {
      target_version: recoveryTargetVersion(deployment),
    });
    await refreshData();
    recoveryMessages.value[deployment.id] =
      "Read-only recovery report queued; nothing on the cluster is touched.";
  } catch (cause) {
    recoveryMessages.value[deployment.id] =
      cause instanceof Error ? cause.message : "Unable to request a recovery report.";
  } finally {
    recoveryBusy.value = null;
  }
}

async function requestRollback(deployment: ClusterDeployment): Promise<void> {
  const snapshot = (rollbackSnapshots.value[deployment.id] ?? "").trim();
  if (!deployment.target_node_id) {
    rollbackMessages.value[deployment.id] = "A Target Agent must be assigned first.";
    return;
  }
  if (!snapshot.startsWith("/") || !snapshot.endsWith(".db")) {
    rollbackMessages.value[deployment.id] = "Enter the absolute .db snapshot path written before the upgrade.";
    return;
  }
  const targetAgent = nodes.value.find((node) => node.id === deployment.target_node_id);
  if (!targetAgent || targetAgent.node_type !== "target_cluster" || targetAgent.status !== "online") {
    rollbackMessages.value[deployment.id] = "The Target Agent must be online to restore a snapshot.";
    return;
  }
  rollbackBusy.value = deployment.id;
  rollbackMessages.value[deployment.id] = "";
  try {
    const command = await createAgentCommand(targetAgent.id, "cluster.kubernetes.version.rollback", {
      cluster_name: deployment.cluster_name,
      snapshot_path: snapshot,
      execution_mode: "execute",
      confirm_cluster_name: deployment.cluster_name,
    });
    rollbackMessages.value[deployment.id] =
      "Rollback request " + command.status + "; approval is required. It stops the kubelet, restores the snapshot and keeps the old etcd data on disk.";
    await refreshData();
  } catch (cause) {
    rollbackMessages.value[deployment.id] =
      cause instanceof Error ? cause.message : "Unable to request a rollback.";
  } finally {
    rollbackBusy.value = null;
  }
}

function upgradeEtcdHealth(command: AgentCommand) {
  return getKubernetesUpgradeEtcdHealth(command.result);
}

function upgradeStepStatus(step: Record<string, unknown>): string {
  const status = step.status;
  return typeof status === "string" ? status.replaceAll("_", " ") : "not reported";
}

watch(selectedTenantId, (tenantId) => void loadProjects(tenantId));
watch(selectedProjectId, () => void refreshData());
watch(selectedSriovDeploymentId, (deploymentId) => loadSriovConfig(deploymentId));

onMounted(async () => {
  void loadLabWorkflow();
  await loadTenants();
  refreshTimer = window.setInterval(() => void refreshData(), 15000);
});
onUnmounted(() => {
  if (refreshTimer !== undefined) window.clearInterval(refreshTimer);
});
</script>

<template>
  <section class="metal3-fleet">
    <header class="fleet-heading">
      <div>
        <p class="eyebrow">BARE-METAL CONTROL PLANE</p>
        <h2>Metal3 Fleet</h2>
        <p>EPH bootstrap agents, Target Cluster agents, deployment workflows and persisted command traces.</p>
      </div>
      <div class="fleet-scope">
        <el-select v-model="selectedTenantId" placeholder="Tenant" size="small" style="width: 180px">
          <el-option v-for="tenant in tenants" :key="tenant.id" :label="tenant.name" :value="tenant.id" />
        </el-select>
        <el-select v-model="selectedProjectId" placeholder="Project" size="small" style="width: 180px">
          <el-option v-for="project in projects" :key="project.id" :label="project.name" :value="project.id" />
        </el-select>
        <el-button plain size="small" :loading="refreshing || loading" @click="refreshData">Refresh</el-button>
      </div>
    </header>

    <el-alert v-if="error" :title="error" type="error" show-icon :closable="true" @close="error = ''" />

    <nav class="fleet-tabs">
      <button
        v-for="tab in tabs"
        :key="tab.id"
        :class="{ active: activeTab === tab.id }"
        @click="activeTab = tab.id"
      >{{ tab.label }}</button>
      <span class="fleet-live"><i></i> POLL 15s</span>
    </nav>

    <template v-if="activeTab === 'dashboard'">
      <div class="fleet-metrics">
        <article><span>Online Agents</span><strong>{{ onlineNodes.length }} / {{ nodes.length }}</strong><small>Current project scope</small></article>
        <article><span>EPH Bootstrap Nodes</span><strong>{{ ephNodes.length }}</strong><small>Temporary management plane</small></article>
        <article><span>Target Cluster Agents</span><strong>{{ targetAgents.length }}</strong><small>Target-side supervisors</small></article>
        <article><span>Active Deployments</span><strong>{{ activeDeployments.length }}</strong><small>{{ failedDeployments.length }} failed</small></article>
      </div>
      <div class="fleet-two-column">
        <section class="fleet-panel">
          <div class="fleet-panel-heading"><h3>Agent topology</h3><span>{{ nodes.length }} NODES</span></div>
          <div v-for="node in nodes" :key="node.id" class="fleet-node-row">
            <div class="fleet-node-icon"><span class="fleet-node-glyph">▦</span></div>
            <div class="fleet-node-main">
              <strong>{{ node.name }}</strong>
              <small>{{ node.node_type === "eph_node" ? "EPH bootstrap" : "Target cluster" }} · {{ node.cluster_name || "cluster unassigned" }}</small>
            </div>
            <el-tag :type="statusType(node.status)" size="small">{{ node.status }}</el-tag>
          </div>
          <el-empty v-if="!loading && !inventory.length" description="No machine inventory records match this project and filter" :image-size="56" />
        <p class="fleet-footnote">Hardware identity reported by an Agent remains unverified until an independent BMC or hardware-backed attestation workflow is completed.</p>
        </section>
        <section class="fleet-panel">
          <div class="fleet-panel-heading"><h3>Deployment pipeline</h3><span>{{ deployments.length }} CLUSTERS</span></div>
          <div class="lab-workflow-tag">
            <el-tag
              :type="labWorkflow?.ready ? 'success' : 'warning'"
              size="small"
              effect="plain"
            >
              Lab acceptance ·
              {{ labWorkflow ? (labWorkflow.ready ? "workflow ready" : labWorkflow.available ? "workflow incomplete" : "workflow not deployed") : "probe unavailable" }}
            </el-tag>
            <el-link
              v-if="labWorkflow?.workflow_url"
              :href="labWorkflow.workflow_url"
              target="_blank"
              :underline="false"
            >Open workflow</el-link>
            <small v-if="labWorkflow">
              Physical result requires runner labels
              {{ labWorkflow.acceptance_job?.runs_on.join(", ") || "not declared" }}; secret presence
              is decided by the readiness job, not here.
            </small>
          </div>
          <div v-for="deployment in deployments.slice(0, 6)" :key="deployment.id" class="fleet-deployment-row">
            <div>
              <strong>{{ deployment.cluster_name }}</strong>
              <small>{{ deployment.kubernetes_version }} · {{ formatTime(deployment.updated_at) }}</small>
              <small>{{ deploymentPhaseLabel(deployment) }}</small>
              <small v-if="deployment.network_spec_status?.migration" class="readiness-blocked">
                Network spec v{{ deployment.network_spec_status.migration.version }} ·
                re-plan required (v{{ deployment.network_spec_status.migration.current_version }} available)
              </small>
            </div>
            <el-tag
              :type="deploymentDualAgent(deployment).pivot_simulated === true ? 'info' : statusType(deployment.status)"
              size="small"
            >
              {{ deploymentDualAgent(deployment).pivot_simulated === true ? 'SIMULATION' : deployment.status }}
            </el-tag>
          </div>
          <el-empty v-if="!loading && !deployments.length" description="No cluster delivery workflows yet" :image-size="56" />
        </section>
      </div>
      <p class="fleet-footnote">Last recorded activity: {{ formatTime(latestActivity) }}. Data is sourced from persisted Agent node, command and ClusterDeployment APIs; no simulated hardware telemetry is displayed.</p>
    </template>

    <template v-else-if="activeTab === 'fleet'">
      <section class="fleet-panel">
        <div class="fleet-panel-heading"><h3>Bare-metal Machine Inventory</h3><span>{{ inventoryCount }} REGISTERED</span></div>
        <div class="lab-workflow-tag">
          <el-tag
            :type="labWorkflow?.ready ? 'success' : 'warning'"
            size="small"
            effect="plain"
          >
            Metal3 lab e2e ·
            {{ labWorkflow ? (labWorkflow.ready ? "workflow ready" : labWorkflow.available ? "workflow incomplete" : "workflow not deployed") : "probe unavailable" }}
          </el-tag>
          <el-tag
            v-if="labWorkflow?.last_run"
            :type="labWorkflow.last_run.conclusion === 'success' ? 'success' : labWorkflow.last_run.conclusion === 'failure' ? 'danger' : 'warning'"
            size="small"
            effect="dark"
          >
            Last run #{{ labWorkflow.last_run.run_number }} ·
            {{ labWorkflow.last_run.conclusion || labWorkflow.last_run.status }}
          </el-tag>
          <el-link
            v-if="labWorkflow?.workflow_url"
            :href="labWorkflow.workflow_url"
            target="_blank"
            :underline="false"
          >Open workflow</el-link>
          <el-link
            v-if="labWorkflow?.last_run?.html_url"
            :href="labWorkflow.last_run.html_url"
            target="_blank"
            :underline="false"
          >Open last run</el-link>
          <small v-if="labWorkflow">
            Runner labels {{ labWorkflow.acceptance_job?.runs_on.join(", ") || "not declared" }} ·
            secrets referenced {{ labWorkflow.secrets_referenced.join(", ") || "none" }}
          </small>
          <small class="lab-workflow-note">
            {{ labWorkflow?.secret_presence === "observed"
              ? "A GitHub token is configured, so the last run above is the real lab result."
              : "Secret values are not visible to the platform; set LAB_WORKFLOW_REPOSITORY and LAB_WORKFLOW_TOKEN to show the last real run." }}
          </small>
          <small
            v-for="check in labWorkflow?.checks || []"
            :key="'lab-' + check.name"
            :class="check.ok ? 'lab-workflow-ok' : 'readiness-blocked'"
          >{{ check.ok ? "✓" : "✗" }} {{ check.name }}: {{ check.detail }}</small>
          <small v-if="labWorkflowError" class="readiness-blocked">{{ labWorkflowError }}</small>
        </div>
        <div class="fleet-inventory-controls">
          <el-input v-model="inventorySearch" clearable size="small" placeholder="Search machine, cluster or agent version" @keyup.enter="refreshData" />
          <el-select v-model="inventoryNodeType" clearable size="small" placeholder="All roles">
            <el-option label="EPH Bootstrap" value="eph_node" />
            <el-option label="Target Cluster" value="target_cluster" />
          </el-select>
          <el-select v-model="inventoryStatus" clearable size="small" placeholder="All statuses">
            <el-option label="Online" value="online" />
            <el-option label="Stale heartbeat" value="stale" />
            <el-option label="Offline" value="offline" />
            <el-option label="Revoked" value="revoked" />
          </el-select>
          <el-button size="small" type="primary" plain :loading="refreshing" @click="refreshData">Apply filters</el-button>
          <el-button size="small" plain :loading="collectBusy" @click="collectAllNetworks">
            Collect all node networks
          </el-button>
          <el-button size="small" plain :loading="pxeBootBusy" @click="runPxeBoot(false)">
            Plan PXE boot
          </el-button>
          <el-button size="small" type="warning" :loading="pxeBootBusy" @click="runPxeBoot(true)">
            Execute PXE boot
          </el-button>
          <el-button size="small" type="danger" :loading="discoveryBusy" @click="runDiscovery">
            Run discovery (PXE + inspect)
          </el-button>
          <el-button size="small" plain @click="bmhDialogVisible = true">
            Ingest BMH inspection
          </el-button>
        </div>
        <p v-if="collectMessage" class="task-note">{{ collectMessage }}</p>
        <p v-if="discoveryMessage" class="task-note">{{ discoveryMessage }}</p>
        <div v-if="discoveryResult" class="pxe-boot-results">
          <small>Discovery chain</small>
          <ul>
            <li v-for="item in discoveryResult.results" :key="'discovery-' + item.machine_id">
              {{ item.name }}: {{ item.status }}<template v-if="item.error_code"> · {{ item.error_code }}</template>
            </li>
          </ul>
        </div>
        <p v-if="pxeBootMessage" class="task-note">{{ pxeBootMessage }}</p>
        <p v-if="lifecycleMessage" class="task-note">{{ lifecycleMessage }}</p>
        <div v-if="pxeBootResults" class="pxe-boot-results">
          <small>PXE boot ({{ pxeBootResults.execution ? "executed" : "planned" }})</small>
          <ul>
            <li v-for="item in pxeBootResults.results" :key="item.machine_id">
              {{ item.name }}: {{ item.status }}<template v-if="item.error_code"> · {{ item.error_code }}</template>
            </li>
          </ul>
        </div>
        <el-table :data="inventory" size="small" class="task-table">
          <el-table-column prop="name" label="Agent / Node" min-width="160" />
          <el-table-column label="Role" min-width="140">
            <template #default="scope">{{ scope.row.node_type === "eph_node" ? "EPH Bootstrap" : "Target Cluster" }}</template>
          </el-table-column>
          <el-table-column prop="cluster_name" label="Cluster" min-width="150" />
          <el-table-column label="Status" min-width="110">
            <template #default="scope"><el-tag :type="statusType(scope.row.observed_status)" size="small">{{ scope.row.observed_status }}</el-tag></template>
          </el-table-column>
          <el-table-column label="Vendor / Product" min-width="190">
            <template #default="scope">
              <span>{{ scope.row.hardware_inventory.system_identity?.sys_vendor || "—" }} / {{ scope.row.hardware_inventory.system_identity?.product_name || "—" }}</span>
            </template>
          </el-table-column>
          <el-table-column label="Hardware Identity" min-width="150">
            <template #default="scope">
              <el-tag :type="identityStatusType(scope.row.identity.identity_status)" size="small">
                {{ identityStatusLabel(scope.row.identity.identity_status) }}
              </el-tag>
              <small v-if="scope.row.identity.identity_conflicts.length" class="identity-conflict-count">
                {{ scope.row.identity.identity_conflicts.length }} conflict(s)
              </small>
            </template>
          </el-table-column>
          <el-table-column label="BMC" min-width="150">
            <template #default="scope">
              <el-tag :type="scope.row.bmc_configured ? 'success' : 'warning'" size="small">
                {{ scope.row.bmc_configured ? (scope.row.bmc_host || "configured") : "not configured" }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="Lifecycle" min-width="130">
            <template #default="scope">
              <el-tag size="small" type="info">{{ scope.row.lifecycle_state || "n/a" }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="bmh_name" label="BMH" min-width="120">
            <template #default="scope">{{ scope.row.bmh_name || "—" }}</template>
          </el-table-column>
          <el-table-column prop="agent_version" label="Agent Version" min-width="130" />
          <el-table-column label="Inventory Observed" min-width="170">
            <template #default="scope">{{ formatTime(scope.row.observed_at) }}</template>
          </el-table-column>
          <el-table-column label="Last Heartbeat" min-width="170">
            <template #default="scope">{{ formatTime(scope.row.last_seen_at) }}</template>
          </el-table-column>
          <el-table-column prop="last_seen_ip" label="Source IP" min-width="140" />
          <el-table-column label="Hardware" min-width="180">
            <template #default="scope">
              <span v-if="scope.row.hardware_inventory.cpu">CPU {{ String((scope.row.hardware_inventory.cpu as Record<string, unknown>).logical_cpus ?? "reported") }}</span>
              <span v-else>Not reported</span>
            </template>
          </el-table-column>
          <el-table-column label="Capabilities" min-width="180">
            <template #default="scope">{{ scope.row.capabilities.join(", ") || "—" }}</template>
          </el-table-column>
          <el-table-column label="Actions" min-width="150" fixed="right">
            <template #default="scope">
              <el-button
                size="small"
                plain
                :disabled="!scope.row.identity.machine_id"
                @click="openFirmwarePanel(scope.row)"
              >Firmware</el-button>
              <el-button
                size="small"
                plain
                :disabled="!scope.row.identity.machine_id"
                @click="openNetworkPanel(scope.row)"
              >Network</el-button>
              <el-button
                size="small"
                plain
                :disabled="!scope.row.machine_id"
                @click="openBmcDialog(scope.row)"
              >BMC</el-button>
              <el-dropdown
                v-if="scope.row.lifecycle_targets.length"
                trigger="click"
                @command="(target: string) => moveMachineLifecycle(scope.row, target)"
              >
                <el-button size="small" plain :loading="lifecycleBusy === scope.row.machine_id">
                  Lifecycle
                </el-button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item
                      v-for="target in scope.row.lifecycle_targets"
                      :key="target"
                      :command="target"
                    >Move to {{ target }}</el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
            </template>
          </el-table-column>
        </el-table>

        <el-dialog v-model="networkDialogVisible" title="Host Network Verification" width="820px">
          <div v-if="networkMachine" class="firmware-panel">
            <div class="fleet-panel-heading">
              <h3>{{ networkMachine.name }} · Declared vs observed</h3>
              <div>
                <el-button size="small" @click="requestNetworkObservation">Read OS network</el-button>
                <el-button size="small" :loading="networkLoading" @click="loadNetworkVerification">Re-check</el-button>
              </div>
            </div>
            <div class="fleet-version-control" style="margin-bottom: 12px">
              <el-select v-model="networkHostRole" size="small" style="width: 170px" @change="loadNetworkVerification">
                <el-option label="Control plane" value="control_plane" />
                <el-option label="Worker" value="worker" />
                <el-option label="Storage" value="storage" />
              </el-select>
              <el-tag v-if="networkVerification" :type="networkFindingType(networkVerification.status)" size="small">
                {{ networkVerification.status }}
              </el-tag>
            </div>
            <el-alert
              v-if="networkObserveMessage"
              :title="networkObserveMessage"
              type="success"
              show-icon
              :closable="false"
            />
            <el-alert
              v-if="networkError"
              :title="networkError"
              type="warning"
              show-icon
              :closable="false"
            />
            <template v-if="networkVerification">
              <p class="fleet-footnote">
                Checked: {{ networkVerification.checked_networks.join(", ") || "none" }} ·
                OS interfaces: {{ networkVerification.observed_interfaces.join(", ") || "none" }}
              </p>
              <el-table :data="networkVerification.observed_nics" size="small" class="task-table">
                <el-table-column prop="name" label="OS interface" min-width="130" />
                <el-table-column prop="mtu" label="MTU" min-width="70" />
                <el-table-column prop="pci_address" label="PCI address" min-width="140" />
                <el-table-column prop="driver" label="Driver" min-width="100" />
                <el-table-column label="VLANs" min-width="100">
                  <template #default="scope">{{ (scope.row.vlans || []).join(", ") || "—" }}</template>
                </el-table-column>
                <el-table-column label="SR-IOV VFs" min-width="110">
                  <template #default="scope">
                    <span v-if="scope.row.sriov_numvfs !== undefined">
                      {{ scope.row.sriov_numvfs }} / {{ scope.row.sriov_totalvfs ?? "?" }}
                    </span>
                    <span v-else>—</span>
                  </template>
                </el-table-column>
              </el-table>
              <el-table :data="networkVerification.findings" size="small" class="task-table">
                <el-table-column prop="code" label="Finding" min-width="220" />
                <el-table-column label="Status" min-width="90">
                  <template #default="scope">
                    <el-tag :type="networkFindingType(scope.row.status)" size="small">{{ scope.row.status }}</el-tag>
                  </template>
                </el-table-column>
                <el-table-column prop="network" label="Network" min-width="110" />
                <el-table-column prop="interface" label="Interface" min-width="110" />
                <el-table-column label="Expected" min-width="150">
                  <template #default="scope">{{ scope.row.expected ?? "—" }}</template>
                </el-table-column>
                <el-table-column label="Observed" min-width="150">
                  <template #default="scope">{{ scope.row.observed ?? "—" }}</template>
                </el-table-column>
              </el-table>
              <p v-if="!networkVerification.findings.length" class="fleet-footnote">No findings.</p>
            </template>
          </div>
        </el-dialog>

        <el-dialog v-model="bmcDialogVisible" title="BMC Registration" width="560px">
          <p class="task-note">
            Redfish endpoint and the credential reference the platform resolves at dispatch time.
            The credential itself is never stored here or echoed back.
          </p>
          <label>BMC endpoint
            <el-input v-model="bmcEndpoint" placeholder="https://10.0.0.11" />
          </label>
          <label>Credential reference
            <el-input v-model="bmcCredentialRef" placeholder="vault/bmc/master-0" />
          </label>
          <label>Trust bundle reference (optional)
            <el-input v-model="bmcTrustBundleRef" placeholder="vault/ca/lab" />
          </label>
          <p class="task-note">Machine: {{ bmcDialogMachine?.name }} · {{ bmcDialogMachine?.machine_id }}</p>
        <p v-if="bmcMessage" class="task-note">{{ bmcMessage }}</p>
          <template #footer>
            <el-button @click="bmcDialogVisible = false">Cancel</el-button>
            <el-button type="primary" :loading="bmcBusy" @click="saveBmcConfig">Save BMC configuration</el-button>
          </template>
        </el-dialog>

        <el-dialog v-model="firmwareDialogVisible" title="BMC Firmware Management" width="880px">
          <div v-if="firmwareMachine" class="firmware-panel">
            <div class="fleet-panel-heading">
              <h3>{{ firmwareMachine.name }} · Redfish UpdateService</h3>
              <el-button size="small" :loading="firmwareLoading" @click="loadFirmwareState">Reload</el-button>
            </div>
            <el-alert
              v-if="firmwareError"
              :title="firmwareError"
              type="error"
              show-icon
              :closable="false"
            />
            <el-alert
              v-if="firmwareMessage"
              :title="firmwareMessage"
              type="success"
              show-icon
              :closable="false"
            />
            <el-alert
              title="Firmware updates are enabled for HPE, Dell, Inspur, H3C and Lenovo BMCs only."
              type="info"
              show-icon
              :closable="false"
            />
            <el-alert
              v-if="firmwareState && !firmwareState.bmc_configured"
              title="Configure the BMC endpoint and credential reference before managing firmware."
              type="warning"
              show-icon
              :closable="false"
            />
            <div v-if="firmwareState" class="firmware-summary">
              <span>Service enabled: <strong>{{ firmwareState.service_enabled ? "Yes" : "No" }}</strong></span>
              <span>State: <strong>{{ firmwareState.service_state || "Unknown" }}</strong></span>
              <span>Components: <strong>{{ firmwareState.firmware?.length ?? 0 }}</strong></span>
            </div>
            <el-table v-if="firmwareState?.firmware?.length" :data="firmwareState.firmware" size="small" class="task-table">
              <el-table-column prop="name" label="Component" min-width="180" />
              <el-table-column prop="version" label="Version" min-width="140" />
              <el-table-column prop="status" label="Health" min-width="100" />
              <el-table-column label="Updateable" min-width="110">
                <template #default="scope">{{ scope.row.updateable ? "Yes" : "No" }}</template>
              </el-table-column>
            </el-table>

            <div class="firmware-upgrade-form">
              <h4>SimpleUpdate</h4>
              <el-input v-model="firmwareImageUrl" size="small" placeholder="https://images.example.com/firmware.bin" />
              <el-input v-model="firmwareComponent" size="small" placeholder="Component filter (optional, e.g. BMC)" />
              <el-select v-model="firmwareApplyTime" size="small">
                <el-option label="Immediate" value="Immediate" />
                <el-option label="On reset" value="OnReset" />
                <el-option label="At maintenance window" value="AtMaintenanceWindowStart" />
              </el-select>
              <el-button type="danger" size="small" :loading="firmwareBusy" @click="applyFirmwareUpgrade">Submit firmware upgrade</el-button>
            </div>

            <h4>Recent firmware jobs</h4>
            <el-table v-if="firmwareJobs.length" :data="firmwareJobs" size="small" class="task-table">
              <el-table-column prop="status" label="Status" min-width="110">
                <template #default="scope"><el-tag :type="firmwareStatusType(scope.row.status)" size="small">{{ scope.row.status }}</el-tag></template>
              </el-table-column>
              <el-table-column prop="task_state" label="Task state" min-width="120" />
              <el-table-column prop="image_url" label="Image" min-width="220" show-overflow-tooltip />
              <el-table-column prop="apply_time" label="Apply time" min-width="140" />
              <el-table-column label="Updated" min-width="160">
                <template #default="scope">{{ formatTime(scope.row.updated_at) }}</template>
              </el-table-column>
              <el-table-column label="Actions" min-width="100">
                <template #default="scope">
                  <el-button size="small" text @click="refreshFirmwareJob(scope.row)">Refresh</el-button>
                </template>
              </el-table-column>
            </el-table>
            <p v-else class="fleet-footnote">No firmware upgrades have been submitted for this machine.</p>
          </div>
        </el-dialog>
      </section>
    </template>

    <template v-else-if="activeTab === 'eph'">
      <section class="fleet-panel">
        <div class="fleet-panel-heading"><h3>EPH Temporary Bootstrap Nodes</h3><span>{{ ephNodes.length }} NODES</span></div>
        <div v-for="node in ephNodes" :key="node.id" class="eph-node-card">
          <div class="eph-node-header">
            <div><p class="eyebrow">TEMPORARY MANAGEMENT PLANE</p><h3>{{ node.name }}</h3></div>
            <el-tag :type="statusType(node.status)" size="small">{{ node.status }}</el-tag>
          </div>
          <div class="eph-node-grid">
            <div><span>Cluster binding</span><strong>{{ node.cluster_name || "Not assigned" }}</strong></div>
            <div><span>Agent version</span><strong>{{ node.agent_version }}</strong></div>
            <div><span>Last heartbeat</span><strong>{{ formatTime(node.last_seen_at) }}</strong></div>
            <div><span>Last source IP</span><strong>{{ node.last_seen_ip || "—" }}</strong></div>
          </div>
          <div class="fleet-capabilities"><span v-for="capability in node.capabilities" :key="capability">{{ capability }}</span></div>
          <p class="fleet-footnote">EPH lifecycle: RAM-booted single-node Kubernetes, EPH Agent takeover, CAPI management pivot, then hardware reclaim as a Target Worker. The steps are shown as architecture requirements; this page reports only the Agent state currently confirmed by the backend.</p>
        </div>
        <el-empty v-if="!loading && !ephNodes.length" description="No EPH bootstrap Agent enrolled" :image-size="56" />
      </section>
    </template>

    <template v-else-if="activeTab === 'clusters'">
      <section class="fleet-panel">
        <div class="fleet-panel-heading"><h3>Target Cluster Delivery</h3><span>{{ deployments.length }} WORKFLOWS</span></div>
        <el-table :data="deployments" size="small" class="task-table">
          <el-table-column prop="cluster_name" label="Cluster" min-width="150" />
          <el-table-column label="etcd Backups" min-width="240">
            <template #default="scope">
              <el-button
                size="small"
                text
                :loading="backupBusy === scope.row.id"
                @click="loadEtcdBackup(scope.row)"
              >Backups</el-button>
              <template v-if="backupStates[scope.row.id]">
                <el-tag
                  :type="backupStates[scope.row.id].stale ? 'warning' : 'success'"
                  size="small"
                  effect="plain"
                >{{ backupStates[scope.row.id].stale ? "stale" : "fresh" }}</el-tag>
                <small class="fleet-version-current">
                  {{ backupStates[scope.row.id].newest?.name || "no snapshot yet" }}
                  · {{ backupAgeLabel(backupStates[scope.row.id]) }}
                </small>
                <small
                  v-if="backupStates[scope.row.id].pre_upgrade_count"
                  class="fleet-version-current"
                >pre-upgrade snapshots: {{ backupStates[scope.row.id].pre_upgrade_count }}</small>
                <small
                  v-for="finding in backupStates[scope.row.id].findings.filter((item) => !item.ok)"
                  :key="scope.row.id + finding.code"
                  class="readiness-blocked"
                >{{ finding.code }}: {{ finding.detail }}</small>
              </template>
              <small v-if="backupMessages[scope.row.id]" class="readiness-blocked">{{ backupMessages[scope.row.id] }}</small>
            </template>
          </el-table-column>
          <el-table-column label="Kubernetes Version" min-width="310">
            <template #default="scope">
              <div class="fleet-version-control">
                <el-select
                  :model-value="selectedKubernetesVersion(scope.row)"
                  size="small"
                  style="width: 112px"
                  @change="(value: string) => setSelectedKubernetesVersion(scope.row.id, value)"
                >
                  <el-option
                    v-for="version in supportedKubernetesVersions"
                    :key="version"
                    :label="version"
                    :value="version"
                    :disabled="!isNextKubernetesMinorVersion(scope.row, version)"
                  />
                </el-select>
                <el-select
                  :model-value="upgradeMode(scope.row)"
                  size="small"
                  style="width: 108px"
                  @change="(value: string) => setUpgradeMode(scope.row.id, value as 'simulation' | 'execute')"
                >
                  <el-option v-for="mode in upgradeModes" :key="mode" :label="mode" :value="mode" />
                </el-select>
                <el-button
                  size="small"
                  type="primary"
                  plain
                  :loading="versionApplyBusy === scope.row.id"
                  :disabled="!targetAgentOnline(scope.row) || !isNextKubernetesMinorVersion(scope.row, selectedKubernetesVersion(scope.row))"
                  @click="applyKubernetesVersion(scope.row)"
                >Apply</el-button>
              </div>
              <small class="fleet-version-current">Current: {{ scope.row.kubernetes_version }} · Upgrade one minor version at a time</small>
              <small v-if="versionApplyMessages[scope.row.id]" class="fleet-version-message">{{ versionApplyMessages[scope.row.id] }}</small>
              <small v-if="upgradeMode(scope.row) === 'execute'" class="readiness-blocked">
                Execute mode runs kubeadm, package upgrades and drain/uncordon on the cluster.
              </small>
              <div class="fleet-version-control">
                <el-button
                  size="small"
                  text
                  :loading="readinessBusy === scope.row.id"
                  @click="checkUpgradeReadiness(scope.row)"
                >Check upgrade readiness</el-button>
                <template v-if="upgradeReadinessOutput(scope.row)">
                  <el-tag
                    :type="upgradeReadinessOutput(scope.row)?.execute_ready ? 'success' : 'warning'"
                    size="small"
                    effect="plain"
                  >
                    {{ upgradeReadinessOutput(scope.row)?.execute_ready ? "execute ready" : "execute not ready" }}
                  </el-tag>
                  <small
                    v-if="!upgradeReadinessOutput(scope.row)?.remote_executor_configured"
                    class="readiness-blocked"
                  >workers need KUBERNETES_UPGRADE_REMOTE_USER</small>
                  <small
                    v-for="item in (upgradeReadinessOutput(scope.row)?.missing as string[] | undefined) || []"
                    :key="'missing-' + item"
                    class="readiness-blocked"
                  >missing {{ item }}</small>
                </template>
              </div>
              <div class="fleet-version-control">
                <el-button
                  size="small"
                  text
                  :loading="recoveryBusy === scope.row.id"
                  @click="requestRecoveryReport(scope.row)"
                >Recovery report</el-button>
                <template v-if="recoveryReport(scope.row.target_node_id)">
                  <el-tag
                    :type="recoveryReport(scope.row.target_node_id)?.drift ? 'warning' : 'success'"
                    size="small"
                    effect="plain"
                  >
                    {{ recoveryReport(scope.row.target_node_id)?.drift === null ? "no target set" : recoveryReport(scope.row.target_node_id)?.drift + " node(s) off target" }}
                  </el-tag>
                </template>
              </div>
              <small v-if="readinessMessages[scope.row.id]" class="fleet-version-message">{{ readinessMessages[scope.row.id] }}</small>
              <small v-if="recoveryMessages[scope.row.id]" class="fleet-version-message">{{ recoveryMessages[scope.row.id] }}</small>
              <div class="fleet-version-control">
                <el-input
                  v-model="rollbackSnapshots[scope.row.id]"
                  size="small"
                  placeholder="/var/lib/etcd/backups/snapshot-pre-*.db"
                  style="width: min(300px, 100%)"
                />
                <el-button
                  size="small"
                  type="danger"
                  plain
                  :loading="rollbackBusy === scope.row.id"
                  @click="requestRollback(scope.row)"
                >Rollback</el-button>
              </div>
              <small class="readiness-blocked">
                Rollback restores the pre-upgrade etcd snapshot. Single control-plane only; approval required.
              </small>
              <small v-if="rollbackMessages[scope.row.id]" class="fleet-version-message">{{ rollbackMessages[scope.row.id] }}</small>
            </template>
          </el-table-column>
          <el-table-column prop="status" label="Workflow Status" min-width="130">
            <template #default="scope"><el-tag :type="statusType(scope.row.status)" size="small">{{ scope.row.status }}</el-tag></template>
          </el-table-column>
          <el-table-column label="CAPI Pivot / Ownership" min-width="260">
            <template #default="scope">
              <div class="fleet-node-main">
                <strong>{{ deploymentPhaseLabel(scope.row) }}</strong>
                <small>{{ deploymentOwnershipLabel(scope.row) }}</small>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="EPH Agent" min-width="140">
            <template #default="scope">{{ nodeLabel(scope.row.eph_node_id) }}</template>
          </el-table-column>
          <el-table-column label="Target Agent" min-width="140">
            <template #default="scope">{{ scope.row.target_node_id ? nodeLabel(scope.row.target_node_id) : "Pending handoff" }}</template>
          </el-table-column>
          <el-table-column label="Updated" min-width="170">
            <template #default="scope">{{ formatTime(scope.row.updated_at) }}</template>
          </el-table-column>
          <el-table-column label="Plan Hash" min-width="130">
            <template #default="scope"><code>{{ scope.row.plan_hash.slice(0, 14) }}</code></template>
          </el-table-column>
        </el-table>
        <section class="fleet-panel sriov-config-panel">
          <div class="fleet-panel-heading">
            <h3>Worker SR-IOV PCI Resource Configuration</h3>
            <span>DEVICE PLUGIN</span>
          </div>
          <div class="fleet-inventory-controls">
            <el-select
              v-model="selectedSriovDeploymentId"
              size="small"
              placeholder="Select target cluster deployment"
              style="width: min(360px, 100%)"
            >
              <el-option
                v-for="deployment in deployments"
                :key="deployment.id"
                :label="deployment.cluster_name + ' · ' + deployment.status"
                :value="deployment.id"
              />
            </el-select>
            <el-button
              size="small"
              type="primary"
              :loading="sriovSaving"
              :disabled="!selectedSriovDeploymentId"
              @click="saveSriovConfig"
            >Save to backend</el-button>
          </div>
          <SriovDevicePluginEditor
            ref="sriovEditor"
            v-model="sriovPools"
            :pci-addresses="sriovPciAddresses"
          />
          <el-alert
            v-if="sriovSaveMessage"
            :title="sriovSaveMessage"
            :type="sriovSaveMessage.startsWith('SR-IOV pool') ? 'success' : 'error'"
            :closable="false"
            show-icon
          />
          <p class="fleet-footnote">
            PCI addresses discovered in Agent hardware inventory are offered as selectable values. You can also enter a BDF manually.
            The backend validates the pool, ConfigMap identity, vendor, driver, RDMA flag and PCI ownership before saving.
          </p>
        </section>
        <p class="fleet-footnote">Supported selections: v1.33, v1.34, v1.35 and v1.36. Applying a selection creates an approval-gated Target Agent command; this integration is simulation-only and does not perform live Kubernetes, CAPI or kubeadm writes.</p>
      </section>
    </template>

    <template v-else-if="activeTab === 'terminal'">
      <section class="fleet-terminal">
        <div class="fleet-terminal-bar">
          <span>AGENT COMMAND HISTORY</span>
          <span>{{ commandCount }} TOTAL · PERSISTED POLL</span>
        </div>
        <div class="fleet-terminal-controls">
          <el-select :model-value="commandStatus" size="small" clearable placeholder="All statuses" @change="setCommandStatus">
            <el-option label="Queued" value="queued" />
            <el-option label="Running" value="running" />
            <el-option label="Succeeded" value="succeeded" />
            <el-option label="Failed" value="failed" />
            <el-option label="Waiting approval" value="waiting_approval" />
            <el-option label="Cancelled" value="cancelled" />
          </el-select>
          <span>Showing {{ commandCount ? commandOffset + 1 : 0 }}–{{ Math.min(commandOffset + commands.length, commandCount) }} of {{ commandCount }}</span>
          <el-button size="small" plain :disabled="commandOffset === 0 || refreshing" @click="changeCommandPage(-1)">Previous</el-button>
          <el-button size="small" plain :disabled="commandOffset + commandPageSize >= commandCount || refreshing" @click="changeCommandPage(1)">Next</el-button>
        </div>
        <section v-if="kubernetesRollbackCommands.length" class="fleet-upgrade-plans">
          <div class="fleet-panel-heading">
            <h3>Kubernetes rollback plans</h3>
            <span>{{ kubernetesRollbackCommands.length }} COMMANDS</span>
          </div>
          <article v-for="command in kubernetesRollbackCommands" :key="command.id" class="fleet-upgrade-plan">
            <div class="fleet-upgrade-plan-heading">
              <div>
                <strong>{{ String(command.payload.cluster_name ?? "Target cluster") }}</strong>
                <small>restore {{ String(command.payload.snapshot_path ?? "snapshot not set") }}</small>
              </div>
              <div class="fleet-upgrade-plan-tags">
                <el-tag :type="command.result?.rolled_back ? 'success' : 'warning'" size="small" effect="plain">
                  {{ command.result?.rolled_back ? "RESTORED" : "NOT RESTORED" }}
                </el-tag>
                <el-tag :type="statusType(command.status)" size="small">{{ command.status }}</el-tag>
              </div>
            </div>
            <ol v-if="rollbackSteps(command).length" class="fleet-upgrade-steps">
              <li v-for="step in rollbackSteps(command)" :key="String(step.sequence ?? step.name)">
                <span class="fleet-upgrade-step-sequence">{{ step.sequence }}</span>
                <div>
                  <strong>{{ String(step.name ?? "Rollback step").replaceAll("_", " ") }}</strong>
                  <small v-if="upgradeStepDetail(step)" class="fleet-upgrade-step-detail">{{ upgradeStepDetail(step) }}</small>
                </div>
                <el-tag :type="step.status === 'failed' ? 'danger' : step.status === 'succeeded' ? 'success' : 'info'" size="small">{{ upgradeStepStatus(step) }}</el-tag>
              </li>
            </ol>
            <div v-if="rollbackPreflight(command)" class="fleet-preflight">
              <div class="fleet-preflight-heading">
                <span>Rollback preflight</span>
                <el-tag :type="rollbackBlocked(command) ? 'danger' : 'success'" size="small">
                  {{ rollbackBlocked(command) ? "BLOCKED" : "PASSED" }}
                </el-tag>
              </div>
              <ul>
                <li v-for="check in rollbackPreflight(command)" :key="String(check.name)" :class="{ failed: check.ok !== true }">
                  <span class="fleet-preflight-mark">{{ check.ok === true ? "✓" : "✕" }}</span>
                  <code>{{ check.name }}</code>
                  <small>{{ String(check.detail ?? "") }}</small>
                </li>
              </ul>
            </div>
            <p v-if="command.result?.previous_member_dir" class="fleet-preflight-hint">
              previous etcd data kept at {{ command.result.previous_member_dir }}
            </p>
            <p v-if="command.status === 'failed'" class="fleet-upgrade-pending fleet-upgrade-failure">
              Rollback did not finish.
              <span v-if="getKubernetesUpgradeFailure(command.error)">{{ getKubernetesUpgradeFailure(command.error) }}</span>
              <span v-else>No structured failure detail was returned.</span>
            </p>
            <p v-else-if="command.status === 'waiting_approval'" class="fleet-upgrade-pending">
              Awaiting independent approval; the kubelet has not been stopped.
            </p>
          </article>
        </section>
        <section v-if="kubernetesUpgradeCommands.length" class="fleet-upgrade-plans">
          <div class="fleet-panel-heading">
            <h3>Kubernetes upgrade plans</h3>
            <span>{{ kubernetesUpgradeCommands.length }} COMMANDS</span>
          </div>
          <article v-for="command in kubernetesUpgradeCommands" :key="command.id" class="fleet-upgrade-plan">
            <div class="fleet-upgrade-plan-heading">
              <div>
                <strong>{{ String(command.payload.cluster_name ?? "Target cluster") }}</strong>
                <small>{{ String(command.payload.current_version ?? "unknown") }} → {{ String(command.payload.target_version ?? "unknown") }}</small>
              </div>
              <div class="fleet-upgrade-plan-tags">
                <el-tag :type="upgradeCommandMode(command) === 'execute' ? 'danger' : 'info'" size="small" effect="plain">
                  {{ upgradeCommandMode(command) === "execute" ? "LIVE EXECUTION" : "SIMULATION" }}
                </el-tag>
                <el-tag :type="statusType(command.status)" size="small">{{ command.status }}</el-tag>
              </div>
            </div>
            <ol v-if="kubernetesUpgradeSteps(command).length" class="fleet-upgrade-steps">
              <li v-for="step in kubernetesUpgradeSteps(command)" :key="String(step.sequence ?? step.name)">
                <span class="fleet-upgrade-step-sequence">{{ step.sequence }}</span>
                <div>
                  <strong>{{ String(step.name ?? "Upgrade step").replaceAll("_", " ") }}</strong>
                  <small>{{ String(step.description ?? "") }}</small>
                  <small v-if="upgradeStepDetail(step)" class="fleet-upgrade-step-detail">{{ upgradeStepDetail(step) }}</small>
                </div>
                <el-tag :type="step.status === 'failed' ? 'danger' : step.status === 'succeeded' ? 'success' : 'info'" size="small">{{ upgradeStepStatus(step) }}</el-tag>
              </li>
            </ol>
            <div v-if="upgradePreflight(command)" class="fleet-preflight">
              <div class="fleet-preflight-heading">
                <span>Preflight checks</span>
                <el-tag :type="upgradePreflight(command)?.blocked ? 'danger' : 'success'" size="small">
                  {{ upgradePreflight(command)?.blocked ? "BLOCKED" : "PASSED" }}
                </el-tag>
              </div>
              <ul v-if="upgradePreflight(command)?.checks.length">
                <li
                  v-for="check in upgradePreflight(command)?.checks"
                  :key="check.name"
                  :class="{ failed: !check.ok }"
                >
                  <span class="fleet-preflight-mark">{{ check.ok ? "✓" : "✕" }}</span>
                  <code>{{ check.name }}</code>
                  <small>{{ check.detail }}</small>
                </li>
              </ul>
              <p v-else class="fleet-preflight-hint">The Agent reported a preflight without any checks.</p>
              <p v-if="upgradePreflight(command)?.blocked" class="fleet-preflight-hint">
                {{ upgradePreflightFailedCount(command) }} check(s) failed · nothing was drained or upgraded
              </p>
            </div>
            <div v-if="upgradeEtcdHealth(command).length" class="fleet-preflight">
              <div class="fleet-preflight-heading">
                <span>etcd health during the roll</span>
              </div>
              <ul>
                <li
                  v-for="check in upgradeEtcdHealth(command)"
                  :key="check.name"
                  :class="{ failed: !check.ok }"
                >
                  <span class="fleet-preflight-mark">{{ check.ok ? "✓" : "✕" }}</span>
                  <code>{{ check.name }}</code>
                  <small>{{ check.detail }}</small>
                </li>
              </ul>
            </div>
            <div v-if="recoveryReport(command.node_id)" class="fleet-preflight">
              <div class="fleet-preflight-heading">
                <span>Recovery state · {{ recoveryReport(command.node_id)?.targetVersion || "target not set" }}</span>
                <el-tag :type="recoveryReport(command.node_id)?.drift ? 'warning' : 'success'" size="small">
                  {{ recoveryReport(command.node_id)?.drift === null ? "TARGET NOT SET" : recoveryReport(command.node_id)?.drift + " OFF TARGET" }}
                </el-tag>
              </div>
              <ul>
                <li
                  v-for="node in recoveryReport(command.node_id)?.nodes"
                  :key="'recovery-' + node.name"
                  :class="{ failed: !node.onTarget }"
                >
                  <span class="fleet-preflight-mark">{{ node.onTarget ? "✓" : "✕" }}</span>
                  <code>{{ node.name }} <small>{{ node.role }}</small></code>
                  <small>{{ node.version || "version unreadable" }}</small>
                </li>
              </ul>
              <p class="fleet-preflight-hint">
                etcd: {{ recoveryReport(command.node_id)?.etcdHealthy === null ? "external etcd (no stacked members)" : recoveryReport(command.node_id)?.etcdHealthy ? "healthy" : "degraded or unreadable" }}
                · {{ recoveryReport(command.node_id)?.etcdMembers.length }} member(s)
                · {{ recoveryReport(command.node_id)?.snapshots.length }} snapshot(s)
              </p>
              <p
                v-for="warning in recoveryReport(command.node_id)?.warnings || []"
                :key="'recovery-warning-' + warning"
                class="fleet-preflight-hint"
              >{{ warning }}</p>
              <p
                v-for="snapshot in recoveryReport(command.node_id)?.snapshots || []"
                :key="'recovery-snapshot-' + snapshot.name"
                class="fleet-preflight-hint"
              >{{ snapshot.name }} · {{ snapshot.taken }}</p>
            </div>
            <p v-else-if="command.status === 'failed'" class="fleet-upgrade-pending fleet-upgrade-failure">
              Upgrade simulation failed. Review the Agent error before taking further action.
              <span v-if="getKubernetesUpgradeFailure(command.error)">{{ getKubernetesUpgradeFailure(command.error) }}</span>
              <span v-else>No structured failure detail was returned.</span>
            </p>
            <p v-else class="fleet-upgrade-pending">
              {{ command.status === "waiting_approval" ? "Awaiting independent approval; no upgrade step has executed." : "The Agent has not returned an upgrade plan yet." }}
            </p>
            <p v-if="upgradeCommandMode(command) === 'execute'" class="fleet-upgrade-safety fleet-upgrade-live">
              Live execution · steps above are the real commands and their exit codes
            </p>
            <p v-else class="fleet-upgrade-safety">Simulation-only · no live Kubernetes or kubeadm changes</p>
          </article>
        </section>
        <div class="fleet-terminal-body">
          <div v-for="command in recentCommands" :key="command.id" class="fleet-terminal-line">
            <span class="terminal-time">{{ formatTime(command.finished_at ?? command.started_at ?? command.created_at) }}</span>
            <span class="terminal-node">{{ nodeLabel(command.node_id) }}</span>
            <span class="terminal-command">{{ command.command_type }}</span>
            <el-tag :type="statusType(command.status)" size="small">{{ command.status }}</el-tag>
            <pre>{{ commandDetail(command) }}</pre>
          </div>
          <el-empty v-if="!recentCommands.length" description="No persisted Agent commands yet" :image-size="56" />
        </div>
      </section>
    </template>

    <template v-else-if="activeTab === 'operations'">
      <InfrastructureAgentsView />
    </template>

    <el-dialog v-model="bmhDialogVisible" title="Ingest BMH inspection" width="720px">
      <p class="task-note">
        Paste the output of <code>kubectl get bmh -A -o json</code> from the EPH node, or the
        <code>metal3.host.inspect</code> command output. Hosts match by BMC address, then BMH name,
        then system UUID. PCI addresses are adopted only when the machine has no agent-observed NIC
        inventory.
      </p>
      <el-input
        v-model="bmhDocumentText"
        type="textarea"
        :rows="12"
        placeholder='{"items": [...]}'
      />
      <p v-if="bmhIngestMessage" class="task-note">{{ bmhIngestMessage }}</p>
      <ul v-if="bmhIngestResult" class="pxe-boot-results">
        <li v-for="item in bmhIngestResult.updated" :key="item.machine_id">
          {{ item.name }}: {{ item.pci_inventory_adopted ? "PCI adopted" : "BMH recorded" }}
        </li>
        <li v-for="name in bmhIngestResult.unmatched" :key="name">{{ name }}: unmatched</li>
      </ul>
      <template #footer>
        <el-button size="small" @click="bmhDialogVisible = false">Close</el-button>
        <el-button size="small" type="primary" :loading="bmhIngestBusy" @click="ingestBmhInventory">
          Ingest
        </el-button>
      </template>
    </el-dialog>
  </section>
</template>


<style scoped>
.metal3-fleet { display: grid; gap: 18px; color: #dce8f5; }
.fleet-heading { display: flex; align-items: flex-end; justify-content: space-between; gap: 18px; }
.fleet-heading h2 { margin: 4px 0 6px; font-size: 26px; color: #f1f6fc; }
.fleet-heading p:not(.eyebrow) { margin: 0; color: #8fa5bc; font-size: 13px; }
.eyebrow { margin: 0; color: #52c8d9; font-size: 10px; font-weight: 800; letter-spacing: .16em; }
.fleet-scope { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.fleet-tabs { display: flex; align-items: center; gap: 4px; border-bottom: 1px solid rgba(126, 164, 198, .2); flex-wrap: wrap; }
.fleet-tabs button { padding: 12px 14px; border: 0; border-bottom: 2px solid transparent; background: transparent; color: #8197ae; font-size: 12px; cursor: pointer; }
.fleet-tabs button.active { border-bottom-color: #4fc5d7; color: #e6f4ff; background: rgba(47, 157, 184, .08); }
.fleet-live { margin-left: auto; display: inline-flex; align-items: center; gap: 7px; color: #7e9bb4; font-size: 10px; letter-spacing: .1em; }
.fleet-live i { width: 7px; height: 7px; border-radius: 50%; background: #39c99a; box-shadow: 0 0 10px rgba(57, 201, 154, .7); }
.fleet-metrics { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
.fleet-metrics article, .fleet-panel, .eph-node-card { border: 1px solid rgba(112, 151, 187, .2); border-radius: 12px; background: linear-gradient(145deg, rgba(20, 39, 59, .92), rgba(12, 26, 42, .96)); }
.fleet-metrics article { padding: 16px 18px; display: grid; gap: 8px; }
.fleet-metrics article span { color: #8ea5bd; font-size: 11px; }
.fleet-metrics article strong { color: #f0f7ff; font-size: 26px; font-weight: 700; }
.fleet-metrics article small { color: #637f99; font-size: 10px; }
.fleet-two-column { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.fleet-panel { padding: 16px; min-width: 0; }
.fleet-inventory-controls { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }
.fleet-inventory-controls .el-input { width: min(320px, 100%); }
.fleet-inventory-controls .el-select { width: 160px; }
.fleet-panel-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 12px; }
.fleet-panel-heading h3 { margin: 0; color: #e4effa; font-size: 14px; }
.fleet-panel-heading span { color: #6f8ba5; font-size: 9px; letter-spacing: .12em; }
.fleet-version-control { display: flex; align-items: center; gap: 7px; }
.fleet-version-current, .fleet-version-message { display: block; margin-top: 4px; color: #7791aa; font-size: 10px; line-height: 1.4; }
.fleet-node-row, .fleet-deployment-row { display: flex; align-items: center; gap: 12px; padding: 11px 2px; border-top: 1px solid rgba(119, 151, 180, .12); }
.fleet-node-icon { display: grid; place-items: center; width: 34px; height: 34px; border-radius: 9px; background: rgba(52, 161, 188, .13); color: #55c7d8; }
.fleet-node-glyph { font-size: 18px; line-height: 1; }
.fleet-node-main, .fleet-deployment-row > div { min-width: 0; display: grid; gap: 4px; flex: 1; }
.fleet-node-main strong, .fleet-deployment-row strong { color: #dce9f6; font-size: 12px; }
.fleet-node-main small, .fleet-deployment-row small { color: #7791aa; font-size: 10px; }
.identity-conflict-count { display: block; margin-top: 4px; color: var(--el-color-danger); font-size: 11px; }
.fleet-footnote { color: #708aa2; font-size: 10px; line-height: 1.6; }
.eph-node-card { padding: 18px; margin-bottom: 12px; }
.eph-node-header { display: flex; justify-content: space-between; align-items: flex-start; }
.eph-node-header h3 { margin: 5px 0 0; font-size: 18px; color: #eef6ff; }
.eph-node-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin: 18px 0; }
.eph-node-grid div { display: grid; gap: 6px; }
.eph-node-grid span { color: #7089a2; font-size: 10px; }
.eph-node-grid strong { color: #cbdbea; font-size: 11px; font-weight: 600; overflow-wrap: anywhere; }
.fleet-capabilities { display: flex; flex-wrap: wrap; gap: 6px; }
.fleet-capabilities span { padding: 5px 8px; border-radius: 6px; border: 1px solid rgba(70, 185, 204, .22); background: rgba(40, 139, 159, .1); color: #7bc9d4; font-size: 10px; }
.fleet-terminal { overflow: hidden; border: 1px solid rgba(82, 137, 170, .25); border-radius: 12px; background: #07111d; }
.fleet-terminal-bar { display: flex; justify-content: space-between; gap: 12px; padding: 12px 16px; border-bottom: 1px solid rgba(93, 134, 164, .2); color: #73a2ba; font-size: 9px; letter-spacing: .12em; }
.fleet-terminal-controls { display: flex; align-items: center; justify-content: flex-end; flex-wrap: wrap; gap: 8px; padding: 10px 14px; border-bottom: 1px solid rgba(93, 134, 164, .16); color: #7e9bb4; font-size: 10px; }
.fleet-terminal-controls .el-select { width: 170px; }
.fleet-upgrade-plans { margin: 12px 0 16px; padding: 14px; border: 1px solid rgba(112, 151, 187, .2); border-radius: 10px; background: rgba(14, 31, 49, .72); }
.fleet-upgrade-plan { padding: 12px 0; border-top: 1px solid rgba(119, 151, 180, .12); }
.fleet-upgrade-plan-heading { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
.fleet-upgrade-plan-heading > div { display: grid; gap: 4px; }
.fleet-upgrade-plan-heading strong, .fleet-upgrade-steps strong { color: #dce9f6; font-size: 12px; }
.fleet-upgrade-plan-heading small, .fleet-upgrade-steps small { color: #8198af; font-size: 10px; }
.fleet-upgrade-steps { display: grid; gap: 8px; margin: 12px 0; padding: 0; list-style: none; }
.fleet-upgrade-steps li { display: flex; align-items: center; gap: 10px; }
.fleet-upgrade-steps li > div { display: grid; gap: 3px; flex: 1; }
.fleet-upgrade-step-sequence { display: grid; place-items: center; width: 22px; height: 22px; border-radius: 50%; background: rgba(52, 161, 188, .13); color: #55c7d8; font-size: 10px; }
.fleet-upgrade-pending, .fleet-upgrade-safety { color: #8198af; font-size: 10px; }\n.fleet-upgrade-failure { display: grid; gap: 5px; color: #e5a0a0; }\n.fleet-upgrade-failure span { color: #c58f8f; overflow-wrap: anywhere; }
.fleet-upgrade-safety { margin: 8px 0 0; color: #d3a66b; }
.fleet-terminal-body { max-height: 600px; overflow: auto; padding: 12px 16px; }
.fleet-terminal-line { display: grid; grid-template-columns: 150px 110px minmax(140px, 1fr) auto; align-items: center; gap: 10px; padding: 8px 0; border-bottom: 1px solid rgba(80, 122, 150, .12); }
.terminal-time, .terminal-node { color: #6f8da5; font-size: 10px; }
.terminal-command { color: #8ed7d2; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; }
.fleet-terminal-line pre { grid-column: 1 / -1; margin: 0; max-height: 110px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; color: #91a8ba; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 10px; line-height: 1.5; }
@media (max-width: 1000px) {
  .fleet-heading { align-items: flex-start; flex-direction: column; }
  .fleet-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .eph-node-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 700px) {
  .fleet-two-column { grid-template-columns: 1fr; }
  .fleet-terminal-line { grid-template-columns: 1fr auto; }
  .terminal-time, .terminal-node, .terminal-command { grid-column: 1 / -1; }
}
</style>
