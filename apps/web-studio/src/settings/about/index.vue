<script setup lang="ts">
import { computed, onMounted } from "vue";
import { t } from "../../i18n";
import SettingsPageShell from "../shared/SettingsPageShell.vue";
import { useSettingsResource } from "../shared/useSettingsResource";
import { fetchAbout } from "./api";
import { describeError, normalizeAbout, pageLabels } from "./logic";

const resource = useSettingsResource(fetchAbout);
const info = computed(() => normalizeAbout(resource.data.value));

onMounted(() => void resource.reload());
void pageLabels;
void describeError;
</script>

<template>
  <SettingsPageShell
    title="About"
    description="System version, build, licensing and release notes reported by the backend."
    :state="resource.state.value"
    :error-code="resource.errorCode.value"
    :error-message="resource.errorMessage.value"
    empty-message="About information could not be loaded."
    denied-message="Your role does not allow viewing system information."
    @retry="resource.reload()"
  >
    <el-card class="settings-card" shadow="never">
      <template #header>{{ t("System information") }}</template>
      <el-descriptions :column="1" border>
        <el-descriptions-item :label="t('Version')">{{ info.version }}</el-descriptions-item>
        <el-descriptions-item :label="t('Build')">{{ info.build }}</el-descriptions-item>
        <el-descriptions-item :label="t('Licenses')">{{ info.licenses }}</el-descriptions-item>
        <el-descriptions-item :label="t('Release notes')">
          <a v-if="info.release_notes_url" :href="info.release_notes_url" target="_blank" rel="noopener noreferrer">
            {{ t("Release notes") }} ↗
          </a>
          <span v-else>unknown</span>
        </el-descriptions-item>
      </el-descriptions>
      <p class="muted">
        {{ t("Version metadata comes from the server; values shown as unknown were not reported by the backend.") }}
      </p>
    </el-card>
  </SettingsPageShell>
</template>
