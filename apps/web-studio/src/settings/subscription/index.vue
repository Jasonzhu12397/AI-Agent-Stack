<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { t } from "../../i18n";
import { ApiError } from "../../services/http";
import SettingsPageShell from "../shared/SettingsPageShell.vue";
import { useSettingsResource } from "../shared/useSettingsResource";
import { activeTenantId, ensureTenantContext, tenantContextError } from "../shared/tenantContext";
import { ElMessage, ElMessageBox } from "element-plus";
import {
  cancelSubscription,
  closeOrder,
  fetchReconciliation,
  fetchSubscriptionBundle,
  refundOrder,
  resumeSubscription,
  type CancelMode,
  type ReconciliationReport,
} from "./api";
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
  type OrderRecord,
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

const actionError = ref("");
const busyOrderId = ref("");
const refundTarget = ref<OrderRecord | null>(null);
const refundVisible = ref(false);
const refundYuan = ref<number | undefined>(undefined);
const refundReason = ref("");
const refunding = ref(false);

const refundRemaining = computed(() =>
  refundTarget.value
    ? refundTarget.value.amount_cents - (refundTarget.value.refunded_cents ?? 0)
    : 0,
);

function canClose(order: OrderRecord): boolean {
  return order.status === "created" || order.status === "pending";
}

function canRefund(order: OrderRecord): boolean {
  return (
    (order.status === "paid" || order.status === "partial_refunded") &&
    (order.amount_cents - (order.refunded_cents ?? 0)) > 0
  );
}

async function closeUnpaid(order: OrderRecord): Promise<void> {
  if (!activeTenantId.value) return;
  actionError.value = "";
  try {
    await ElMessageBox.confirm(
      t("Close this unpaid order? The payment provider trade is closed first, so it can no longer be paid."),
      t("Close order"),
      { confirmButtonText: t("Close order"), cancelButtonText: t("Cancel") },
    );
  } catch {
    return;
  }
  busyOrderId.value = order.id;
  try {
    await closeOrder(activeTenantId.value, order.id);
    ElMessage.success(t("The order was closed."));
    await resource.reload();
  } catch (cause) {
    actionError.value = cause instanceof ApiError ? describeError(cause.code) : t("The order could not be closed.");
  } finally {
    busyOrderId.value = "";
  }
}

function openRefund(order: OrderRecord): void {
  refundTarget.value = order;
  refundYuan.value = undefined;
  refundReason.value = "";
  actionError.value = "";
  refundVisible.value = true;
}

async function submitRefund(): Promise<void> {
  if (!activeTenantId.value || !refundTarget.value) return;
  const remaining = refundRemaining.value;
  const cents =
    refundYuan.value === undefined || refundYuan.value === null
      ? remaining
      : Math.round(Number(refundYuan.value) * 100);
  if (!Number.isFinite(cents) || cents <= 0 || cents > remaining) {
    actionError.value = `${t("Refund amount must be between ¥0.01 and")} ${formatAmount(remaining, refundTarget.value.currency)}.`;
    return;
  }
  refunding.value = true;
  actionError.value = "";
  try {
    await refundOrder(activeTenantId.value, refundTarget.value.id, cents, refundReason.value.trim());
    ElMessage.success(t("The refund was submitted to the payment provider."));
    refundVisible.value = false;
    refundTarget.value = null;
    await resource.reload();
  } catch (cause) {
    actionError.value = cause instanceof ApiError ? describeError(cause.code) : t("The refund could not be submitted.");
  } finally {
    refunding.value = false;
  }
}

// Reconciliation: read-only window over the same date the server accepts
// (local ``YYYY-MM-DD`` boundaries). The report never mutates orders.
function todayIso(): string {
  const now = new Date();
  const local = new Date(now.getTime() - now.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 10);
}

const reconciliation = ref<ReconciliationReport | null>(null);
const reconciliationStart = ref(todayIso());
const reconciliationEnd = ref(todayIso());
const reconciliationLoading = ref(false);
const reconciliationError = ref("");

const reconciliationDiscrepancyCount = computed(
  () =>
    (reconciliation.value?.discrepancies.length ?? 0) +
    (reconciliation.value?.refund_discrepancies.length ?? 0),
);

