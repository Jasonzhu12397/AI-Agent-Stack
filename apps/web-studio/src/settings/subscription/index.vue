<script setup lang="ts">
import { computed, onMounted } from "vue";
import { t } from "../../i18n";
import { ApiError } from "../../services/http";
import SettingsPageShell from "../shared/SettingsPageShell.vue";
import { useSettingsResource } from "../shared/useSettingsResource";
import { activeTenantId, ensureTenantContext, tenantContextError } from "../shared/tenantContext";
import { fetchSubscriptionBundle } from "./api";
import {
  describeError,
  formatAmount,
  formatTokens,
  isPaid,
  orderStatusLabel,
  pageLabels,
  usagePercent,
} from "./logic";

const resource = useSettingsResource(async () => {
  await ensureTenantContext();
  if (!activeTenantId.value) {
    throw new ApiError(400, "TENANT_CONTEXT_UNAVAILABLE", tenantContextError.value || "No tenant is available.");
  }
  return fetchSubscriptionBundle(activeTenantId.value);
});

const usage = computed(() => resource.data.value?.usage ?? null);
const orders = computed(() => resource.data.value?.orders ?? []);
const subscription = computed(() => resource.data.value?.subscription ?? null);

onMounted(() => void resource.reload());
void pageLabels;
</script>

<template>
  <SettingsPageShell
    title="Subscription & Usage Management"
    description="Subscription lifecycle, authoritative token consumption and order history. Values are read from the billing and quota ledgers, never estimated here."
    :state="resource.state.value"
    :error-code="resource.errorCode.value"
    :error-message="resource.errorMessage.value"
    empty-message="No subscription is active and no usage has been recorded yet."
    denied-message="Only tenant members can read billing data."
    @retry="resource.reload()"
  >
    <el-card class="settings-card" shadow="never">
      <template #header>{{ t("Subscription & Usage Management") }}</template>
      <div v-if="subscription">
        <p>
          <strong>{{ subscription.plan_name }}</strong>
          <el-tag class="settings-tag" effect="plain" :type="subscription.status === 'active' ? 'success' : 'warning'">
            {{ subscription.status }}
          </el-tag>
          <el-tag v-if="subscription.cancel_at_period_end" class="settings-tag" type="info" effect="plain">
            cancels at period end
          </el-tag>
        </p>
        <p class="muted">{{ t("Billing period") }}: {{ subscription.current_period_start || "—" }} → {{ subscription.current_period_end || "—" }}</p>
        <p class="muted">Renewal: {{ subscription.renewal_mode }}</p>
      </div>
      <el-empty v-else description="No subscription is active. Nothing is billed for this tenant." :image-size="64" />
    </el-card>

    <el-card v-if="usage" class="settings-card" shadow="never">
      <template #header>{{ t("Usage values are authoritative") }}</template>
      <div class="settings-usage-row">
        <div>
          <p class="settings-usage-label">{{ t("Daily quota") }}</p>
          <p>{{ formatTokens(usage.daily_used) }} / {{ formatTokens(usage.daily_limit) }} tokens</p>
          <el-progress :percentage="usagePercent(usage.daily_used, usage.daily_limit)" :stroke-width="10" />
        </div>
        <div>
          <p class="settings-usage-label">{{ t("Monthly quota") }}</p>
          <p>{{ formatTokens(usage.monthly_used) }} / {{ formatTokens(usage.monthly_limit) }} tokens</p>
          <el-progress :percentage="usagePercent(usage.monthly_used, usage.monthly_limit)" :stroke-width="10" />
        </div>
      </div>
      <p v-if="usage.period_start || usage.period_end" class="muted">
        {{ t("Billing period") }}: {{ usage.period_start || "—" }} → {{ usage.period_end || "—" }}
      </p>
    </el-card>

    <el-card class="settings-card" shadow="never">
      <template #header>{{ t("Usage history") }}</template>
      <el-table v-if="usage && usage.history.length > 0" :data="usage.history" size="small">
        <el-table-column prop="period" label="Period" />
        <el-table-column label="Consumed tokens">
          <template #default="{ row }">{{ formatTokens(row.consumed_tokens) }}</template>
        </el-table-column>
        <el-table-column prop="source" label="Source" />
      </el-table>
      <el-empty v-else description="No usage history yet." :image-size="64" />
    </el-card>

    <el-card class="settings-card" shadow="never">
      <template #header>{{ t("Orders") }}</template>
      <el-table v-if="orders.length > 0" :data="orders" size="small">
        <el-table-column prop="id" label="Order" width="240" />
        <el-table-column prop="plan_code" label="Plan" />
        <el-table-column label="Amount">
          <template #default="{ row }">{{ formatAmount(row.amount_cents, row.currency) }}</template>
        </el-table-column>
        <el-table-column prop="channel" label="Channel" />
        <el-table-column label="Status">
          <template #default="{ row }">
            <el-tag :type="isPaid(row) ? 'success' : 'info'" effect="plain">{{ t(orderStatusLabel(row.status)) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="created_at" label="Created" />
      </el-table>
      <el-empty v-else description="No orders yet." :image-size="64" />
    </el-card>

    <p class="muted">{{ describeError("PAYMENT_PROVIDER_NOT_CONFIGURED") }}</p>
  </SettingsPageShell>
</template>
