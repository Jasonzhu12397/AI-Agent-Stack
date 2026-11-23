<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { request, ApiError } from "../../services/http";
import { useSettingsResource } from "../shared/useSettingsResource";
import SettingsPageShell from "../shared/SettingsPageShell.vue";
import {
  validateProfile,
  profilePayload,
  summarizeIdentities,
  sessionCanRevoke,
  fieldErrorsFromError,
  stateFromError,
  errorMessageKey,
  type ProfileForm,
  type Identity,
  type SessionInfo,
} from "./logic";

const profile = useSettingsResource<ProfileForm>(() =>
  request<ProfileForm>("/api/v1/users/me/"),
);

const identities = useSettingsResource<Identity[]>(async () => {
  const payload = await request<{ results?: Identity[] } | Identity[]>("/api/v1/users/me/identities/");
  return Array.isArray(payload) ? payload : payload.results ?? [];
});

const sessions = useSettingsResource<SessionInfo[]>(async () => {
  const payload = await request<{ results?: SessionInfo[] } | SessionInfo[]>("/api/v1/users/me/sessions/");
  return Array.isArray(payload) ? payload : payload.results ?? [];
});

const form = ref<ProfileForm>({ display_name: "", email: "", timezone: "", locale: "en" });
const saving = ref(false);
const savedNotice = ref(false);
const saveError = ref("");
const saveCode = ref("");
const fieldErrors = ref<Record<string, string[]>>({});
const revokingKey = ref("");

const identitySummary = computed(() =>
  identities.data.value ? summarizeIdentities(identities.data.value) : null,
);

function fillForm(data: ProfileForm): void {
  form.value = {
    display_name: data.display_name ?? "",
    email: data.email ?? "",
    timezone: data.timezone ?? "",
    locale: data.locale ?? "en",
  };
}

async function reloadAll(): Promise<void> {
  await Promise.all([profile.reload(), identities.reload(), sessions.reload()]);
  if (profile.data.value) fillForm(profile.data.value);
}

onMounted(() => {
  void reloadAll();
});

function firstError(field: string): string {
  return fieldErrors.value[field]?.[0] ?? "";
}

async function saveProfile(): Promise<void> {
  saveError.value = "";
  saveCode.value = "";
  fieldErrors.value = {};
  savedNotice.value = false;

  const errors = validateProfile(form.value);
  if (Object.keys(errors).length > 0) {
    for (const [field, code] of Object.entries(errors)) {
      fieldErrors.value[field] = [errorMessageKey(code)];
    }
    return;
  }

  saving.value = true;
  try {
    const updated = await request<ProfileForm>("/api/v1/users/me/", {
      method: "PATCH",
      body: JSON.stringify(profilePayload(form.value)),
    });
    fillForm(updated);
    profile.data.value = updated;
    savedNotice.value = true;
  } catch (cause) {
    const mapped = stateFromError(cause);
    if (mapped.kind === "denied") profile.state.value = "denied";
    if (cause instanceof ApiError) {
      saveCode.value = cause.code;
      saveError.value = cause.message || errorMessageKey(cause.code);
      fieldErrors.value = fieldErrorsFromError(cause);
    } else {
      saveError.value = cause instanceof Error ? cause.message : "Profile could not be saved.";
    }
  } finally {
    saving.value = false;
  }
}

