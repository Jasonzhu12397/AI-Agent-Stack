<script setup lang="ts">
import { t } from "../../i18n";
import type { SettingsLoadState } from "./scope";

const props = withDefaults(
  defineProps<{
    title: string;
    description?: string;
    state: SettingsLoadState;
    errorCode?: string;
    errorMessage?: string;
    emptyMessage?: string;
    loadingMessage?: string;
    deniedMessage?: string;
    unavailable?: boolean;
    unavailableCode?: string;
  }>(),
  {
    description: "",
    errorCode: "",
    errorMessage: "",
    emptyMessage: "Nothing to show yet.",
    loadingMessage: "Loading settings...",
    deniedMessage: "You do not have permission to view or change these settings.",
    unavailable: false,
    unavailableCode: "",
  },
);

const emit = defineEmits<{ (event: "retry"): void }>();
</script>

<template>
  <section class="settings-page" :aria-busy="state === 'loading'" data-settings-page>
    <header class="settings-page__header">
      <div>
        <h2>{{ t(title) }}</h2>
        <p v-if="description" class="settings-page__description">{{ t(description) }}</p>
      </div>
      <div v-if="$slots.actions" class="settings-page__actions">
        <slot name="actions" />
      </div>
    </header>

    <el-alert
      v-if="unavailable"
      class="settings-unavailable"
      type="warning"
      show-icon
      :closable="false"
      :title="t('This capability is unavailable in the current deployment.')"
      :description="unavailableCode"
      data-settings-unavailable
    />

    <div v-if="state === 'loading'" class="settings-loading" aria-live="polite">
      <el-skeleton :rows="4" animated />
      <p>{{ t(loadingMessage) }}</p>
    </div>

    <el-result
      v-else-if="state === 'denied'"
      icon="warning"
      :title="t('Permission denied')"
      :sub-title="errorCode ? `${t(deniedMessage)} (${errorCode})` : t(deniedMessage)"
      data-settings-denied
    />

    <el-result
      v-else-if="state === 'error'"
      icon="error"
      :title="t('Settings could not be loaded')"
      :sub-title="errorMessage ? t(errorMessage) : errorCode"
      data-settings-error
    >
      <template #extra>
        <el-button type="primary" @click="emit('retry')">{{ t("Retry") }}</el-button>
      </template>
    </el-result>

    <el-empty
      v-else-if="state === 'empty'"
      :description="t(emptyMessage)"
      data-settings-empty
    />

    <div v-else class="settings-page__body">
      <slot />
    </div>

    <footer v-if="$slots.footer" class="settings-page__footer">
      <slot name="footer" />
    </footer>
  </section>
</template>
