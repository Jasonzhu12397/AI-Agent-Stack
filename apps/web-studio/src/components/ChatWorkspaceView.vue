<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { t } from "../i18n";
import { renderMarkdown } from "../services/markdown";
import {
  chatAttachmentUrl,
  createChatThread,
  deleteChatAttachment,
  getChatAttachments,
  getChatContext,
  installChatPlugin,
  getChatThread,
  getKnowledgeBases,
  getKnowledgeDocuments,
  sendChatMessage,
  updateChatThread,
  uploadChatAttachment,
  type ChatAttachment,
  type ChatCatalogEntry,
  type ChatContext,
  type ChatMessage,
  type ChatThread,
  type ChatToolCall,
  type KnowledgeBase,
  type KnowledgeDocument,
} from "../services/api";
import {
  activeProjectId,
  activeTenantId,
  activeThreadId,
  newChatMode,
  newChatSignal,
  projects,
  refreshThreads,
  selectProject,
  setActiveThread,
  threads,
} from "../state/workspace";

const router = useRouter();

const context = ref<ChatContext | null>(null);
const thread = ref<(ChatThread & { attachments?: ChatAttachment[] }) | null>(null);
const messages = ref<ChatMessage[]>([]);
const attachments = ref<ChatAttachment[]>([]);
const draft = ref("");
const sending = ref(false);
const error = ref("");
const notice = ref("");
const startedAt = ref(0);
const elapsed = ref(0);
const detailOpen = ref(false);
const abortController = ref<AbortController | null>(null);

const selectedSkills = ref<string[]>([]);
const installingPlugin = ref("");
const toolTag = (kind: string) =>
  kind === "plugin" ? "PLUGIN" : kind === "local" ? "LOCAL" : "PLATFORM";

const selectedPlugins = ref<string[]>([]);
async function installCatalogPlugin(entry: ChatCatalogEntry): Promise<void> {
  if (!activeTenantId.value || !activeProjectId.value) return;
  installingPlugin.value = entry.key;
  try {
    const installation = await installChatPlugin(
      activeTenantId.value,
      activeProjectId.value,
      entry.key,
      entry.capabilities,
    );
    await loadContext();
    selectedPlugins.value = toggleIn(selectedPlugins.value, installation.id);
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : String(caught);
  } finally {
    installingPlugin.value = "";
  }
}
const selectedAttachments = ref<string[]>([]);
const selectedDocuments = ref<string[]>([]);
const selectedModelId = ref("");
const knowledgeDocuments = ref<{ id: string; title: string; base: string }[]>([]);

// Menu visibility, one popover per composer control, the way the reference
// workspace opens each selector in place.
const addOpen = ref(false);
const skillOpen = ref(false);
const pluginOpen = ref(false);
const envOpen = ref(false);
const effortOpen = ref(false);
const projectOpen = ref(false);
const goalOpen = ref(false);
const goalDraft = ref("");
const knowledgeOpen = ref(false);

const fileInput = ref<HTMLInputElement | null>(null);
const transcript = ref<HTMLElement | null>(null);
const composer = ref<HTMLTextAreaElement | null>(null);

const hasWorkspace = computed(() => Boolean(activeTenantId.value && activeProjectId.value));
const title = computed(() => thread.value?.title ?? t("New conversation"));
const target = computed<"cloud" | "local">(() => thread.value?.execution_target ?? "cloud");
const effort = computed<"auto" | "low">(() => thread.value?.effort ?? "auto");
const planFirst = computed(() => thread.value?.plan_first ?? false);
const goal = computed(() => thread.value?.goal ?? "");
const models = computed(() => (context.value?.models ?? []).filter((item) => item.ready !== false));
const activeModel = computed(
  () => models.value.find((item) => item.id === selectedModelId.value) ?? models.value[0] ?? null,
);
const projectName = computed(
  () => projects.value.find((item) => item.id === activeProjectId.value)?.name ?? "",
);
const canSend = computed(() => draft.value.trim().length > 0 && !sending.value);
const usageTotals = computed(() => {
  let input = 0;
  let output = 0;
  for (const message of messages.value) {
    const usage = message.usage ?? {};
    input += Number(usage.input_tokens ?? 0);
    output += Number(usage.output_tokens ?? 0);
  }
  return { input, output };
});

function bubbleText(message: ChatMessage): string {
  if (message.error_code) return message.content || message.error_code;
  return message.content;
}

function rendered(message: ChatMessage): string {
  if (message.role === "user" || message.error_code) return bubbleText(message);
  return renderMarkdown(bubbleText(message));
}

function scrollToLatest(): void {
  const el = transcript.value;
  if (el) el.scrollTop = el.scrollHeight;
}

