<script setup lang="ts">
import { computed } from "vue";
import { useRoute } from "vue-router";
import { SETTINGS_GROUPS, SETTINGS_LABELS } from "../shared/scope";

const route = useRoute();
const currentSection = computed(() => (route.meta.section as string) ?? "");
</script>

<template>
  <nav class="settings-nav" aria-label="Settings navigation">
    <div v-for="group in SETTINGS_GROUPS" :key="group.group" class="settings-nav__group">
      <p class="settings-nav__group-title">
        {{ group.group === "account" ? "Account" : "General" }}
      </p>
      <ul class="settings-nav__list">
        <li v-for="slug in group.slugs" :key="slug">
          <router-link
            :to="`/settings/${slug}`"
            class="settings-nav__link"
            :class="{ active: currentSection === slug }"
            :aria-current="currentSection === slug ? 'page' : undefined"
          >
            {{ SETTINGS_LABELS[slug] }}
          </router-link>
        </li>
      </ul>
    </div>
  </nav>
</template>
