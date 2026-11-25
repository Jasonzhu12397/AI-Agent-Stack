<script setup lang="ts">
import { computed, ref } from "vue";
import { useRoute } from "vue-router";
import { register, type CurrentUser } from "../services/api";

const emit = defineEmits<{ authenticated: [user: CurrentUser] }>();
const route = useRoute();
const mode = ref<"email" | "phone">("email");
const identifier = ref("");
const password = ref("");
const confirmPassword = ref("");
const displayName = ref("");
const error = ref("");
const submitting = ref(false);

const canSubmit = computed(
  () =>
    identifier.value.trim().length > 0 &&
    password.value.length >= 8 &&
    password.value === confirmPassword.value,
);

function switchMode(next: "email" | "phone"): void {
  mode.value = next;
  identifier.value = "";
  error.value = "";
}

async function submit(): Promise<void> {
  error.value = "";
  if (password.value !== confirmPassword.value) {
    error.value = "The two passwords do not match.";
    return;
  }
  submitting.value = true;
  try {
    const user = await register(identifier.value.trim(), password.value, displayName.value.trim());
    emit("authenticated", user);
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Registration failed.";
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <main class="login-page">
    <section class="login-card">
      <a class="brand" href="/">
        <span class="brand-mark">A</span>
        <span>AI Agent <strong>Studio</strong></span>
      </a>
      <p class="eyebrow">CREATE YOUR WORKSPACE</p>
      <h1>Start building.</h1>
      <p class="login-intro">
        Register with an email address or a phone number. A personal workspace and a default
        project are created for you automatically.
      </p>
      <el-form label-position="top" @submit.prevent="submit">
        <el-form-item label="Sign up with">
          <el-radio-group :model-value="mode" @change="switchMode(String($event) as 'email' | 'phone')">
            <el-radio-button value="email">Email</el-radio-button>
            <el-radio-button value="phone">Phone</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item :label="mode === 'email' ? 'Email address' : 'Phone number'">
          <el-input
            v-model="identifier"
            :autocomplete="mode === 'email' ? 'email' : 'tel'"
            :placeholder="mode === 'email' ? 'name@example.com' : '13800138000'"
            size="large"
          />
        </el-form-item>
        <el-form-item label="Display name (optional)">
          <el-input v-model="displayName" autocomplete="name" placeholder="How should we call you?" size="large" />
        </el-form-item>
        <el-form-item label="Password">
          <el-input
            v-model="password"
            autocomplete="new-password"
            placeholder="At least 8 characters"
            show-password
            size="large"
          />
        </el-form-item>
        <el-form-item label="Confirm password">
          <el-input
            v-model="confirmPassword"
            autocomplete="new-password"
            placeholder="Repeat the password"
            show-password
            size="large"
            @keyup.enter="submit"
          />
        </el-form-item>
        <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
        <el-button
          class="login-submit"
          type="primary"
          size="large"
          native-type="submit"
          :loading="submitting"
          :disabled="!canSubmit"
        >
          Create account
        </el-button>
      </el-form>
      <p class="login-footnote">
        Already registered?
        <router-link :to="{ path: '/login', query: route.query }">Sign in</router-link>
      </p>
    </section>
  </main>
</template>

<style scoped>
.login-intro {
  margin-bottom: 24px;
}

.login-card :deep(.el-radio-group) {
  width: 100%;
}

.login-card :deep(.el-radio-button) {
  flex: 1;
}

.login-card :deep(.el-radio-button__inner) {
  width: 100%;
}

.login-footnote a {
  color: #0b6bcb;
  font-weight: 600;
}
</style>