async function loadContext(): Promise<void> {
  if (!hasWorkspace.value) return;
  try {
    context.value = await getChatContext(activeTenantId.value, activeProjectId.value);
    const previousModel = selectedModelId.value;
    selectedModelId.value = models.value.some((item) => item.id === previousModel)
      ? previousModel
      : models.value[0]?.id ?? "";
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause);
  }
}

async function loadThread(threadId: string): Promise<void> {
  if (!threadId) {
    thread.value = null;
    messages.value = [];
    attachments.value = [];
    selectedAttachments.value = [];
    return;
  }
  try {
    const loaded = await getChatThread(threadId);
    thread.value = loaded;
    messages.value = loaded.messages ?? [];
    attachments.value = loaded.attachments ?? [];
    selectedModelId.value = loaded.model_config_id ?? selectedModelId.value;
    selectedAttachments.value = (loaded.attachments ?? []).map((item) => item.id);
    await nextTick();
    scrollToLatest();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause);
  }
}

async function ensureThread(): Promise<string> {
  if (thread.value?.id) return thread.value.id;
  if (!hasWorkspace.value) throw new Error(t("Choose a project first"));
  const created = await createChatThread(activeTenantId.value, activeProjectId.value, {
    model_config_id: selectedModelId.value || null,
  });
  setActiveThread(created.id);
  thread.value = created;
  await refreshThreads();
  return created.id;
}

async function patchThread(payload: Partial<ChatThread>): Promise<void> {
  try {
    const threadId = await ensureThread();
    const updated = await updateChatThread(threadId, payload);
    thread.value = { ...(thread.value as ChatThread), ...updated };
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause);
  }
}

function setTarget(value: "cloud" | "local"): void {
  envOpen.value = false;
  void patchThread({ execution_target: value });
}

function setEffort(value: "auto" | "low"): void {
  effortOpen.value = false;
  void patchThread({ effort: value });
}

function togglePlan(): void {
  void patchThread({ plan_first: !planFirst.value });
}

function saveGoal(value: string): void {
  goalOpen.value = false;
  goalDraft.value = "";
  void patchThread({ goal: value.trim() });
}

async function pickProject(projectId: string): Promise<void> {
  projectOpen.value = false;
  await selectProject(projectId);
  await loadContext();
  await loadThread(activeThreadId.value);
}

async function newConversation(mode: "chat" | "task" = "chat"): Promise<void> {
  if (!hasWorkspace.value) {
    error.value = t("Choose a project first");
    return;
  }
  try {
    const created = await createChatThread(activeTenantId.value, activeProjectId.value, {
      model_config_id: selectedModelId.value || null,
    });
    if (mode === "task") {
      const updated = await updateChatThread(created.id, { plan_first: true });
      thread.value = updated;
    } else {
      thread.value = created;
    }
    messages.value = [];
    attachments.value = [];
    selectedAttachments.value = [];
    setActiveThread(created.id);
    await refreshThreads();
    await nextTick();
    composer.value?.focus();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause);
  }
}

async function uploadFiles(files: FileList | null): Promise<void> {
  if (!files || !files.length) return;
  addOpen.value = false;
  try {
    const threadId = await ensureThread();
    for (const file of Array.from(files).slice(0, 5)) {
      const stored = await uploadChatAttachment(threadId, file);
      attachments.value = [...attachments.value, stored];
      selectedAttachments.value = [...selectedAttachments.value, stored.id];
    }
    notice.value = t("Files attached to this conversation");
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause);
  } finally {
    if (fileInput.value) fileInput.value.value = "";
  }
}

async function removeAttachment(id: string): Promise<void> {
  try {
    await deleteChatAttachment(id);
    attachments.value = attachments.value.filter((item) => item.id !== id);
    selectedAttachments.value = selectedAttachments.value.filter((item) => item !== id);
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause);
  }
}

async function openKnowledge(): Promise<void> {
  addOpen.value = false;
  knowledgeOpen.value = true;
  if (knowledgeDocuments.value.length) return;
  try {
    const page = await getKnowledgeBases(activeTenantId.value, activeProjectId.value);
    const bases: KnowledgeBase[] = page.results ?? [];
    const docs: { id: string; title: string; base: string }[] = [];
    for (const base of bases) {
      const documents = await getKnowledgeDocuments(base.id);
      for (const doc of documents.results ?? []) {
        docs.push({ id: doc.id, title: doc.title, base: base.name });
      }
    }
    knowledgeDocuments.value = docs;
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause);
  }
}

function toggleIn(list: string[], value: string): string[] {
  return list.includes(value) ? list.filter((item) => item !== value) : [...list, value];
}

