<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { t } from "../../i18n";
import { ApiError } from "../../services/http";
import SettingsPageShell from "../shared/SettingsPageShell.vue";
import { useSettingsResource } from "../shared/useSettingsResource";
import { activeTenantId, ensureTenantContext, tenantContextError } from "../shared/tenantContext";
import { createOrder, fetchBillingOverview } from "./api";
import {
  canPurchase,
  describeError,
  formatPrice,
  intervalLabel,
  pageLabels,
  paymentAvailability,
  quotaSummary,
  type BillingPlan,
} from "./logic";

const resource = useSettingsResource(async () => {
  await ensureTenantContext();
  if (!activeTenantId.value) {
    throw new ApiError(400, "TENANT_CONTEXT_UNAVAILABLE", tenantContextError.value || "No tenant is available.");
  }
  return fetchBillingOverview(activeTenantId.value);
});

const orderError = ref("");
const pendingPlan = ref("");
const payment = computed(() => paymentAvailability(resource.data.value?.capabilities));
const subscription = computed(() => resource.data.value?.subscription ?? null);

function purchaseState(plan: BillingPlan): { ok: boolean; reasonCode: string | null } {
  return canPurchase(plan, resource.data.value?.capabilities);
}

async function buy(plan: BillingPlan): Promise<void> {
  if (!activeTenantId.value) return;
  const state = purchaseState(plan);
  if (!state.ok) {
    orderError.value = describeError(state.reasonCode ?? "PURCHASE_NOT_ALLOWED");
    return;
  }
  orderError.value = "";
  pendingPlan.value = plan.code;
  try {
    const order = await createOrder(
      activeTenantId.value,
      plan.code,
      "wechat",
      `plan-${plan.code}-${Date.now()}`,
    );
    orderError.value = `Order ${order.id} is ${order.status}. Complete the payment in the payment provider app.`;
  } catch (cause) {
    orderError.value = cause instanceof ApiError ? describeError(cause.code) : "Order could not be created.";
  } finally {
    pendingPlan.value = "";
  }
}

onMounted(() => void resource.reload());
void pageLabels;
</script>

<template>
  <SettingsPageShell
    title="Standard Plan"
    description="Current plan, included capabilities and plan limits. Prices and purchasability come from the server."
    :state="resource.state.value"
    :error-code="resource.errorCode.value"
    :error-message="resource.errorMessage.value"
    empty-message="No plans are available for this tenant yet."
    denied-message="Only tenant members can view plans; tenant administrators can change them."
    @retry="resource.reload()"
  >
    <el-alert
      v-if="!payment.available"
      type="warning"
      show-icon
      :closable="false"
      title="Payment is unavailable"
      :description="`${describeError(payment.reasonCode)} (${payment.reasonCode})`"
      data-payment-unavailable
    />

    <el-card v-if="subscription" class="settings-card" shadow="never">
      <template #header>{{ t("Current plan") }}</template>
      <p>
        <strong>{{ subscription.plan_name }}</strong>
        <el-tag class="settings-tag" :type="subscription.status === 'active' ? 'success' : 'info'" effect="plain">
          {{ subscription.status }}
        </el-tag>
      </p>
      <p class="muted">
        {{ subscription.current_period_start || "—" }} → {{ subscription.current_period_end || "—" }}
      </p>
    </el-card>

    <el-empty v-else description="No active subscription. Choose a plan below." />

    <div class="settings-grid">
      <el-card v-for="plan in resource.data.value?.plans ?? []" :key="plan.code" shadow="never" class="settings-card">
        <template #header>
          <div class="settings-card__title">
            <span>{{ plan.name }}</span>
            <el-tag v-if="!plan.purchasable" type="info" effect="plain">{{ plan.reason_code }}</el-tag>
          </div>
        </template>
        <p class="settings-price">{{ formatPrice(plan.price_cents, plan.currency) }} <span class="muted">{{ t(intervalLabel(plan.interval)) }}</span></p>
        <p class="muted">{{ plan.description }}</p>
        <ul class="settings-list">
          <li v-for="feature in plan.features" :key="feature">{{ feature }}</li>
        </ul>
        <p class="muted">{{ t("Plan limits") }}: {{ quotaSummary(plan) }}</p>
        <el-tooltip
          :disabled="purchaseState(plan).ok"
          :content="describeError(purchaseState(plan).reasonCode ?? '')"
          placement="top"
        >
          <span>
            <el-button
              type="primary"
              :disabled="!purchaseState(plan).ok"
              :loading="pendingPlan === plan.code"
              @click="buy(plan)"
            >
              {{ t("Purchase") }}
            </el-button>
          </span>
        </el-tooltip>
      </el-card>
    </div>

    <el-alert
      v-if="orderError"
      class="settings-inline-error"
      type="error"
      show-icon
      :closable="true"
      :title="orderError"
      @close="orderError = ''"
    />

    <p class="muted">{{ t("Payment channels") }}: WeChat Pay, Alipay.</p>
  </SettingsPageShell>
</template>
