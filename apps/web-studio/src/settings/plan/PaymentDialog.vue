<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { ElMessage } from "element-plus";
import QRCode from "qrcode";
import { t } from "../../i18n";
import { closeOrder, fetchOrderStatus, type OrderResponse } from "./api";
import { describeError, formatPrice } from "./logic";
import { ApiError } from "../../services/http";

const props = defineProps<{
  tenantId: string;
  order: OrderResponse | null;
}>();

const emit = defineEmits<{
  settled: [OrderResponse];
  closed: [OrderResponse];
  dismissed: [];
}>();

const POLL_INTERVAL_MS = 3_000;

const visible = ref(false);
const status = ref("");
const providerStatus = ref("");
const qrDataUrl = ref("");
const qrError = ref("");
const polling = ref(false);
const actionError = ref("");
const closing = ref(false);
let timer: ReturnType<typeof setTimeout> | null = null;
let disposed = false;

const checkoutText = computed(() => props.order?.payment?.code_url || props.order?.payment?.qr_code || "");
const amountText = computed(() =>
  props.order ? formatPrice(props.order.amount_cents, props.order.currency) : "",
);
const isSettled = computed(() => status.value === "paid" || status.value === "partial_refunded" || status.value === "refunded");
const isTerminal = computed(() => ["closed", "failed"].includes(status.value));

function stopPolling(): void {
  if (timer !== null) {
    clearTimeout(timer);
    timer = null;
  }
}

function schedulePoll(): void {
  stopPolling();
  if (disposed || isSettled.value || isTerminal.value || !props.order) return;
  timer = setTimeout(() => void poll(), POLL_INTERVAL_MS);
}

async function renderQr(): Promise<void> {
  qrDataUrl.value = "";
  qrError.value = "";
  if (!checkoutText.value) return;
  try {
    qrDataUrl.value = await QRCode.toDataURL(checkoutText.value, {
      margin: 1,
      width: 240,
      color: { dark: "#121f28", light: "#ffffff" },
    });
  } catch {
    qrError.value = t("The payment code could not be rendered; open the link below instead.");
  }
}

async function poll(): Promise<void> {
  if (!props.order || disposed) return;
  polling.value = true;
  try {
    const latest = await fetchOrderStatus(props.tenantId, props.order.id);
    status.value = latest.status;
    providerStatus.value = latest.provider_status ?? "";
    actionError.value = latest.provider_error ? describeError(latest.provider_error) : "";
    if (latest.status === "paid") {
      stopPolling();
      emit("settled", latest);
    } else if (["closed", "failed"].includes(latest.status)) {
      stopPolling();
      emit("closed", latest);
    } else if (latest.status === "refunded" || latest.status === "partial_refunded") {
      stopPolling();
      emit("settled", latest);
    }
  } catch (cause) {
    actionError.value = cause instanceof ApiError ? describeError(cause.code) : t("Payment status could not be checked.");
  } finally {
    polling.value = false;
    schedulePoll();
  }
}

async function closeTrade(): Promise<void> {
  if (!props.order) return;
  closing.value = true;
  actionError.value = "";
  try {
    const closed = await closeOrder(props.tenantId, props.order.id);
    status.value = closed.status;
    stopPolling();
    emit("closed", closed);
    ElMessage.success(t("The order was closed on the payment provider."));
  } catch (cause) {
    actionError.value = cause instanceof ApiError ? describeError(cause.code) : t("The order could not be closed.");
  } finally {
    closing.value = false;
  }
}

async function copyCheckoutText(): Promise<void> {
  try {
    await navigator.clipboard.writeText(checkoutText.value);
    ElMessage.success(t("Payment link copied."));
  } catch {
    ElMessage.warning(t("Copy failed; select the link and copy it manually."));
  }
}

function dismiss(): void {
  visible.value = false;
  emit("dismissed");
}