async function send(): Promise<void> {
  if (!canSend.value) return;
  if (!hasWorkspace.value) {
    error.value = t("Choose a project first");
    return;
  }
  const content = draft.value.trim();
  draft.value = "";
  error.value = "";
  notice.value = "";
  sending.value = true;
  startedAt.value = Date.now();
  elapsed.value = 0;
  const ticker = window.setInterval(() => {
    elapsed.value = Math.round((Date.now() - startedAt.value) / 1000);
  }, 1000);
  const controller = new AbortController();
  abortController.value = controller;
  try {
    const threadId = await ensureThread();
    const result = await sendChatMessage(
      threadId,
      {
        content,
        model_config_id: selectedModelId.value || null,
        context: {
          skills: selectedSkills.value,
          plugins: selectedPlugins.value,
          attachments: selectedAttachments.value,
          knowledge_documents: selectedDocuments.value,
        },
      },
      { signal: controller.signal },
    );
    messages.value = [...messages.value, result.user, result.assistant];
    await nextTick();
    scrollToLatest();
    await loadThread(threadId);
    await refreshThreads();
  } catch (cause) {
    if (controller.signal.aborted) {
      notice.value = t("Generation stopped from this console");
      if (thread.value?.id) await loadThread(thread.value.id);
    } else {
      error.value = cause instanceof Error ? cause.message : String(cause);
    }
  } finally {
    window.clearInterval(ticker);
    sending.value = false;
    abortController.value = null;
  }
}

function stop(): void {
  abortController.value?.abort();
}

function footerStatus(message: ChatMessage): string {
  const usage = message.usage ?? {};
  const parts: string[] = [t("Control Agent")];
  parts.push(usage.execution_target === "local" ? t("Local machine") : t("Cloud computer"));
  if (usage.effort) parts.push(usage.effort === "low" ? t("Reasoning low") : t("Reasoning auto"));
  if (usage.model) parts.push(String(usage.model));
  if (usage.latency_ms) parts.push(`${Math.round(Number(usage.latency_ms) / 100) / 10}s`);
  return parts.join(" · ");
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    void send();
  }
}

