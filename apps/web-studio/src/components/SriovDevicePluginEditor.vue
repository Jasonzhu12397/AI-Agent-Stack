<script setup lang="ts">
import { computed, ref, watch } from "vue";

type SriovResource = {
  resourceName: string;
  resourcePrefix: string;
  isRdma: boolean;
  selectors: {
    vendors: string[];
    drivers: string[];
    rootDevices: string[];
    isRdma: boolean;
  };
};
type SriovPool = {
  node_selectors: Record<string, string>;
  tolerations: Array<{ effect: string; key: string; operator: string; value?: string }>;
  sriov_network_device_plugin_configmap_name: string;
  sriov_network_device_plugin_configmap_data: { resourceList: SriovResource[] };
};
type SriovConfig = Record<string, SriovPool>;

const props = defineProps<{ modelValue: SriovConfig; pciAddresses?: string[] }>();
const emit = defineEmits<{ "update:modelValue": [value: SriovConfig] }>();
const poolNames = computed(() => Object.keys(props.modelValue));
const selectedPool = ref("");
const error = ref("");
const selectedPoolConfig = computed(() => props.modelValue[selectedPool.value]);
const selectedNodeSelector = computed(
  () => Object.entries(selectedPoolConfig.value?.node_selectors ?? {})[0] ?? ["", ""],
);
const selectedToleration = computed(
  () => selectedPoolConfig.value?.tolerations?.[0] ?? {
    effect: "NoSchedule",
    key: "",
    operator: "Equal",
    value: "",
  },
);

function clone(value: SriovConfig): SriovConfig {
  return JSON.parse(JSON.stringify(value)) as SriovConfig;
}
function update(mutator: (draft: SriovConfig) => void): void {
  const draft = clone(props.modelValue);
  mutator(draft);
  emit("update:modelValue", draft);
}
function ensureSelection(): void {
  if (!poolNames.value.includes(selectedPool.value)) selectedPool.value = poolNames.value[0] ?? "";
}
watch(poolNames, ensureSelection, { immediate: true });