watch(
  () => props.order,
  async (order) => {
    stopPolling();
    if (!order) {
      visible.value = false;
      return;
    }
    status.value = order.status;
    providerStatus.value = order.provider_status ?? "";
    actionError.value = "";
    visible.value = true;
    await renderQr();
    void poll();
  },
  { immediate: true },
);

watch(checkoutText, () => void renderQr());

onBeforeUnmount(() => {
  disposed = true;
  stopPolling();
});
</script>

<template>
  <el-dialog
    :model-value="visible"
    width="420px"
    align-center
    :close-on-click-modal="false"
    data-payment-dialog
    @close="dismiss"
  >
    <template #header>
      <span class="payment-dialog__title">{{ t("Complete the payment") }}</span>
    </template>

    <div class="payment-dialog" :data-order-status="status">
      <p class="payment-dialog__amount">{{ amountText }}</p>
      <p class="muted">{{ t("Order") }} {{ order?.id }}</p>
      <p class="muted" data-payment-status>
        {{ t("Status") }}:
        <strong>{{ status || "—" }}</strong>
        <span v-if="providerStatus"> · {{ t("provider reports") }} {{ providerStatus }}</span>
      </p>

      <div v-if="isSettled" class="payment-dialog__result payment-dialog__result--ok">
        <el-result icon="success" :title="t('Payment received')" :sub-title="t('Your subscription is being activated.')" />
      </div>

      <div v-else-if="isTerminal" class="payment-dialog__result">
        <el-result
          icon="warning"
          :title="t('The order is no longer payable')"
          :sub-title="t('Start a new order to try again.')"
        />
      </div>

      <template v-else>
        <div class="payment-dialog__qr">
          <img v-if="qrDataUrl" :src="qrDataUrl" :alt="t('Payment QR code')" width="240" height="240" />
          <el-empty v-else :image-size="96" :description="qrError || t('No payment code was returned by the provider.')" />
        </div>
        <p class="muted payment-dialog__hint">{{ t("Scan the code with WeChat Pay or Alipay to pay. This window checks the result automatically.") }}</p>
        <div v-if="checkoutText" class="payment-dialog__link">
          <code>{{ checkoutText }}</code>
          <el-button size="small" text type="primary" @click="copyCheckoutText">{{ t("Copy link") }}</el-button>
        </div>
        <el-alert
          v-if="actionError"
          class="settings-inline-error"
          type="error"
          show-icon
          :closable="false"
          :title="actionError"
        />
        <div class="payment-dialog__actions">
          <el-button :loading="polling" @click="poll">{{ t("Check payment status") }}</el-button>
          <el-button :loading="closing" @click="closeTrade">{{ t("Close order") }}</el-button>
        </div>
      </template>
    </div>

    <template #footer>
      <el-button @click="dismiss">{{ isSettled || isTerminal ? t("Done") : t("Pay later") }}</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.payment-dialog__title {
  font-size: 16px;
  font-weight: 600;
}

.payment-dialog {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.payment-dialog__amount {
  font-size: 24px;
  font-weight: 600;
  margin: 0;
  color: var(--app-text-strong, #121f28);
}

.payment-dialog__qr {
  display: flex;
  justify-content: center;
  padding: 8px 0;
}

.payment-dialog__qr img {
  display: block;
  border: 1px solid var(--app-border, #e3eaf2);
  border-radius: 8px;
  padding: 8px;
  background: #ffffff;
}

.payment-dialog__hint {
  text-align: center;
  margin: 0;
}

.payment-dialog__link {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  padding: 8px 10px;
  border: 1px solid var(--app-border, #e3eaf2);
  border-radius: 6px;
  background: var(--app-surface-muted, #f5f9fc);
}

.payment-dialog__link code {
  flex: 1;
  min-width: 0;
  overflow-wrap: anywhere;
  font-size: 12px;
}

.payment-dialog__actions {
  display: flex;
  justify-content: center;
  gap: 8px;
  flex-wrap: wrap;
}

.payment-dialog__result {
  padding: 4px 0;
}
</style>
