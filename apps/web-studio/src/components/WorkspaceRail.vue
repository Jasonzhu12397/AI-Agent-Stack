<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { t } from "../i18n";
import {
  activeProjectId,
  activeTenantId,
  activeThreadId,
  loadWorkspace,
  projects,
  refreshThreads,
  selectProject,
  selectTenant,
  setActiveThread,
  tenants,
  threads,
} from "../state/workspace";

const props = defineProps<{ user?: string }>();
const emit = defineEmits<{ (event: "new-chat"): void; (event: "new-task"): void }>();
const route = useRoute();
const router = useRouter();

const moreOpen = ref(false);
const projectsOpen = ref(true);

// The rail's primary entries. Each one goes to a real page: the work bench,
// the recurring-turn scheduler, the plugin and skill hub, the knowledge bases.
const primaryItems = computed(() => [
  { key: "newTask", label: t("New task"), icon: "✎", action: "new-task" as const },
  { key: "newChat", label: t("New conversation"), icon: "＋", action: "new-chat" as const },
  { key: "workbench", label: t("Workbench"), icon: "▦", to: "/" },
  { key: "scheduled", label: t("Scheduled tasks"), icon: "◷", to: "/scheduled" },
  { key: "plugins", label: t("Plugins · Skills · Partners"), icon: "⧉", to: "/integrations" },
  { key: "knowledge", label: t("Knowledge"), icon: "▤", to: "/knowledge" },
]);

const moreItems = [
  { to: "/workflows", label: t("Workflow canvas") },
  { to: "/agents", label: t("Agent Studio") },
  { to: "/models", label: t("Model Manager") },
  { to: "/marketplace", label: t("Agent marketplace") },
  { to: "/registry", label: t("Image registry") },
  { to: "/infrastructure", label: t("Metal3 fleet") },
  { to: "/cloud-vms", label: t("Cloud VMs") },
  { to: "/governance", label: t("Governance") },
  { to: "/audit", label: t("Audit and activity") },
  { to: "/organization", label: t("Organization") },
  { to: "/docs", label: t("Docs and matrix") },
];

function isActive(path: string): boolean {
  if (path === "/") return route.path === "/";
  return route.path === path || route.path.startsWith(`${path}/`);
}

async function chooseThread(threadId: string): Promise<void> {
  setActiveThread(threadId);
  if (route.path !== "/chat") await router.push("/chat");
}

async function switchProject(projectId: string): Promise<void> {
  await selectProject(projectId);
  if (route.path !== "/chat") await router.push("/chat");
}

async function switchTenant(tenantId: string): Promise<void> {
  await selectTenant(tenantId);
  await router.push("/chat");
}

function openPrimary(item: { action?: "new-task" | "new-chat"; to?: string }): void {
  if (item.action === "new-chat") {
    emit("new-chat");
    return;
  }
  if (item.action === "new-task") {
    emit("new-task");
    return;
  }
  if (item.to) void router.push(item.to);
}