function updateConfigMapName(value: string): void {
  const name = selectedPool.value;
  if (!name) return;
  update((draft) => { draft[name].sriov_network_device_plugin_configmap_name = value; });
}
function updateNodeSelectorValue(value: string): void {
  const name = selectedPool.value;
  const key = selectedNodeSelector.value[0];
  if (!name || !key) return;
  update((draft) => { draft[name].node_selectors[key] = value; });
}
function updateToleration(field: "key" | "value" | "effect", value: string): void {
  const name = selectedPool.value;
  if (!name) return;
  update((draft) => {
    const current = draft[name].tolerations[0] ?? {
      effect: "NoSchedule",
      key: "",
      operator: "Equal",
      value: "",
    };
    draft[name].tolerations[0] = { ...current, [field]: value };
  });
}
function updateResourceField(
  index: number,
  field: "resourceName" | "resourcePrefix",
  value: string,
): void {
  const name = selectedPool.value;
  if (!name) return;
  update((draft) => {
    draft[name].sriov_network_device_plugin_configmap_data.resourceList[index][field] = value;
  });
}
function addPool(): void {
  let index = 4;
  while (props.modelValue["pool" + index]) index += 1;
  const name = "pool" + index;
  update((draft) => {
    draft[name] = {
      node_selectors: { type: "high-throughput" },
      tolerations: [{ effect: "NoSchedule", key: "high-throughput", operator: "Equal", value: "true" }],
      sriov_network_device_plugin_configmap_name: "sriov-dp-configmap",
      sriov_network_device_plugin_configmap_data: { resourceList: [] },
    };
  });
  selectedPool.value = name;
}
function duplicatePool(): void {
  const source = selectedPool.value;
  if (!source) return;
  let index = 4;
  while (props.modelValue["pool" + index]) index += 1;
  const name = "pool" + index;
  update((draft) => { draft[name] = JSON.parse(JSON.stringify(draft[source])) as SriovPool; });
  selectedPool.value = name;
}
function removePool(name: string): void {
  update((draft) => { delete draft[name]; });
  selectedPool.value = Object.keys(props.modelValue).find((item) => item !== name) ?? "";
}
function addResource(): void {
  const name = selectedPool.value;
  if (!name) return;
  update((draft) => {
    draft[name].sriov_network_device_plugin_configmap_data.resourceList.push({
      resourceName: "mlnx_dpdk_" + (draft[name].sriov_network_device_plugin_configmap_data.resourceList.length ? "right" : "left"),
      resourcePrefix: "mellanox.com",
      isRdma: true,
      selectors: { vendors: ["15b3"], drivers: ["mlx5_core"], rootDevices: [], isRdma: true },
    });
  });
}
function removeResource(index: number): void {
  const name = selectedPool.value;
  update((draft) => { draft[name].sriov_network_device_plugin_configmap_data.resourceList.splice(index, 1); });
}
function updateRootDevices(index: number, value: string[]): void {
  const name = selectedPool.value;
  update((draft) => { draft[name].sriov_network_device_plugin_configmap_data.resourceList[index].selectors.rootDevices = value; });
}
function setRdma(index: number, value: boolean): void {
  const name = selectedPool.value;
  update((draft) => {
    const resource = draft[name].sriov_network_device_plugin_configmap_data.resourceList[index];
    resource.isRdma = value;
    resource.selectors.isRdma = value;
  });
}
function updateList(index: number, field: "vendors" | "drivers", value: string): void {
  const name = selectedPool.value;
  update((draft) => {
    draft[name].sriov_network_device_plugin_configmap_data.resourceList[index].selectors[field] =
      value.split(",").map((item) => item.trim()).filter(Boolean);
  });
}
function validate(): boolean {
  error.value = "";
  const resources = Object.values(props.modelValue).flatMap((pool) => pool.sriov_network_device_plugin_configmap_data.resourceList);
  if (!Object.keys(props.modelValue).length || !resources.length) {
    error.value = "Add at least one resource pool and one SR-IOV resource.";
    return false;
  }
  const names = new Set<string>();
  const bdfs = new Set<string>();
  for (const resource of resources) {
    if (!resource.resourceName.trim() || !resource.resourcePrefix.trim()) {
      error.value = "Resource name and prefix are required.";
      return false;
    }
    if (!resource.selectors.rootDevices.length) {
      error.value = "Select at least one PCI bus address for every resource.";
      return false;
    }
    for (const bdf of resource.selectors.rootDevices) {
      if (!/^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]$/.test(bdf)) {
        error.value = "Invalid PCI BDF: " + bdf;
        return false;
      }
      if (bdfs.has(bdf.toLowerCase())) {
        error.value = "A PCI device cannot be assigned to multiple resources: " + bdf;
        return false;
      }
      bdfs.add(bdf.toLowerCase());
    }
    const key = resource.resourcePrefix + "/" + resource.resourceName;
    if (names.has(key)) {
      error.value = "Duplicate extended resource name: " + key;
      return false;
    }
    names.add(key);
  }
  return true;
}
defineExpose({ validate });
</script>

