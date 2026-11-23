<script setup lang="ts">
import { onMounted, ref } from "vue";
import { t } from "../../i18n";
import { ApiError } from "../../services/http";
import SettingsPageShell from "../shared/SettingsPageShell.vue";
import { useSettingsResource } from "../shared/useSettingsResource";
import { activeTenantId, ensureTenantContext, tenantContextError } from "../shared/tenantContext";
import { fetchFeedback, submitFeedback } from "./api";
import {
  FEEDBACK_CATEGORIES,
  FEEDBACK_SEVERITIES,
  HELP_LINKS,
  categoryLabel,
  describeError,
  fieldErrorsFromDetails,
  pageLabels,
  severityLabel,
  validateFeedbackForm,
  type FeedbackRecord,
} from "./logic";

const category = ref<string>(FEEDBACK_CATEGORIES[0]);
const severity = ref<string>(FEEDBACK_SEVERITIES[0]);
const body = ref("");
const fieldErrors = ref<Record<string, string>>({});
const submitState = ref<"idle" | "submitting" | "submitted" | "error">("idle");
const submitError = ref("");
const submitted = ref<{ id: string; created_at: string | null } | null>(null);

const resource = useSettingsResource(async () => {
  await ensureTenantContext();
  if (!activeTenantId.value) {
    throw new ApiError(400, "TENANT_CONTEXT_UNAVAILABLE", tenantContextError.value || "No tenant is available.");
  }
  return fetchFeedback(activeTenantId.value);
});

async function submit(): Promise<void> {
  if (!activeTenantId.value) return;
  const validation = validateFeedbackForm({ category: category.value, severity: severity.value, body: body.value });
  fieldErrors.value = validation.fieldErrors;
  if (!validation.ok) {
    submitState.value = "error";
    submitError.value = describeError(validation.code);
    return;
  }
  submitState.value = "submitting";
  submitError.value = "";
  submitted.value = null;
  try {
    const record = await submitFeedback(activeTenantId.value, {
      category: category.value,
      severity: severity.value,
      body: body.value.trim(),
    });
    submitted.value = { id: record.id, created_at: record.created_at };
    submitState.value = "submitted";
    body.value = "";
    fieldErrors.value = {};
    await resource.reload();
  } catch (cause) {
    submitState.value = "error";
    if (cause instanceof ApiError) {
      fieldErrors.value = fieldErrorsFromDetails(cause.details);
      submitError.value = `${describeError(cause.code)} (${cause.code})`;
    } else {
      submitError.value = "Feedback could not be submitted.";
    }
  }
}

onMounted(() => void resource.reload());
void pageLabels;
</script>

<template>
  <SettingsPageShell
    title="Help & Feedback"
    description="Report issues, request features, and browse platform documentation and source resources."
    :state="resource.state.value"
    :error-code="resource.errorCode.value"
    :error-message="resource.errorMessage.value"
    empty-message="No feedback has been submitted yet."
    denied-message="Only tenant members can submit feedback."
    @retry="resource.reload()"
  >
    <el-card class="settings-card" shadow="never">
      <template #header>{{ t("Send feedback") }}</template>
      <el-form label-position="top" class="settings-form" @submit.prevent>
        <el-form-item :label="t('Category')" :error="fieldErrors.category">
          <el-select v-model="category" style="max-width: 240px">
            <el-option v-for="option in FEEDBACK_CATEGORIES" :key="option" :label="t(categoryLabel(option))" :value="option" />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('Severity')" :error="fieldErrors.severity">
          <el-select v-model="severity" style="max-width: 240px">
            <el-option v-for="option in FEEDBACK_SEVERITIES" :key="option" :label="t(severityLabel(option))" :value="option" />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('Message')" :error="fieldErrors.body">
          <el-input v-model="body" type="textarea" :rows="4" :placeholder="t('Describe the issue or your request.')" />
        </el-form-item>
      </el-form>

      <el-alert
        v-if="submitState === 'submitted' && submitted"
        type="success"
        show-icon
        :closable="false"
        :title="`${t('Feedback submitted')}: #${submitted.id}`"
        :description="submitted.created_at ?? ''"
        data-feedback-submitted
      />
      <el-alert
        v-if="submitState === 'error' && submitError"
        class="settings-inline-error"
        type="error"
        show-icon
        :closable="true"
        :title="submitError"
        @close="submitError = ''"
      />

      <el-button type="primary" :loading="submitState === 'submitting'" @click="submit">
        {{ t("Submit feedback") }}
      </el-button>
    </el-card>

    <el-card class="settings-card" shadow="never">
      <template #header>{{ t("Help resources") }}</template>
      <ul class="settings-list">
        <li v-for="link in HELP_LINKS" :key="link.href">
          <a :href="link.href" target="_blank" rel="noopener noreferrer">{{ t(link.label) }} ↗</a>
        </li>
      </ul>
    </el-card>

    <el-card v-if="(resource.data.value ?? []).length > 0" class="settings-card" shadow="never">
      <template #header>{{ t("Previous feedback") }}</template>
      <el-table :data="resource.data.value ?? []" size="small" data-feedback-history>
        <el-table-column :label="t('Category')">
          <template #default="{ row }">{{ t(categoryLabel(row.category)) }}</template>
        </el-table-column>
        <el-table-column :label="t('Severity')">
          <template #default="{ row }">{{ t(severityLabel(row.severity)) }}</template>
        </el-table-column>
        <el-table-column prop="body" :label="t('Message')" show-overflow-tooltip />
        <el-table-column :label="t('Reference')">
          <template #default="{ row }">#{{ (row as FeedbackRecord).id }}</template>
        </el-table-column>
      </el-table>
    </el-card>
  </SettingsPageShell>
</template>
