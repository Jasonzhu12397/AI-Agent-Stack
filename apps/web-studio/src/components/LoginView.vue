<script setup lang="ts">
import { ref } from "vue";
import { login, type CurrentUser } from "../services/api";

const emit = defineEmits<{ authenticated: [user: CurrentUser] }>();
const username = ref("");
const password = ref("");
const error = ref("");
const submitting = ref(false);

async function submit(): Promise<void> {
  error.value = "";
  submitting.value = true;
  try {
    const user = await login(username.value.trim(), password.value);
    emit("authenticated", user);
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Login failed.";
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
      <p class="eyebrow">SECURE WORKSPACE</p>
      <h1>Welcome back.</h1>
      <p class="login-intro">Sign in to manage your agents, workflows and integrations.</p>
      <el-form label-position="top" @submit.prevent="submit">
        <el-form-item label="Username">
          <el-input
            v-model="username"
            autocomplete="username"
            placeholder="Enter your username"
            size="large"
          />
        </el-form-item>
        <el-form-item label="Password">
          <el-input
            v-model="password"
            autocomplete="current-password"
            placeholder="Enter your password"
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
          :disabled="!username || !password"
        >
          Sign in
        </el-button>
      </el-form>
      <p class="login-footnote">Access is controlled by your organization membership.</p>
    </section>
  </main>
</template>