<template>
  <section class="sriov-editor">
    <div class="editor-heading">
      <div>
        <h3>SR-IOV Network Device Plugin</h3>
        <p>Assign PCI BDF addresses to extended resources and bind each pool to its node selector.</p>
      </div>
      <div class="editor-actions">
        <el-button size="small" plain :disabled="!selectedPool" @click="duplicatePool">Duplicate selected pool</el-button>
        <el-button size="small" type="primary" plain @click="addPool">Add empty pool</el-button>
      </div>
    </div>
    <el-alert
      title="PCI addresses are applied to the selected Target Cluster. Confirm each BDF against the machine's PCI inventory before deployment."
      type="info"
      :closable="false"
      show-icon
    />
    <div class="pool-layout">
      <aside class="pool-list">
        <button
          v-for="name in poolNames"
          :key="name"
          type="button"
          :class="{ active: selectedPool === name }"
          @click="selectedPool = name"
        >{{ name }}</button>
        <el-empty v-if="!poolNames.length" description="No pools configured" :image-size="48" />
      </aside>
      <div v-if="selectedPool" class="pool-editor">
        <div class="pool-title">
          <strong>{{ selectedPool }}</strong>
          <el-button text type="danger" size="small" @click="removePool(selectedPool)">Remove pool</el-button>
        </div>
        <el-form label-position="top" size="small">
          <div class="form-grid">
            <el-form-item label="Node selector key">
              <el-input
                :model-value="selectedNodeSelector[0]"
                disabled
              />
            </el-form-item>
            <el-form-item label="Node selector value">
              <el-input :model-value="selectedNodeSelector[1]" placeholder="high-throughput" @update:model-value="updateNodeSelectorValue" />
            </el-form-item>
            <el-form-item label="ConfigMap name">
              <el-input :model-value="props.modelValue[selectedPool].sriov_network_device_plugin_configmap_name" @update:model-value="updateConfigMapName" />
            </el-form-item>
            <el-form-item label="Toleration key">
              <el-input :model-value="selectedToleration.key" @update:model-value="(value: string) => updateToleration('key', value)" />
            </el-form-item>
            <el-form-item label="Toleration value">
              <el-input :model-value="selectedToleration.value ?? ''" @update:model-value="(value: string) => updateToleration('value', value)" />
            </el-form-item>
            <el-form-item label="Toleration effect">
              <el-select :model-value="selectedToleration.effect" @update:model-value="(value: string) => updateToleration('effect', value)">
                <el-option label="NoSchedule" value="NoSchedule" />
                <el-option label="PreferNoSchedule" value="PreferNoSchedule" />
                <el-option label="NoExecute" value="NoExecute" />
              </el-select>
            </el-form-item>
          </div>
        </el-form>
        <div class="resource-heading">
          <strong>Resource list</strong>
          <el-button size="small" plain @click="addResource">Add SR-IOV resource</el-button>
        </div>
        <article
          v-for="(resource, index) in props.modelValue[selectedPool].sriov_network_device_plugin_configmap_data.resourceList"
          :key="selectedPool + '-' + index"
          class="resource-card"
        >
          <div class="resource-heading">
            <strong>Resource {{ index + 1 }}</strong>
            <el-button text type="danger" size="small" @click="removeResource(index)">Remove</el-button>
          </div>
          <div class="form-grid">
            <el-form-item label="resourceName">
              <el-input :model-value="resource.resourceName" placeholder="mlnx_dpdk_left" @update:model-value="(value: string) => updateResourceField(index, 'resourceName', value)" />
            </el-form-item>
            <el-form-item label="resourcePrefix">
              <el-input :model-value="resource.resourcePrefix" placeholder="mellanox.com" @update:model-value="(value: string) => updateResourceField(index, 'resourcePrefix', value)" />
            </el-form-item>
            <el-form-item label="Vendors (comma separated)">
              <el-input :model-value="resource.selectors.vendors.join(',')" @change="(value: string) => updateList(index, 'vendors', value)" />
            </el-form-item>
            <el-form-item label="Drivers (comma separated)">
              <el-input :model-value="resource.selectors.drivers.join(',')" @change="(value: string) => updateList(index, 'drivers', value)" />
            </el-form-item>
          </div>
          <el-form-item label="PCI Bus Addresses (rootDevices)">
            <el-select
              :model-value="resource.selectors.rootDevices"
              multiple
              filterable
              allow-create
              default-first-option
              clearable
              placeholder="Select discovered PCI BDFs or enter one, e.g. 0000:2f:00.0"
              style="width: 100%"
              @change="(value: string[]) => updateRootDevices(index, value)"
            >
              <el-option v-for="address in props.pciAddresses ?? []" :key="address" :label="address" :value="address" />
            </el-select>
          </el-form-item>
          <el-checkbox :model-value="resource.isRdma" @change="(value: string | number | boolean) => setRdma(index, Boolean(value))">RDMA enabled</el-checkbox>
        </article>
      </div>
    </div>
    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
  </section>
</template>

<style scoped>
.sriov-editor { display: grid; gap: 14px; margin-top: 18px; padding: 16px; border: 1px solid rgba(112,151,187,.2); border-radius: 12px; background: rgba(8,22,36,.5); }
.editor-heading,.pool-title,.resource-heading { display:flex; align-items:center; justify-content:space-between; gap:12px; }
.editor-actions { display:flex; flex-wrap:wrap; gap:6px; }
.editor-heading h3 { margin:0; color:#e4effa; font-size:14px; }
.editor-heading p { margin:5px 0 0; color:#8299b0; font-size:11px; }
.pool-layout { display:grid; grid-template-columns:130px minmax(0,1fr); gap:14px; }
.pool-list { display:grid; align-content:start; gap:6px; }
.pool-list button { padding:9px 10px; border:1px solid rgba(112,151,187,.2); border-radius:7px; background:transparent; color:#91a8bf; text-align:left; cursor:pointer; }
.pool-list button.active { border-color:#4fc5d7; color:#e4f8ff; background:rgba(47,157,184,.1); }
.pool-title strong,.resource-heading strong { color:#dce9f6; font-size:12px; }
.form-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:0 12px; }
.pool-editor :deep(.el-form-item__label) { color:#8fa5bc; }
.resource-card { margin-top:12px; padding:12px; border:1px solid rgba(112,151,187,.18); border-radius:9px; }
@media(max-width:720px) { .pool-layout,.form-grid { grid-template-columns:1fr; } .pool-list { grid-template-columns:repeat(3,minmax(0,1fr)); } }
</style>