async function revokeSession(session: SessionInfo): Promise<void> {
  revokingKey.value = session.key;
  try {
    await request(`/api/v1/users/me/sessions/${encodeURIComponent(session.key)}/revoke/`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    await sessions.reload();
  } catch (cause) {
    const mapped = stateFromError(cause);
    if (mapped.kind === "denied") {
      sessions.state.value = "denied";
    } else {
      sessions.errorMessage.value = cause instanceof Error ? cause.message : "Revocation failed.";
    }
  } finally {
    revokingKey.value = "";
  }
}
</script>

<template>
  <SettingsPageShell
    title="Account Management"
    description="Profile details, sign-in methods and active sessions."
    :state="profile.state.value"
    :error-code="profile.errorCode.value"
    :error-message="profile.errorMessage.value"
    @retry="reloadAll"
  >
    <section class="settings-card" aria-labelledby="profile-heading">
      <h3 id="profile-heading">Profile details</h3>
      <el-alert
        v-if="saveError"
        class="settings-form-alert"
        type="error"
        show-icon
        :closable="false"
        :title="saveCode ? `${saveCode}: ${saveError}` : saveError"
      />
      <el-form label-position="top" class="inline-form">
        <el-form-item label="Display name" :error="firstError('display_name')">
          <el-input v-model="form.display_name" autocomplete="name" />
        </el-form-item>
        <el-form-item label="Email" :error="firstError('email')">
          <el-input v-model="form.email" autocomplete="email" />
        </el-form-item>
        <el-form-item label="Timezone" :error="firstError('timezone')">
          <el-input v-model="form.timezone" autocomplete="timezone" />
        </el-form-item>
        <el-form-item label="Locale" :error="firstError('locale')">
          <el-select v-model="form.locale" class="settings-form-control">
            <el-option label="English" value="en" />
            <el-option label="中文" value="zh" />
          </el-select>
        </el-form-item>
      </el-form>
      <div class="settings-card__actions">
        <el-button type="primary" :loading="saving" @click="saveProfile">Save changes</el-button>
        <el-tag v-if="savedNotice" type="success" effect="plain">Saved</el-tag>
      </div>
    </section>

    <section class="settings-card" aria-labelledby="identities-heading">
      <h3 id="identities-heading">Sign-in methods</h3>
      <div v-if="identities.state.value === 'loading'" class="settings-inline-loading">
        <el-skeleton :rows="2" animated />
      </div>
      <el-empty v-else-if="identities.state.value === 'empty' || identitySummary?.boundCount === 0" description="No identities connected." />
      <el-result
        v-else-if="identities.state.value === 'denied' || identities.state.value === 'error'"
        icon="warning"
        :title="identities.state.value === 'denied' ? 'Permission denied' : 'Sign-in methods could not be loaded'"
        :sub-title="identities.errorMessage.value"
      />
      <ul v-else class="settings-list">
        <li v-for="identity in identities.data.value ?? []" :key="`${identity.provider}-${identity.uid ?? identity.bound_at ?? ''}`" class="settings-list__item">
          <div>
            <span class="settings-list__title">{{ identity.provider }}</span>
            <span v-if="identity.uid" class="muted"> · {{ identity.uid }}</span>
          </div>
          <el-tag v-if="identity.has_password || identity.provider === 'password'" type="info" effect="plain">Password</el-tag>
        </li>
      </ul>
    </section>

    <section class="settings-card" aria-labelledby="sessions-heading">
      <h3 id="sessions-heading">Active sessions</h3>
      <div v-if="sessions.state.value === 'loading'" class="settings-inline-loading">
        <el-skeleton :rows="2" animated />
      </div>
      <el-empty v-else-if="sessions.state.value === 'empty' || (sessions.data.value ?? []).length === 0" description="No active sessions." />
      <el-result
        v-else-if="sessions.state.value === 'denied' || sessions.state.value === 'error'"
        icon="warning"
        :title="sessions.state.value === 'denied' ? 'Permission denied' : 'Sessions could not be loaded'"
        :sub-title="sessions.errorMessage.value"
      />
      <ul v-else class="settings-list">
        <li v-for="session in sessions.data.value ?? []" :key="session.key" class="settings-list__item">
          <div>
            <span class="settings-list__title">
              {{ session.current ? "Current session" : "Session" }}
            </span>
            <span v-if="session.last_ip" class="muted"> · {{ session.last_ip }}</span>
            <span v-if="session.created_at" class="muted"> · {{ session.created_at }}</span>
          </div>
          <el-popconfirm
            v-if="sessionCanRevoke(session)"
            title="Confirm session revocation"
            description="Other sessions will be signed out immediately."
            confirm-button-text="Revoke"
            cancel-button-text="Cancel"
            @confirm="revokeSession(session)"
          >
            <template #reference>
              <el-button
                size="small"
                text
                :loading="revokingKey === session.key"
                :disabled="revokingKey !== ''"
              >
                Revoke
              </el-button>
            </template>
          </el-popconfirm>
          <el-tag v-else type="info" effect="plain">{{ session.current ? "Current session" : "Revoked" }}</el-tag>
        </li>
      </ul>
    </section>
  </SettingsPageShell>
</template>