function relativeTime(value: string | null): string {
  if (!value) return t("never");
  const delta = Date.now() - new Date(value).getTime();
  const minutes = Math.round(delta / 60000);
  if (minutes < 1) return t("just now");
  if (minutes < 60) return `${minutes} ${t("min")}`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} ${t("h")}`;
  return `${Math.round(hours / 24)} ${t("days")}`;
}

watch(activeThreadId, (value) => void loadThread(value));
watch([activeTenantId, activeProjectId], () => {
  void loadContext();
  void loadThread(activeThreadId.value);
});
watch(newChatSignal, () => void newConversation(newChatMode.value));

onMounted(async () => {
  await loadContext();
  if (activeThreadId.value) await loadThread(activeThreadId.value);
});
</script>

<template>
  <div class="chat-workspace">
    <header class="chat-top">
      <div class="chat-title">
        <span class="chat-title-text">{{ title }}</span>
        <el-tag size="small" effect="plain" :type="target === 'local' ? 'info' : 'success'">
          {{ target === "local" ? t("Local machine") : t("Cloud computer") }}
        </el-tag>
        <el-tag v-if="planFirst" size="small" effect="plain" type="warning">{{ t("Plan first") }}</el-tag>
        <el-tag v-if="goal" size="small" effect="plain">{{ t("Goal set") }}</el-tag>
      </div>
      <div class="chat-top-actions">
        <span v-if="activeModel" class="chat-model">{{ activeModel.display_name }}</span>
        <el-button size="small" text @click="detailOpen = true">{{ t("Task details") }}</el-button>
        <el-button size="small" plain @click="newConversation('chat')">＋ {{ t("New conversation") }}</el-button>
      </div>
    </header>

    <el-alert v-if="error" :title="error" type="error" show-icon :closable="true" @close="error = ''" />
    <el-alert v-if="notice" :title="notice" type="success" :closable="true" @close="notice = ''" />

    <section ref="transcript" class="chat-transcript">
      <div v-if="!messages.length" class="chat-empty">
        <p class="eyebrow">CONTROL AGENT</p>
        <h2>{{ t("What should this workspace do?") }}</h2>
        <p class="chat-empty-hint">
          {{ t("Cloud Control plans the work, EPH bootstraps the cluster, Target takes over, and every image comes from the registry.") }}
        </p>
        <div class="chat-suggestions">
          <button
            v-for="item in [
              t('Inspect the pending enrollments'),
              t('Plan a bare-metal cluster'),
              t('Check which images are ready'),
              t('Summarise the last scheduled run'),
            ]"
            :key="item"
            type="button"
            @click="draft = item"
          >
            {{ item }}
          </button>
        </div>
      </div>

      <article
        v-for="message in messages"
        :key="message.id"
        class="chat-message"
        :class="message.role"
      >
        <div class="chat-avatar" :class="message.role">
          {{ message.role === "user" ? (message.author ?? "U").slice(0, 1).toUpperCase() : "A" }}
        </div>
        <div class="chat-body">
          <div v-if="message.role === 'assistant'" class="chat-body-head">
            <strong>{{ t("Control Agent") }}</strong>
            <span>{{ relativeTime(message.created_at) }}</span>
          </div>
          <div
            v-if="message.role === 'assistant' && !message.error_code"
            class="chat-markdown"
            v-html="rendered(message)"
          />
          <div v-else class="chat-plain" :class="{ failure: message.error_code }">{{ rendered(message) }}</div>

          <div v-if="message.role === 'user' && message.context?.attachments?.length" class="chat-attachments">
            <el-tag v-for="name in message.context.attachments" :key="name" size="small" effect="plain">
              📎 {{ name }}
            </el-tag>
          </div>
          <ul v-if="message.role === 'assistant' && message.tool_calls?.length" class="chat-tools">
            <li v-for="(call, index) in message.tool_calls" :key="index" :class="{ failed: !call.ok }">
              <span class="chat-tool-tag">{{ toolTag(call.kind) }}</span>
              <strong>{{ call.identifier }}</strong>
              <span v-if="call.code" class="chat-tool-code">{{ call.code }}</span>
              <span class="chat-tool-meta">{{ call.duration_ms }}ms</span>
            </li>
          </ul>
          <p
            v-if="message.role === 'assistant' && message.usage?.tools_note"
            class="chat-tool-note"
          >
            ⚠ {{ t("This model does not support tool calls; the scope above was passed as context only.") }}
          </p>
          <div
            v-if="message.role === 'assistant' && (message.context?.skills?.length || message.context?.plugins?.length)"
            class="chat-scope"
          >
            <el-tag v-for="skill in message.context.skills ?? []" :key="skill" size="small" type="success" effect="plain">
              {{ skill }}
            </el-tag>
          </div>
          <p v-if="message.role === 'assistant' && !message.error_code" class="chat-status">
            {{ footerStatus(message) }}
          </p>
        </div>
      </article>

      <article v-if="sending" class="chat-message assistant">
        <div class="chat-avatar assistant">A</div>
        <div class="chat-body">
          <div class="chat-body-head">
            <strong>{{ t("Control Agent") }}</strong>
            <span>{{ target === "local" ? t("Local machine") : t("Cloud computer") }}</span>
          </div>
          <div class="chat-running">
            <span class="chat-dots"><i /><i /><i /></span>
            {{ t("Working") }} · {{ effort === "low" ? t("Reasoning low") : t("Reasoning auto") }} · {{ elapsed }}s
          </div>
        </div>
      </article>
    </section>

    <div class="chat-composer">
      <div v-if="attachments.length || goal || planFirst || selectedDocuments.length" class="chat-pending">
        <el-tag
          v-for="item in attachments"
          :key="item.id"
          size="small"
          effect="plain"
          :type="selectedAttachments.includes(item.id) ? 'success' : 'info'"
          closable
          @close="removeAttachment(item.id)"
          @click="selectedAttachments = toggleIn(selectedAttachments, item.id)"
        >
          📎 {{ item.filename }}
        </el-tag>
        <el-tag
          v-for="id in selectedDocuments"
          :key="id"
          size="small"
          type="warning"
          effect="plain"
          closable
          @close="selectedDocuments = selectedDocuments.filter((entry) => entry !== id)"
        >
          📚 {{ knowledgeDocuments.find((doc) => doc.id === id)?.title ?? id }}
        </el-tag>
        <el-tag v-if="goal" size="small" effect="plain" closable @close="patchThread({ goal: '' })">
          ◎ {{ goal }}
        </el-tag>
        <el-tag v-if="planFirst" size="small" type="warning" effect="plain" closable @close="togglePlan()">
          ☰ {{ t("Plan first") }}
        </el-tag>
      </div>

      <div class="chat-input-shell">
        <textarea
          ref="composer"
          v-model="draft"
          class="chat-input"
          rows="1"
          :placeholder="t('Describe the task, or attach a file and ask about it')"
          @keydown="onKeydown"
        />
        <div class="chat-controls">
          <el-popover v-model:visible="addOpen" placement="top-start" :width="240" trigger="click">
            <template #reference>
              <button class="chat-chip" type="button">＋</button>
            </template>
            <div class="chat-menu">
              <button type="button" @click="fileInput?.click()">{{ t("Upload a file or an image") }}</button>
              <button type="button" @click="openKnowledge">{{ t("Pick a knowledge file") }}</button>
              <button type="button" :class="{ on: skillOpen }" @click="addOpen = false; skillOpen = true">
                {{ t("Skills") }}
              </button>
              <button type="button" @click="addOpen = false; pluginOpen = true">{{ t("Plugins") }}</button>
              <button type="button" @click="addOpen = false; goalOpen = true">{{ t("Goal") }}</button>
              <button type="button" @click="togglePlan()">
                {{ planFirst ? t("Plan mode on") : t("Plan mode") }}
              </button>
              <button type="button" @click="detailOpen = true">{{ t("Creative material") }}</button>
              <button type="button" @click="addOpen = false; envOpen = true">
                {{ t("Cloud or local machine") }}
              </button>
            </div>
          </el-popover>
          <input
            ref="fileInput"
            type="file"
            multiple
            class="chat-file"
            @change="uploadFiles(($event.target as HTMLInputElement).files)"
          />

          <el-popover v-model:visible="envOpen" placement="top-start" :width="260" trigger="click">
            <template #reference>
              <button class="chat-chip" type="button">
                {{ target === "local" ? "⌂" : "☁" }} {{ target === "local" ? t("Local machine") : t("Cloud computer") }}
              </button>
            </template>
            <div class="chat-menu">
              <button type="button" :class="{ on: target === 'cloud' }" @click="setTarget('cloud')">
                ☁ {{ t("Cloud computer") }} — {{ t("Cloud Control, EPH and Target agents") }}
              </button>
              <button type="button" :class="{ on: target === 'local' }" @click="setTarget('local')">
                ⌂ {{ t("Local machine") }} — {{ t("run against the local workspace") }}
              </button>
            </div>
          </el-popover>

          <el-popover v-model:visible="projectOpen" placement="top-start" :width="220" trigger="click">
            <template #reference>
              <button class="chat-chip" type="button">▤ {{ projectName || t("Project") }}</button>
            </template>
            <div class="chat-menu">
              <button
                v-for="project in projects"
                :key="project.id"
                type="button"
                :class="{ on: project.id === activeProjectId }"
                @click="pickProject(project.id)"
              >
                {{ project.name }}
              </button>
            </div>
          </el-popover>

          <el-popover v-model:visible="skillOpen" placement="top-start" :width="280" trigger="click">
            <template #reference>
              <button class="chat-chip" type="button">
                ✦ {{ t("Skills") }}<span v-if="selectedSkills.length"> · {{ selectedSkills.length }}</span>
              </button>
            </template>
            <div class="chat-menu chat-menu-scroll">
              <p class="chat-menu-label">{{ t("Skill menu") }}</p>
              <button
                v-for="skill in context?.skills ?? []"
                :key="skill.id"
                type="button"
                :class="{ on: selectedSkills.includes(skill.id) }"
                @click="selectedSkills = toggleIn(selectedSkills, skill.id)"
              >
                {{ skill.name }}
              </button>
              <p v-if="!(context?.skills ?? []).length" class="chat-menu-empty">{{ t("No skill granted in this project") }}</p>
              <p class="chat-menu-label">{{ t("Manage") }}</p>
              <button type="button" @click="skillOpen = false; router.push('/integrations')">{{ t("Skill management") }}</button>
              <button type="button" @click="detailOpen = true">{{ t("Usage") }} · {{ usageTotals.input + usageTotals.output }} {{ t("tokens") }}</button>
              <button type="button" @click="goalDraft = goal; goalOpen = true; skillOpen = false">{{ t("Goal") }}</button>
              <button type="button" @click="togglePlan()">{{ planFirst ? t("Plan mode on") : t("Plan mode") }}</button>
              <button type="button" @click="skillOpen = false; router.push('/workflows')">{{ t("Open the canvas") }}</button>
              <a class="chat-menu-link" href="https://github.com/Jasonzhu12397/AI-Agent-Stack/issues" target="_blank" rel="noreferrer">
                {{ t("Feedback") }}
              </a>
            </div>
          </el-popover>

          <el-popover v-model:visible="pluginOpen" placement="top-start" :width="280" trigger="click">
            <template #reference>
              <button class="chat-chip" type="button">
                ⧉ {{ t("Plugins") }}<span v-if="selectedPlugins.length"> · {{ selectedPlugins.length }}</span>
              </button>
            </template>
            <div class="chat-menu chat-menu-scroll">
              <p class="chat-menu-label">{{ t("Plugin menu") }}</p>
              <button
                v-for="plugin in context?.plugins ?? []"
                :key="plugin.id"
                type="button"
                :class="{ on: selectedPlugins.includes(plugin.id), warn: !plugin.credential_configured }"
                @click="selectedPlugins = toggleIn(selectedPlugins, plugin.id)"
              >
                {{ plugin.name }}
                <span v-if="!plugin.credential_configured"> · {{ t("authorize") }}</span>
              </button>
              <p v-if="!(context?.plugins ?? []).length" class="chat-menu-empty">{{ t("No plugin installed in this project") }}</p>
              <template v-if="(context?.catalog ?? []).length">
                <p class="chat-menu-label">{{ t("Available connectors") }}</p>
                <template v-for="entry in context?.catalog ?? []" :key="entry.key">
                  <button
                    v-if="entry.enabled"
                    type="button"
                    :disabled="installingPlugin === entry.key"
                    @click="installCatalogPlugin(entry)"
                  >
                    {{ entry.name }} · {{ t("install") }}
                  </button>
                  <p v-else class="chat-menu-hint">
                    <strong>{{ entry.name }}</strong> · {{ entry.description }}
                  </p>
                </template>
              </template>
              <button type="button" @click="pluginOpen = false; router.push('/integrations')">{{ t("Add a plugin") }}</button>
              <button type="button" @click="pluginOpen = false; router.push('/integrations')">{{ t("Manage plugins") }}</button>
            </div>
          </el-popover>

          <el-popover v-model:visible="effortOpen" placement="top-start" :width="240" trigger="click">
            <template #reference>
              <button class="chat-chip" type="button">
                ⚡ {{ effort === "low" ? t("Low") : t("Auto") }}
              </button>
            </template>
            <div class="chat-menu">
              <button type="button" :class="{ on: effort === 'auto' }" @click="setEffort('auto')">
                {{ t("Auto") }} — {{ t("the model decides the depth") }}
              </button>
              <button type="button" :class="{ on: effort === 'low' }" @click="setEffort('low')">
                {{ t("Low") }} — {{ t("short answer, smaller token cap") }}
              </button>
              <p class="chat-menu-label">{{ t("Model") }}</p>
              <button
                v-for="model in models"
                :key="model.id"
                type="button"
                :class="{ on: model.id === selectedModelId }"
                @click="selectedModelId = model.id; effortOpen = false"
              >
                {{ model.display_name }} · {{ model.provider_name }}
              </button>
            </div>
          </el-popover>

          <span class="chat-controls-spacer" />
          <button v-if="sending" class="chat-send stop" type="button" @click="stop">
            ■ {{ t("Stop") }}
          </button>
          <button v-else class="chat-send" type="button" :disabled="!canSend" @click="send">
            ↑ {{ t("Send") }}
          </button>
        </div>
      </div>
      <p class="chat-hint">{{ t("Enter sends, Shift + Enter starts a new line") }}</p>
    </div>

    <el-drawer v-model="detailOpen" :title="t('Task details')" size="420px">
      <div class="chat-detail">
        <section>
          <h4>{{ t("Scope") }}</h4>
          <p>{{ t("Project") }}: {{ projectName || t("none") }}</p>
          <p>{{ t("Environment") }}: {{ target === "local" ? t("Local machine") : t("Cloud computer") }}</p>
          <p>{{ t("Reasoning") }}: {{ effort }}</p>
          <p>{{ t("Plan first") }}: {{ planFirst ? t("on") : t("off") }}</p>
          <p>{{ t("Goal") }}: {{ goal || t("not set") }}</p>
        </section>
        <section>
          <h4>{{ t("Files in this conversation") }}</h4>
          <p v-if="!attachments.length" class="chat-detail-empty">{{ t("No file attached") }}</p>
          <div v-for="item in attachments" :key="item.id" class="chat-detail-file">
            <a :href="chatAttachmentUrl(item.id)" target="_blank" rel="noreferrer">{{ item.filename }}</a>
            <span>{{ Math.round(item.size_bytes / 1024) }} KB</span>
          </div>
        </section>
        <section>
          <h4>{{ t("Usage") }}</h4>
          <p>{{ t("Input tokens") }}: {{ usageTotals.input }}</p>
          <p>{{ t("Output tokens") }}: {{ usageTotals.output }}</p>
          <p>{{ t("Turns") }}: {{ messages.length }}</p>
        </section>
        <section>
          <h4>{{ t("Skills and plugins in scope") }}</h4>
          <p>{{ selectedSkills.join(", ") || t("none") }}</p>
          <p>{{ (context?.plugins ?? []).filter((plugin) => selectedPlugins.includes(plugin.id)).map((plugin) => plugin.name).join(", ") || t("none") }}</p>
        </section>
      </div>
    </el-drawer>

    <el-dialog v-model="knowledgeOpen" :title="t('Pick a knowledge file')" width="520px">
      <p v-if="!knowledgeDocuments.length" class="chat-detail-empty">{{ t("No document in this project") }}</p>
      <div class="chat-menu chat-menu-scroll">
        <button
          v-for="doc in knowledgeDocuments"
          :key="doc.id"
          type="button"
          :class="{ on: selectedDocuments.includes(doc.id) }"
          @click="selectedDocuments = toggleIn(selectedDocuments, doc.id)"
        >
          {{ doc.title }} <span class="chat-menu-muted">· {{ doc.base }}</span>
        </button>
      </div>
      <template #footer>
        <el-button @click="knowledgeOpen = false">{{ t("Done") }}</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="goalOpen" :title="t('Goal of this task')" width="560px">
      <el-input
        v-model="goalDraft"
        type="textarea"
        :rows="3"
        :placeholder="t('What should be true when this work is done?')"
      />
      <template #footer>
        <el-button @click="goalOpen = false">{{ t("Cancel") }}</el-button>
        <el-button type="primary" @click="saveGoal(goalDraft)">{{ t("Save") }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.chat-workspace { display: flex; flex-direction: column; height: 100%; min-height: 0; }
.chat-top { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 10px 18px; border-bottom: 1px solid #e6edf3; background: #fff; }
.chat-title { display: flex; align-items: center; gap: 8px; min-width: 0; }
.chat-title-text { font-size: 14px; font-weight: 600; color: #16222b; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 46vw; }
.chat-top-actions { display: flex; align-items: center; gap: 8px; flex: 0 0 auto; }
.chat-model { color: #7b8c99; font-size: 11px; }
.chat-transcript { flex: 1; min-height: 0; overflow-y: auto; padding: 20px 18px 8px; display: flex; flex-direction: column; gap: 18px; }
.chat-empty { max-width: 620px; margin: auto; text-align: center; display: grid; gap: 10px; align-content: center; }
.chat-empty h2 { margin: 0; font-size: 22px; color: #16222b; }
.chat-empty-hint { margin: 0 auto; max-width: 520px; color: #6c7d8a; font-size: 12px; line-height: 1.6; }
.chat-suggestions { display: flex; flex-wrap: wrap; gap: 8px; justify-content: center; margin-top: 6px; }
.chat-suggestions button { padding: 7px 12px; border: 1px solid #dbe5ee; border-radius: 999px; background: #fff; color: #22303a; font: inherit; font-size: 11.5px; cursor: pointer; }
.chat-suggestions button:hover { border-color: #0a7f58; color: #0a7f58; }
.chat-message { display: flex; gap: 10px; max-width: 860px; width: 100%; margin: 0 auto; }
.chat-message.user { flex-direction: row-reverse; }
.chat-avatar { display: grid; place-items: center; width: 26px; height: 26px; flex: 0 0 26px; border-radius: 50%; font-size: 11px; font-weight: 600; }
.chat-avatar.assistant { background: #e3f2ec; color: #0a7f58; }
.chat-avatar.user { background: #1e5c9e; color: #fff; }
.chat-body { min-width: 0; flex: 1; }
.chat-message.user .chat-body { display: flex; flex-direction: column; align-items: flex-end; }
.chat-body-head { display: flex; align-items: baseline; gap: 8px; margin-bottom: 4px; }
.chat-body-head strong { font-size: 12px; color: #16222b; }
.chat-body-head span { color: #8a9aa6; font-size: 10px; }
.chat-plain { padding: 9px 13px; border-radius: 12px; background: #eaf4f1; color: #16222b; font-size: 13px; line-height: 1.7; white-space: pre-wrap; }
.chat-message.assistant .chat-plain { background: #f6f9fb; }
.chat-plain.failure { background: #fdeeee; color: #a3231f; }
.chat-markdown { padding: 2px 0; color: #1a2630; font-size: 13px; line-height: 1.75; overflow-wrap: anywhere; }
.chat-markdown :deep(pre) { padding: 12px; overflow-x: auto; background: #0f1a23; border-radius: 10px; }
.chat-markdown :deep(pre code) { color: #e6f1f8; font: 11.5px/1.65 "DM Mono", monospace; }
.chat-markdown :deep(code) { padding: 1px 5px; border-radius: 5px; background: #eef3f7; font: 11.5px "DM Mono", monospace; }
.chat-markdown :deep(table) { border-collapse: collapse; margin: 8px 0; font-size: 11.5px; }
.chat-markdown :deep(th), .chat-markdown :deep(td) { padding: 5px 9px; border: 1px solid #dde7ef; }
.chat-attachments, .chat-scope { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px; justify-content: flex-end; }
.chat-scope { justify-content: flex-start; }
.chat-status { margin: 6px 0 0; color: #8a9aa6; font-size: 10px; }
.chat-tools { list-style: none; margin: 8px 0 0; padding: 0; display: grid; gap: 3px; }
.chat-tools li { display: flex; align-items: center; gap: 7px; padding: 4px 8px; border: 1px solid #dbe7ef; border-radius: 7px; background: #f7fafc; font-size: 10.5px; }
.chat-tools li.failed { border-color: #f3cfcf; background: #fdeeee; }
.chat-tool-tag { padding: 1px 5px; border-radius: 4px; background: #e3f2ec; color: #0a7f58; font-size: 9px; letter-spacing: 0.05em; }
.chat-tools li.failed .chat-tool-tag { background: #f7dcdc; color: #a3231f; }
.chat-tools strong { color: #16222b; font-weight: 600; }
.chat-tool-code { color: #a3231f; font: 9.5px "DM Mono", monospace; }
.chat-tool-meta { margin-left: auto; color: #8a9aa6; }
.chat-tool-note { margin: 6px 0 0; color: #a9640a; font-size: 10.5px; }
.chat-running { display: flex; align-items: center; gap: 8px; color: #0a7f58; font-size: 11.5px; }
.chat-dots { display: inline-flex; gap: 3px; }
.chat-dots i { width: 5px; height: 5px; border-radius: 50%; background: #0a7f58; opacity: 0.35; animation: chat-pulse 1.1s infinite; }
.chat-dots i:nth-child(2) { animation-delay: 0.18s; }
.chat-dots i:nth-child(3) { animation-delay: 0.36s; }
@keyframes chat-pulse { 0%, 100% { opacity: 0.3; transform: translateY(0); } 50% { opacity: 1; transform: translateY(-2px); } }
.chat-composer { padding: 0 18px 12px; }
.chat-pending { display: flex; flex-wrap: wrap; gap: 5px; max-width: 860px; margin: 0 auto 7px; }
.chat-input-shell { max-width: 860px; margin: 0 auto; padding: 10px 12px 8px; border: 1px solid #d9e4ee; border-radius: 16px; background: #fff; box-shadow: 0 8px 28px #0d1a2414; }
.chat-input { width: 100%; min-height: 24px; max-height: 160px; padding: 2px 2px 8px; border: none; outline: none; resize: none; background: transparent; color: #16222b; font: inherit; font-size: 13px; line-height: 1.6; field-sizing: content; }
.chat-file { display: none; }
.chat-controls { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.chat-chip { padding: 5px 9px; border: 1px solid #e0e9f1; border-radius: 9px; background: #f8fbfd; color: #33454f; font: inherit; font-size: 11px; cursor: pointer; }
.chat-chip:hover { border-color: #0a7f58; color: #0a7f58; }
.chat-controls-spacer { flex: 1; }
.chat-send { padding: 7px 14px; border: none; border-radius: 10px; background: #0a7f58; color: #fff; font: inherit; font-size: 12px; cursor: pointer; }
.chat-send:disabled { background: #b9cbd4; cursor: not-allowed; }
.chat-send.stop { background: #a3231f; }
.chat-hint { max-width: 860px; margin: 6px auto 0; color: #93a3af; font-size: 10px; text-align: center; }
.chat-menu { display: grid; gap: 2px; }
.chat-menu-scroll { max-height: 300px; overflow-y: auto; }
.chat-menu-label { margin: 6px 0 2px; color: #8a9aa6; font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em; }
.chat-menu button, .chat-menu-link { padding: 7px 9px; border: none; border-radius: 7px; background: transparent; color: #22303a; font: inherit; font-size: 11.5px; text-align: left; cursor: pointer; }
.chat-menu button:hover, .chat-menu-link:hover { background: #eef5f9; }
.chat-menu button.on { background: #e3f2ec; color: #0a7f58; font-weight: 600; }
.chat-menu button.warn { color: #a9640a; }
.chat-menu-hint { margin: 4px 0 0; color: #8a9aa6; font-size: 10.5px; line-height: 1.5; }
.chat-menu-empty { margin: 0; padding: 6px 9px; color: #93a3af; font-size: 11px; }
.chat-menu-muted { color: #8a9aa6; }
.chat-detail { display: grid; gap: 18px; }
.chat-detail h4 { margin: 0 0 6px; font-size: 12px; color: #16222b; }
.chat-detail p { margin: 2px 0; color: #4c5f6b; font-size: 12px; }
.chat-detail-empty { color: #93a3af; font-size: 12px; }
.chat-detail-file { display: flex; justify-content: space-between; gap: 10px; font-size: 12px; padding: 4px 0; border-bottom: 1px dashed #e6edf3; }
.chat-detail-file a { color: #0a7f58; }
.chat-detail-file span { color: #8a9aa6; font-size: 11px; }
@media (max-width: 760px) {
  .chat-top { padding: 8px 12px; flex-wrap: nowrap; }
  .chat-model, .chat-title .el-tag { display: none; }
  .chat-title { flex: 1 1 auto; min-width: 0; }
  .chat-title-text { max-width: none; }
  .chat-top-actions { margin-left: auto; flex: 0 0 auto; }
  /* "New conversation" already lives in the phone rail strip. */
  .chat-top-actions > .el-button:last-child { display: none; }
  .chat-transcript { padding: 14px 12px 6px; }
  .chat-composer { padding: 0 10px 10px; }
  .chat-title-text { max-width: 42vw; }
}
</style>