async function loadReconciliation(): Promise<void> {
  if (!activeTenantId.value) return;
  reconciliationLoading.value = true;
  reconciliationError.value = "";
  try {
    reconciliation.value = await fetchReconciliation(
      activeTenantId.value,
      reconciliationStart.value,
      reconciliationEnd.value,
    );
  } catch (cause) {
    reconciliationError.value =
      cause instanceof ApiError ? describeError(cause.code) : t("The reconciliation report could not be loaded.");
    reconciliation.value = null;
  } finally {
    reconciliationLoading.value = false;
  }
}

// Subscription lifecycle: scheduling a cancellation is reversible, and an
// immediate cancellation is gated behind an explicit confirmation.
const lifecycleBusy = ref(false);

async function cancelSubscriptionAction(mode: CancelMode): Promise<void> {
  if (!activeTenantId.value) return;
  const confirmText =
    mode === "immediate"
      ? t("Cancel immediately? The subscription stops serving right away and this cannot be undone.")
      : t("Cancel at the end of the current period? You can resume before it ends.");
  try {
    await ElMessageBox.confirm(confirmText, t("Cancel subscription"), {
      confirmButtonText: mode === "immediate" ? t("Cancel immediately") : t("Cancel at period end"),
      cancelButtonText: t("Keep subscription"),
      type: "warning",
    });
  } catch {
    return;
  }
  lifecycleBusy.value = true;
  actionError.value = "";
  try {
    await cancelSubscription(activeTenantId.value, mode);
    ElMessage.success(
      mode === "immediate" ? t("The subscription was cancelled.") : t("Cancellation scheduled."),
    );
    await resource.reload();
  } catch (cause) {
    actionError.value =
      cause instanceof ApiError ? describeError(cause.code) : t("The subscription could not be cancelled.");
  } finally {
    lifecycleBusy.value = false;
  }
}

