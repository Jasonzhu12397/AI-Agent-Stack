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
  formatResetAt,
  formatTokens,
  isPaid,
  mapQuotaError,
  orderStatusLabel,
  pageLabels,
  quotaMessage,
  quotaState,
  remainingQuota,
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

const dailyState = computed(() =>
  usage.value ? quotaState(usage.value.daily_used, usage.value.daily_limit) : "ok",
);
const monthlyState = computed(() =>
  usage.value ? quotaState(usage.value.monthly_used, usage.value.monthly_limit) : "ok",
);
const dailyRemaining = computed(() =>
  usage.value
    ? remainingQuota(usage.value.daily_used, usage.value.daily_limit, usage.value.remaining_daily)
    : 0,
);
const monthlyRemaining = computed(() =>
  usage.value
    ? remainingQuota(usage.value.monthly_used, usage.value.monthly_limit, usage.value.remaining_monthly)
    : 0,
);
const dailyResetAt = computed(() => usage.value?.reset_at ?? null);
const monthlyResetAt = computed(() => usage.value?.monthly_reset_at ?? null);

const quotaBlocked = computed(
  () => dailyState.value === "exhausted" || monthlyState.value === "exhausted",
);
const blockedMessage = computed(() => {
  if (!usage.value) return "";
  if (dailyState.value === "exhausted") {
    return mapQuotaError("TENANT_TOKEN_BUDGET_EXCEEDED", "daily", dailyResetAt.value);
  }
  return mapQuotaError("TENANT_TOKEN_BUDGET_EXCEEDED", "monthly", monthlyResetAt.value);
});

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

    <el-alert
      v-if="quotaBlocked"
      class="settings-quota-alert"
      type="error"
      :title="t('Quota exhausted: requests that consume quota are blocked.')"
      :description="blockedMessage"
      show-icon
    />

    <el-card v-if="usage" class="settings-card" shadow="never">
      <template #header>{{ t("Usage values are authoritative") }}</template>
      <div class="settings-usage-row">
        <div :class="['settings-quota-cell', `settings-quota-cell--${dailyState}`]">
          <p class="settings-usage-label">
            {{ t("Daily quota") }}
            <el-tag
              v-if="dailyState !== 'ok'"
              :type="dailyState === 'exhausted' ? 'danger' : 'warning'"
              effect="plain"
            >
              {{
                quotaMessage(dailyState, "daily", {
                  percent: usagePercent(usage.daily_used, usage.daily_limit),
                  resetAt: dailyResetAt,
                })
              }}
            </el-tag>
          </p>
          <p>
            {{ formatTokens(usage.daily_used) }} / {{ formatTokens(usage.daily_limit) }} tokens
            · {{ t("Remaining") }} {{ formatTokens(dailyRemaining) }}
          </p>
          <el-progress
            :percentage="usagePercent(usage.daily_used, usage.daily_limit)"
            :status="dailyState === 'exhausted' ? 'exception' : dailyState === 'warning' ? 'warning' : undefined"
            :stroke-width="10"
          />
          <p class="muted">{{ t("Resets at") }}: {{ formatResetAt(dailyResetAt) }}</p>
        </div>
        <div :class="['settings-quota-cell', `settings-quota-cell--${monthlyState}`]">
          <p class="settings-usage-label">
            {{ t("Monthly quota") }}
            <el-tag
              v-if="monthlyState !== 'ok'"
              :type="monthlyState === 'exhausted' ? 'danger' : 'warning'"
              effect="plain"
            >
              {{
                quotaMessage(monthlyState, "monthly", {
                  percent: usagePercent(usage.monthly_used, usage.monthly_limit),
                  resetAt: monthlyResetAt,
                })
              }}
            </el-tag>
          </p>
          <p>
            {{ formatTokens(usage.monthly_used) }} / {{ formatTokens(usage.monthly_limit) }} tokens
            · {{ t("Remaining") }} {{ formatTokens(monthlyRemaining) }}
          </p>
          <el-progress
            :percentage="usagePercent(usage.monthly_used, usage.monthly_limit)"
            :status="monthlyState === 'exhausted' ? 'exception' : monthlyState === 'warning' ? 'warning' : undefined"
            :stroke-width="10"
          />
          <p class="muted">{{ t("Resets at") }}: {{ formatResetAt(monthlyResetAt) }}</p>
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