function relative(time: string): string {
  const delta = Date.now() - new Date(time).getTime();
  const minutes = Math.round(delta / 60000);
  if (minutes < 1) return t("just now");
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h`;
  return `${Math.round(hours / 24)}d`;
}

onMounted(() => {
  void loadWorkspace();
});
</script>

<template>
  <aside class="workspace-rail">
    <div class="rail-scroll">
      <nav class="rail-primary">
        <button
          v-for="item in primaryItems"
          :key="item.key"
          class="rail-item"
          :class="{ active: item.to && isActive(item.to) }"
          type="button"
          @click="openPrimary(item)"
        >
          <span class="rail-icon" aria-hidden="true">{{ item.icon }}</span>
          <span>{{ item.label }}</span>
        </button>
        <el-popover v-model:visible="moreOpen" placement="right-start" :width="220" trigger="click">
          <template #reference>
            <button class="rail-item" type="button">
              <span class="rail-icon" aria-hidden="true">•••</span>
              <span>{{ t("More") }}</span>
            </button>
          </template>
          <div class="rail-more">
            <router-link v-for="item in moreItems" :key="item.to" :to="item.to" @click="moreOpen = false">
              {{ item.label }}
            </router-link>
          </div>
        </el-popover>
      </nav>

      <section class="rail-scope">
        <div class="rail-scope-head">
          <span>{{ t("Projects and recent chats") }}</span>
          <button class="rail-mini" type="button" @click="projectsOpen = !projectsOpen">
            {{ projectsOpen ? t("Hide") : t("Show") }}
          </button>
        </div>
        <el-select
          v-if="tenants.length > 1"
          :model-value="activeTenantId"
          size="small"
          class="rail-select"
          @change="(value: string) => switchTenant(value)"
        >
          <el-option v-for="tenant in tenants" :key="tenant.id" :label="tenant.name" :value="tenant.id" />
        </el-select>
        <el-select
          :model-value="activeProjectId"
          size="small"
          class="rail-select"
          :placeholder="t('Choose a project')"
          @change="(value: string) => switchProject(value)"
        >
          <el-option v-for="project in projects" :key="project.id" :label="project.name" :value="project.id" />
        </el-select>

        <ul v-if="projectsOpen" class="rail-threads">
          <li v-if="!threads.length" class="rail-empty">{{ t("No conversation yet") }}</li>
          <li v-for="thread in threads" :key="thread.id">
            <button
              type="button"
              class="rail-thread"
              :class="{ active: thread.id === activeThreadId }"
              @click="chooseThread(thread.id)"
            >
              <span class="rail-thread-title">{{ thread.title }}</span>
              <span class="rail-thread-meta">
                <span v-if="thread.last_message">{{ relative(thread.last_message.created_at) }}</span>
                <span v-if="thread.execution_target === 'local'" class="rail-env">⌂</span>
              </span>
            </button>
          </li>
        </ul>
        <button v-if="threads.length" class="rail-mini" type="button" @click="refreshThreads">
          {{ t("Refresh") }}
        </button>
      </section>
    </div>

    <div class="rail-account">
      <router-link class="rail-user" to="/settings/account">
        <span class="rail-avatar">{{ (props.user || "U").slice(0, 1).toUpperCase() }}</span>
        <span>
          <strong>{{ props.user }}</strong>
          <small>{{ t("Account and settings") }}</small>
        </span>
      </router-link>
    </div>
  </aside>
</template>

<style scoped>
.workspace-rail {
  display: flex;
  flex-direction: column;
  width: 248px;
  flex: 0 0 248px;
  min-height: 0;
  height: 100%;
  border-right: 1px solid #e2eaf1;
  background: #f7fafc;
}
.rail-scroll { flex: 1; min-height: 0; overflow-y: auto; padding: 14px 10px 10px; }
.rail-primary { display: grid; gap: 2px; }
.rail-item {
  display: flex;
  align-items: center;
  gap: 9px;
  width: 100%;
  padding: 8px 10px;
  border: none;
  border-radius: 9px;
  background: transparent;
  color: #23303a;
  font: inherit;
  font-size: 12.5px;
  text-align: left;
  cursor: pointer;
}
.rail-item:hover { background: #eaf2f7; }
.rail-item.active { background: #e3f2ec; color: #0a7f58; font-weight: 600; }
.rail-icon { width: 16px; color: #6d7f8c; font-size: 12px; text-align: center; }
.rail-item.active .rail-icon { color: #0a7f58; }
.rail-more { display: grid; gap: 2px; }
.rail-more a { padding: 6px 8px; border-radius: 6px; color: #23303a; font-size: 12px; }
.rail-more a:hover { background: #eef4f8; }
.rail-scope { margin-top: 14px; padding-top: 12px; border-top: 1px solid #e2eaf1; display: grid; gap: 8px; }
.rail-scope-head { display: flex; align-items: center; justify-content: space-between; color: #6d7f8c; font-size: 10px; text-transform: uppercase; letter-spacing: 0.07em; }
.rail-mini { border: none; background: none; color: #0a7f58; font: inherit; font-size: 10px; cursor: pointer; padding: 0; }
.rail-select { width: 100%; }
.rail-threads { list-style: none; margin: 0; padding: 0; display: grid; gap: 1px; max-height: 42vh; overflow-y: auto; }
.rail-empty { padding: 8px 10px; color: #8296a4; font-size: 11px; }
.rail-thread {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  width: 100%;
  padding: 7px 10px;
  border: none;
  border-radius: 8px;
  background: transparent;
  color: #23303a;
  font: inherit;
  font-size: 11.5px;
  text-align: left;
  cursor: pointer;
}
.rail-thread:hover { background: #eaf2f7; }
.rail-thread.active { background: #e3f2ec; color: #0a7f58; }
.rail-thread-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rail-thread-meta { display: flex; gap: 4px; color: #8296a4; font-size: 10px; flex: 0 0 auto; }
.rail-env { color: #0a7f58; }
.rail-account { padding: 10px; border-top: 1px solid #e2eaf1; }
.rail-user { display: flex; align-items: center; gap: 9px; padding: 6px; border-radius: 9px; color: #23303a; }
.rail-user:hover { background: #eaf2f7; }
.rail-avatar {
  display: grid;
  place-items: center;
  width: 26px;
  height: 26px;
  border-radius: 50%;
  background: #0a7f58;
  color: #fff;
  font-size: 11px;
  font-weight: 600;
}
.rail-user strong { display: block; font-size: 12px; }
.rail-user small { color: #6d7f8c; font-size: 10px; }
@media (max-width: 900px) {
  .workspace-rail { width: 100%; flex: 1 1 auto; border-right: none; }
}
</style>