async function resumeSubscriptionAction(): Promise<void> {
  if (!activeTenantId.value) return;
  lifecycleBusy.value = true;
  actionError.value = "";
  try {
    await resumeSubscription(activeTenantId.value);
    ElMessage.success(t("The subscription will keep renewing."));
    await resource.reload();
  } catch (cause) {
    actionError.value =
      cause instanceof ApiError ? describeError(cause.code) : t("The subscription could not be resumed.");
  } finally {
    lifecycleBusy.value = false;
  }
}

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
        <div class="settings-usage-row lifecycle-actions">
          <el-button
            v-if="subscription.cancel_at_period_end"
            :loading="lifecycleBusy"
            type="primary"
            plain
            @click="resumeSubscriptionAction"
          >{{ t("Resume subscription") }}</el-button>
          <template v-else>
            <el-button
              v-if="subscription.status !== 'cancelled' && subscription.status !== 'expired'"
              :loading="lifecycleBusy"
              @click="cancelSubscriptionAction('period_end')"
            >{{ t("Cancel at period end") }}</el-button>
            <el-button
              v-if="subscription.status !== 'cancelled' && subscription.status !== 'expired'"
              :loading="lifecycleBusy"
              type="danger"
              plain
              @click="cancelSubscriptionAction('immediate')"
            >{{ t("Cancel immediately") }}</el-button>
          </template>
        </div>
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
        <el-table-column label="Refunded">
          <template #default="{ row }">{{ formatAmount(row.refunded_cents ?? 0, row.currency) }}</template>
        </el-table-column>
        <el-table-column prop="created_at" label="Created" />
        <el-table-column label="" width="170" fixed="right">
          <template #default="{ row }">
            <el-button
              v-if="canClose(row)"
              size="small"
              :loading="busyOrderId === row.id"
              @click="closeUnpaid(row)"
            >{{ t("Close order") }}</el-button>
            <el-button
              v-if="canRefund(row)"
              size="small"
              type="primary"
              plain
              @click="openRefund(row)"
            >{{ t("Refund") }}</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-else description="No orders yet." :image-size="64" />
    </el-card>

    <el-alert
      v-if="actionError && !refundVisible"
      class="settings-inline-error"
      type="error"
      show-icon
      :closable="true"
      :title="actionError"
      @close="actionError = ''"
    />

    <el-dialog
      v-model="refundVisible"
      width="420px"
      align-center
      :close-on-click-modal="false"
      data-refund-dialog
    >
      <template #header>{{ t("Refund order") }}</template>
      <div class="refund-dialog">
        <p class="muted">{{ t("Order") }} {{ refundTarget?.id }}</p>
        <p class="muted">
          {{ t("Paid") }} {{ formatAmount(refundTarget?.amount_cents ?? 0, refundTarget?.currency ?? "CNY") }}
          · {{ t("Refundable") }} {{ formatAmount(refundRemaining, refundTarget?.currency ?? "CNY") }}
        </p>
        <el-form label-position="top">
          <el-form-item :label="t('Refund amount (¥, blank refunds the remainder)')">
            <el-input-number
              v-model="refundYuan"
              :min="0.01"
              :max="refundRemaining / 100"
              :precision="2"
              :step="1"
              controls-position="right"
              data-refund-amount
            />
          </el-form-item>
          <el-form-item :label="t('Reason')">
            <el-input v-model="refundReason" maxlength="200" show-word-limit data-refund-reason />
          </el-form-item>
        </el-form>
        <el-alert
          v-if="actionError"
          type="error"
          show-icon
          :closable="false"
          :title="actionError"
        />
      </div>
      <template #footer>
        <el-button @click="refundVisible = false">{{ t("Cancel") }}</el-button>
        <el-button type="primary" :loading="refunding" @click="submitRefund">
          {{ t("Submit refund") }}
        </el-button>
      </template>
    </el-dialog>

    <el-card class="settings-card" shadow="never" data-reconciliation>
      <template #header>{{ t("Payment reconciliation") }}</template>
      <p class="muted">
        {{
          t(
            "Read-only comparison between local orders and the payment provider for a date window. Nothing is changed by this report.",
          )
        }}
      </p>
      <div class="settings-usage-row">
        <el-input v-model="reconciliationStart" type="date" :aria-label="t('Start date')" />
        <el-input v-model="reconciliationEnd" type="date" :aria-label="t('End date')" />
        <el-button :loading="reconciliationLoading" @click="loadReconciliation">
          {{ t("Load report") }}
        </el-button>
      </div>
      <el-alert
        v-if="reconciliationError"
        class="settings-inline-error"
        type="error"
        show-icon
        :closable="false"
        :title="reconciliationError"
      />
      <template v-if="reconciliation">
        <p class="muted">
          {{ reconciliation.start }} → {{ reconciliation.end }} ·
          {{ reconciliation.order_count }} {{ t("orders") }} ·
          {{ reconciliation.refund_slice_count }} {{ t("refund slices") }}
        </p>
        <el-alert
          v-if="reconciliation.unavailable_channels.length > 0"
          type="warning"
          show-icon
          :closable="false"
          :title="`${t('Channels that could not be queried')}: ${reconciliation.unavailable_channels.join(', ')}`"
        />
        <el-alert
          v-if="reconciliationDiscrepancyCount === 0"
          type="success"
          show-icon
          :closable="false"
          :title="t('No discrepancies in this window')"
        />
        <el-table
          v-if="reconciliation.discrepancies.length > 0"
          :data="reconciliation.discrepancies"
          size="small"
          data-discrepancies
        >
          <el-table-column prop="order_id" label="Order" />
          <el-table-column prop="channel" :label="t('Channel')" />
          <el-table-column prop="kind" :label="t('Kind')" />
          <el-table-column prop="local_status" :label="t('Local status')" />
          <el-table-column prop="provider_status" :label="t('Provider status')" />
        </el-table>
        <el-table
          v-if="reconciliation.refund_discrepancies.length > 0"
          :data="reconciliation.refund_discrepancies"
          size="small"
          data-refund-discrepancies
        >
          <el-table-column prop="refund_reference" :label="t('Refund reference')" />
          <el-table-column prop="kind" :label="t('Kind')" />
          <el-table-column prop="local_refund_cents" :label="t('Local refund')" />
          <el-table-column prop="provider_refund_cents" :label="t('Provider refund')" />
          <el-table-column prop="provider_status" :label="t('Provider status')" />
        </el-table>
      </template>
    </el-card>

    <p class="muted">{{ describeError("PAYMENT_PROVIDER_NOT_CONFIGURED") }}</p>
  </SettingsPageShell>
</template>

<style scoped>
.refund-dialog {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.refund-dialog :deep(.el-form-item) {
  margin-bottom: 12px;
}
</style>
