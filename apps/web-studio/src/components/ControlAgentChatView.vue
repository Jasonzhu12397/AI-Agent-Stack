<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { t, tf } from "../i18n";
import { tServerError } from "../i18n/serverErrors";
import { renderMarkdown } from "../services/markdown";
import {
  apiErrorCode,
  createChatThread,
  deleteChatThread,
  getChatContext,
  getChatThread,
  getChatThreads,
  sendChatMessage,
  type ChatContext,
  type ChatMessage,
  type ChatThread,
} from "../services/api";

const props = defineProps<{ tenantId: string; projectId: string }>();
const router = useRouter();

const context = ref<ChatContext | null>(null);
const threads = ref<ChatThread[]>([]);
const activeThreadId = ref("");
const messages = ref<ChatMessage[]>([]);
const draft = ref("");
const sending = ref(false);
const error = ref("");
const selectedSkills = ref<string[]>([]);
const selectedPlugins = ref<string[]>([]);
const selectedModelId = ref("");

// Wide screens keep the thread rail in the grid; narrow ones show it as an
// overlay so the transcript owns the full width, the way messenger apps do.
const railOpen = ref(false);
const SUGGESTIONS = [
  "Inspect this cluster",
  "Run a skill",
  "Check model routing",
  "Summarise recent runs",
];

function useSuggestion(prompt: string): void {
  draft.value = t(prompt);
}

const activeThread = computed(() => threads.value.find((item) => item.id === activeThreadId.value) ?? null);
const canSend = computed(() => draft.value.trim().length > 0 && !sending.value);

const errorCode = ref("");
const usableModels = computed(() =>
  (context.value?.models ?? []).filter((model) => model.ready !== false),
);
const needsModelSetup = computed(
  () => errorCode.value === "MODEL_NOT_CONFIGURED" || usableModels.value.length === 0,
);

// A fixed "100vh minus 232px" guess leaves the composer under the fold on tall
// or compact screens. Measure where the chat actually starts and claim the rest
// of the viewport, the same way a chat client fills its window.
const chatRoot = ref<HTMLElement | null>(null);
const chatHeight = ref("");

function fitChat(): void {
  const root = chatRoot.value;
  if (!root) return;
  // On a phone the header alone eats a third of the screen, so locking the
  // panel to the viewport squeezes the thread under the composer. Let the page
  // scroll there and keep the chat panel auto-sized instead.
  if (window.innerWidth <= 900 || window.innerHeight <= 560) {
    chatHeight.value = "";
    return;
  }
  const top = root.getBoundingClientRect().top;
  chatHeight.value = `calc(100dvh - ${Math.round(top)}px - 16px)`;
}

onMounted(() => {
  fitChat();
  window.addEventListener("resize", fitChat);
});

onUnmounted(() => {
  window.removeEventListener("resize", fitChat);
});

// Assistant replies that failed keep the raw backend message plus its code, so
// the transcript can re-render the failure in the reader's language.
function bubbleText(message: ChatMessage): string {
  return message.error_code ? tServerError(message.error_code, message.content) : message.content;
}

// Assistant replies come back as markdown; failures are plain prose. The renderer
// escapes everything first, so v-html only carries markup it produced.
function bubbleHtml(message: ChatMessage): string {
  return renderMarkdown(bubbleText(message));
}

function reportError(cause: unknown): void {
  errorCode.value = apiErrorCode(cause) ?? "";
  if (cause instanceof Error) {
    // Transport failures carry no backend code, only an English prose message
    // ending in "HTTP <status>"; surface the status in the reader's language.
    const status = cause.message.match(/HTTP (\d{3})/)?.[1];
    error.value = status
      ? tf("The server returned HTTP {0}.", status)
      : tServerError(apiErrorCode(cause), cause.message);
    return;
  }
  error.value = t("Request failed.");
}

function openMarketplace(): void {
  void router.push("/marketplace");
}

function isWorkspaceGone(cause: unknown): boolean {
  const status = (cause as { status?: number } | null)?.status;
  return status === 404;
}

async function loadContext(): Promise<void> {
  if (!props.tenantId || !props.projectId) {
    // Nothing to load yet: the chat waits for the pickers instead of firing a
    // request that the API would answer with 404.
    context.value = null;
    return;
  }
  try {
    context.value = await getChatContext(props.tenantId, props.projectId);
    if (!selectedModelId.value && context.value.models.length > 0) {
      // The local echo model needs no credential, so it is the safest default
      // until the member authorizes a real platform account.
      const recommended = context.value.models.find(
        (model) => model.recommended && model.ready !== false,
      );
      selectedModelId.value = (recommended ?? context.value.models[0]).id;
    }
  } catch (cause) {
    if (isWorkspaceGone(cause)) {
      context.value = null;
      error.value = t("This workspace is no longer available. Pick another organization or project.");
      return;
    }
    reportError(cause);
  }
}

async function loadThreads(): Promise<void> {
  if (!props.tenantId || !props.projectId) return;
  try {
    threads.value = (await getChatThreads(props.tenantId, props.projectId)).items;
    if (!threads.value.some((item) => item.id === activeThreadId.value)) {
      activeThreadId.value = threads.value[0]?.id ?? "";
    }
  } catch (cause) {
    reportError(cause);
  }
}

async function openThread(threadId: string): Promise<void> {
  activeThreadId.value = threadId;
  railOpen.value = false;
  if (!threadId) {
    messages.value = [];
    return;
  }
  try {
    messages.value = (await getChatThread(threadId)).messages;
  } catch (cause) {
    reportError(cause);
  }
}

async function newThread(): Promise<void> {
  if (!props.tenantId || !props.projectId) return;
  try {
    const thread = await createChatThread(props.tenantId, props.projectId, {
      title: "Control Agent conversation",
      model_config_id: selectedModelId.value || null,
    });
    threads.value = [thread, ...threads.value];
    activeThreadId.value = thread.id;
    messages.value = [];
    draft.value = "";
  } catch (cause) {
    reportError(cause);
  }
}

async function removeThread(threadId: string): Promise<void> {
  try {
    await deleteChatThread(threadId);
    threads.value = threads.value.filter((item) => item.id !== threadId);
    if (activeThreadId.value === threadId) {
      activeThreadId.value = threads.value[0]?.id ?? "";
      messages.value = [];
    }
  } catch (cause) {
    reportError(cause);
  }
}

async function send(): Promise<void> {
  const text = draft.value.trim();
  if (!text) return;
  if (!activeThreadId.value) await newThread();
  if (!activeThreadId.value) return;
  const threadId = activeThreadId.value;
  sending.value = true;
  error.value = "";
  errorCode.value = "";
  try {
    const result = await sendChatMessage(threadId, {
      content: text,
      model_config_id: selectedModelId.value || null,
      context: { skills: selectedSkills.value, plugins: selectedPlugins.value },
    });
    messages.value = [...messages.value, result.user, result.assistant];
    draft.value = "";
    await loadThreads();
    activeThreadId.value = threadId;
  } catch (cause) {
    reportError(cause);
    // The user turn is persisted even when the model call fails, so re-read it.
    try {
      messages.value = (await getChatThread(threadId)).messages;
    } catch {
      /* keep the local transcript */
    }
  } finally {
    sending.value = false;
  }
}

function toggleSkill(skillId: string): void {
  selectedSkills.value = selectedSkills.value.includes(skillId)
    ? selectedSkills.value.filter((item) => item !== skillId)
    : [...selectedSkills.value, skillId];
}

function togglePlugin(pluginId: string): void {
  selectedPlugins.value = selectedPlugins.value.includes(pluginId)
    ? selectedPlugins.value.filter((item) => item !== pluginId)
    : [...selectedPlugins.value, pluginId];
}

function skillName(skillId: string): string {
  return context.value?.skills.find((item) => item.id === skillId)?.name ?? skillId;
}

// A plugin row opens the plugin's own window in the marketplace, the way the
// desktop client jumps from its plugin list into the plugin page. Selecting the
// checkbox still only attaches it to the next turn.
function openPlugin(pluginKey: string): void {
  void router.push({ path: "/marketplace", query: { plugin: pluginKey } });
}

function pluginKey(pluginId: string): string {
  return context.value?.plugins.find((item) => item.id === pluginId)?.key ?? "";
}

// A connector that declares auth cannot run until its installation carries a
// credential reference, so the row offers authorization instead of selection.
function pluginNeedsAuth(plugin: { auth_required?: boolean; credential_configured?: boolean }): boolean {
  return Boolean(plugin.auth_required) && !plugin.credential_configured;
}

function openProviderAccounts(): void {
  void router.push({ path: "/marketplace", query: { provider: "openai" } });
}

function pluginName(pluginId: string): string {
  return context.value?.plugins.find((item) => item.id === pluginId)?.name ?? pluginId;
}

watch(
  () => [props.tenantId, props.projectId],
  async () => {
    selectedSkills.value = [];
    selectedPlugins.value = [];
    activeThreadId.value = "";
    messages.value = [];
    await loadContext();
    await loadThreads();
    if (activeThreadId.value) await openThread(activeThreadId.value);
  },
  { immediate: true },
);
</script>

<template>
  <section ref="chatRoot" class="agent-chat" :style="chatHeight ? { height: chatHeight } : undefined">
    <header class="chat-toolbar">
      <el-button
        class="rail-toggle"
        text
        :title="t('Conversations')"
        @click="railOpen = !railOpen"
      >
        ☰
      </el-button>
      <div class="chat-title">
        <span class="chat-avatar">CA</span>
        <div>
          <h3>Control Agent</h3>
          <p>
            {{ tf("{0} · {1} plugins · {2} skills", context?.project.name ?? t("Project"), context?.plugins.length ?? 0, context?.skills.length ?? 0) }}
          </p>
        </div>
      </div>
      <div class="chat-actions">
        <el-select v-model="selectedModelId" placeholder="Model" class="model-select">
          <el-option
            v-for="model in context?.models ?? []"
            :key="model.id"
            :label="
              model.auth_mode === 'platform_pool'
                ? `${model.display_name} · included, no key needed`
                : `${model.display_name} · ${model.provider_name}`
            "
            :value="model.id"
            :disabled="model.ready === false"
          />
        </el-select>
        <el-button type="primary" plain @click="newThread">New conversation</el-button>
      </div>
    </header>

    <el-alert v-if="error" :title="error" type="error" show-icon :closable="true" @close="error = ''" />
    <el-alert
      v-if="needsModelSetup"
      title="Chat needs a model. Authorize your own OpenAI, DeepSeek, Qwen, GLM or Kimi account and its free allowance covers the conversation."
      type="warning"
      show-icon
      :closable="false"
    >
      <el-button size="small" type="primary" plain @click="openMarketplace">
        Authorize a platform account
      </el-button>
    </el-alert>

    <div class="chat-body">
      <aside class="thread-rail" :class="{ open: railOpen }">
        <p class="rail-label">CONVERSATIONS</p>
        <article
          v-for="thread in threads"
          :key="thread.id"
          class="thread-item"
          :class="{ active: thread.id === activeThreadId }"
          @click="openThread(thread.id)"
        >
          <div>
            <strong>{{ thread.title }}</strong>
            <p>{{ thread.last_message?.content || "No messages yet" }}</p>
          </div>
          <el-button text size="small" @click.stop="removeThread(thread.id)">✕</el-button>
        </article>
        <p v-if="threads.length === 0" class="rail-empty">Start a conversation to talk to the Control Agent.</p>
      </aside>

      <div class="chat-column">
        <div class="transcript">
          <div v-if="messages.length === 0" class="chat-welcome">
            <span class="welcome-mark">CA</span>
            <h2>{{ t("How can I help?") }}</h2>
            <p class="welcome-sub">{{ t("Ask about a cluster, run a skill, or wire a plugin.") }}</p>
            <div class="suggestions">
              <button
                v-for="prompt in SUGGESTIONS"
                :key="prompt"
                type="button"
                class="suggestion"
                @click="useSuggestion(prompt)"
              >
                {{ t(prompt) }}
              </button>
            </div>
          </div>
          <article
            v-for="message in messages"
            :key="message.id"
            class="bubble"
            :class="message.role"
          >
            <div class="bubble-head">
              <span>{{ message.role === "user" ? t("You") : t("Control Agent") }}</span>
              <span class="bubble-time">{{ new Date(message.created_at).toLocaleTimeString() }}</span>
            </div>
            <div class="bubble-body" v-html="bubbleHtml(message)" />
            <div v-if="message.context.skills?.length || message.context.plugins?.length" class="bubble-context">
              <el-tag v-for="skill in message.context.skills ?? []" :key="skill" size="small" effect="plain">
                {{ t("Skill") }} · {{ skill }}
              </el-tag>
              <el-tag
                v-for="plugin in message.context.plugins ?? []"
                :key="plugin"
                size="small"
                type="success"
                effect="plain"
              >
                {{ t("Plugin") }} · {{ pluginName(plugin) }}
              </el-tag>
            </div>
          </article>
          <article v-if="sending" class="bubble assistant typing">
            <div class="bubble-head">
              <span>{{ t("Control Agent") }}</span>
            </div>
            <span class="dots"><i /><i /><i /></span>
          </article>
        </div>

        <footer class="composer">
          <div v-if="selectedSkills.length || selectedPlugins.length" class="composer-chips">
            <el-tag
              v-for="skill in selectedSkills"
              :key="skill"
              size="small"
              closable
              @close="toggleSkill(skill)"
            >
              {{ t("Skill") }} · {{ skillName(skill) }}
            </el-tag>
            <el-tag
              v-for="plugin in selectedPlugins"
              :key="plugin"
              size="small"
              type="success"
              closable
              @close="togglePlugin(plugin)"
            >
              {{ t("Plugin") }} · {{ pluginName(plugin) }}
            </el-tag>
          </div>
          <el-input
            v-model="draft"
            type="textarea"
            :rows="3"
            resize="none"
            placeholder="Ask the Control Agent to inspect a cluster, run a skill, or wire a plugin…"
            @keydown.enter.exact.prevent="send"
          />
          <div class="composer-actions">
            <div class="composer-pickers">
              <slot name="context" />
              <el-button text size="small" class="account-link" @click="openProviderAccounts">
                {{ t("Platform accounts") }}
              </el-button>

              <el-popover placement="top" trigger="click" :width="320">
                <template #reference>
                  <el-button size="small" text>{{ t("Skills") }} ({{ selectedSkills.length }})</el-button>
                </template>
                <p class="popover-title">Skills granted by installed plugins</p>
                <div v-for="skill in context?.skills ?? []" :key="skill.id" class="popover-row">
                  <el-checkbox
                    :model-value="selectedSkills.includes(skill.id)"
                    @change="toggleSkill(skill.id)"
                  >
                    {{ skill.name }}
                  </el-checkbox>
                </div>
                <p v-if="!(context?.skills ?? []).length" class="rail-empty">Install a plugin to grant skills.</p>
              </el-popover>
              <el-popover placement="top" trigger="click" :width="340">
                <template #reference>
                  <el-button size="small" text>{{ t("Plugins") }} ({{ selectedPlugins.length }})</el-button>
                </template>
                <p class="popover-title">Plugins installed in this project</p>
                <div v-for="plugin in context?.plugins ?? []" :key="plugin.id" class="popover-row">
                  <el-checkbox
                    :model-value="selectedPlugins.includes(plugin.id)"
                    :disabled="pluginNeedsAuth(plugin)"
                    @change="togglePlugin(plugin.id)"
                  >
                    {{ plugin.name }} · {{ plugin.version }}
                  </el-checkbox>
                  <el-button
                    text
                    size="small"
                    class="popover-open"
                    :type="pluginNeedsAuth(plugin) ? 'warning' : ''"
                    @click="openPlugin(plugin.key)"
                  >
                    {{ pluginNeedsAuth(plugin) ? t("Authorize") : t("Open") }} ↗
                  </el-button>
                </div>
                <p v-if="!(context?.plugins ?? []).length" class="rail-empty">No plugin installed yet.</p>
                <el-button text size="small" class="popover-browse" @click="openMarketplace">
                  Browse the plugin marketplace ↗
                </el-button>
              </el-popover>
            </div>
            <el-button type="primary" :loading="sending" :disabled="!canSend" @click="send">
              Send
            </el-button>
          </div>
        </footer>
      </div>
    </div>
  </section>
</template>

<style scoped>
.agent-chat {
  display: flex;
  flex-direction: column;
  gap: 12px;
  /* A chat deserves the viewport: a fixed tall column rather than a card that
     grows with the number of messages. */
  /* Fill the viewport below the shell header, section heading and toolbar.
     dvh keeps the composer visible when a mobile browser hides its chrome. */
  height: calc(100dvh - 232px);
  min-height: 560px;
}
.chat-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 16px;
  background: #fff;
  border: 1px solid #e3e8ee;
  border-radius: 8px;
}
.chat-title {
  display: flex;
  align-items: center;
  gap: 10px;
}
.chat-avatar {
  display: grid;
  place-items: center;
  width: 36px;
  height: 36px;
  border-radius: 8px;
  background: #0e7c86;
  color: #fff;
  font-weight: 700;
  letter-spacing: 0.06em;
}
.chat-title h3 {
  margin: 0;
  font-size: 16px;
}
.chat-title p {
  margin: 2px 0 0;
  color: #6b7785;
  font-size: 12px;
}
.model-select {
  width: min(320px, 42vw);
}
.chat-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
.chat-body {
  display: grid;
  grid-template-columns: 288px minmax(0, 1fr);
  gap: 12px;
  flex: 1;
  min-height: 0;
}
.thread-rail {
  background: #fff;
  border: 1px solid #e3e8ee;
  border-radius: 8px;
  padding: 12px;
  overflow-y: auto;
}
.rail-label {
  margin: 0 0 8px;
  font-size: 11px;
  letter-spacing: 0.08em;
  color: #6b7785;
}
.thread-item {
  display: flex;
  justify-content: space-between;
  gap: 6px;
  padding: 8px 10px;
  border-radius: 6px;
  border: 1px solid transparent;
  cursor: pointer;
}
.thread-item.active {
  border-color: #0e7c86;
  background: #f2fbfb;
}
.thread-item strong {
  display: block;
  font-size: 13px;
}
.thread-item p {
  margin: 2px 0 0;
  font-size: 12px;
  color: #6b7785;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.rail-empty {
  margin: 8px 0 0;
  font-size: 12px;
  color: #8b97a5;
}
/* A reading column, the way ChatGPT and Doubao lay out a thread: the shell is
   full width, the messages keep a comfortable measure instead of stretching. */
.chat-column {
  display: flex;
  flex-direction: column;
  width: 100%;
  max-width: 1040px;
  margin: 0 auto;
  min-height: 0;
  height: 100%;
}
.transcript {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  padding: 16px;
  background: #f7f9fb;
  border: 1px solid #e3e8ee;
  border-radius: 8px 8px 0 0;
  display: flex;
 flex-direction: column;
  gap: 12px;
}
.transcript-empty {
  color: #8b97a5;
  font-size: 13px;
  margin: 0;
}
.bubble {
  max-width: 78%;
  padding: 12px 16px;
  border-radius: 8px;
  background: #fff;
  border: 1px solid #e3e8ee;
}
.bubble.user {
  align-self: flex-end;
  background: #eaf7f7;
  border-color: #cfecec;
}
.bubble-head {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  font-size: 11px;
  color: #6b7785;
}
.bubble-body {
  margin: 6px 0 0;
  font-family: "DM Sans", system-ui, -apple-system, "Segoe UI", sans-serif;
  font-size: 14px;
  line-height: 1.72;
  white-space: normal;
}
.bubble-context {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}
.composer {
  display: flex;
  flex-direction: column;
  gap: 8px;
  flex: 0 0 auto;
  background: #fff;
  border: 1px solid #e3e8ee;
  border-top: none;
  border-radius: 0 0 8px 8px;
  padding: 12px 16px 14px;
}
.composer-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 8px;
}
.composer :deep(.el-textarea) {
  width: 100%;
}
.composer-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
}
.composer-pickers {
  display: flex;
  align-items: center;
  gap: 6px;
}
.picker-label {
  font-size: 12px;
  color: #6b7785;
}
.popover-title {
  margin: 0 0 8px;
  font-size: 12px;
  color: #6b7785;
}
.popover-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 2px 0;
}
.popover-open,
.popover-browse {
  padding: 0;
}

/* Tablet: the thread rail stops competing for width. */
@media (max-width: 1180px) {
  .chat-body {
    grid-template-columns: 232px minmax(0, 1fr);
  }
  .model-select {
    width: min(260px, 44vw);
  }
}

/* Phone: the rail becomes a horizontal strip above the thread, so the
   transcript keeps the full width and the composer stays reachable. */
@media (max-width: 900px) {
  .agent-chat {
    height: auto;
    min-height: 0;
  }
  .transcript {
    max-height: 62dvh;
  }
  .chat-toolbar {
    flex-wrap: wrap;
    gap: 8px;
    padding: 10px 12px;
  }
  .chat-actions {
    flex-wrap: wrap;
    width: 100%;
  }
  .model-select {
    flex: 1 1 160px;
    width: auto;
    min-width: 0;
  }
  .chat-body {
    grid-template-columns: minmax(0, 1fr);
    grid-template-rows: minmax(0, 1fr);
    gap: 8px;
  }
  .bubble {
    max-width: 92%;
  }
  .composer {
    padding: 10px 12px 12px;
  }
}

@media (max-width: 560px) {
  .agent-chat {
    height: auto;
  }
  .transcript {
    max-height: 56dvh;
  }
  .chat-title h3 {
    font-size: 15px;
  }
  .chat-avatar {
    width: 30px;
    height: 30px;
    font-size: 12px;
  }
  .transcript {
    padding: 10px;
    gap: 8px;
  }
  .bubble {
    max-width: 100%;
  }
  .composer-pickers {
    flex-wrap: wrap;
  }
}
/* Threads live in a drawer on narrow screens and as a column on wide ones. */
.chat-body {
  position: relative;
}
.rail-toggle {
  display: none;
  font-size: 16px;
  line-height: 1;
}
@media (max-width: 1200px) {
  .rail-toggle {
    display: inline-flex;
  }
  .thread-rail {
    display: none;
  }
  .thread-rail.open {
    display: block;
    position: absolute;
    top: 0;
    bottom: 0;
    left: 0;
    z-index: 6;
    width: min(320px, 84%);
    overflow-y: auto;
    background: #fff;
    box-shadow: 24px 0 48px rgba(5, 8, 15, 0.18);
  }
  .thread-item {
    border-color: #e3e8ee;
  }
}
/* Empty thread: greet, then offer the four things the agent can actually do. */
.chat-welcome {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  margin: auto;
  padding: 24px 12px;
  text-align: center;
}
.welcome-mark {
  display: grid;
  place-items: center;
  width: 52px;
  height: 52px;
  border-radius: 14px;
  background: #0e7c86;
  color: #fff;
  font-weight: 700;
  letter-spacing: 0.06em;
}
.chat-welcome h2 {
  margin: 6px 0 0;
  font-size: 20px;
  color: #121f28;
}
.welcome-sub {
  margin: 0;
  color: #6b7785;
  font-size: 13px;
}
.suggestions {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 8px;
  width: min(560px, 100%);
  margin-top: 8px;
}
.suggestion {
  padding: 10px 12px;
  border: 1px solid #e3e8ee;
  border-radius: 10px;
  background: #fff;
  color: #121f28;
  font: inherit;
  font-size: 13px;
  text-align: left;
  cursor: pointer;
}
.suggestion:hover {
  border-color: #bfe8d7;
  background: #f2fbfb;
}
@media (max-width: 900px) {
  .chat-welcome h2 {
    font-size: 17px;
  }
  .suggestions {
    grid-template-columns: 1fr 1fr;
  }
}

/* Phone header stays two rows: identity + model, then scope + new chat. */
@media (max-width: 640px) {
  .chat-toolbar {
    flex-wrap: wrap;
    padding: 8px 10px;
    gap: 6px;
  }
  .chat-title p {
    display: none;
  }
  .chat-actions {
    width: 100%;
    gap: 6px;
  }
  .context-select {
    flex: 1 1 44%;
    width: auto;
    min-width: 0;
  }
  .model-select {
    flex: 1 1 100%;
  }
  .chat-actions .el-button {
    margin-left: 0;
  }
}

/* Phone composer: chips wrap onto two lines and keep the send button beside them. */
@media (max-width: 640px) {
  .composer-actions {
    flex-wrap: wrap;
    gap: 6px;
  }
  .composer-pickers {
    flex: 1 1 auto;
    gap: 6px;
  }
  .composer-pickers .el-button {
    padding: 4px 6px;
    font-size: 12px;
  }
  .composer-actions > .el-button {
    margin-left: auto;
  }
}

/* Markdown inside a bubble: code blocks scroll instead of stretching the card. */
.bubble-body :deep(p) {
  margin: 0 0 8px;
}
.bubble-body :deep(p:last-child) {
  margin-bottom: 0;
}
.bubble-body :deep(pre.code-block) {
  margin: 8px 0;
  padding: 10px 12px;
  overflow-x: auto;
  background: #0f1b24;
  border-radius: 8px;
  color: #e6f1ec;
  font-size: 12.5px;
  line-height: 1.6;
}
.bubble-body :deep(.code-lang) {
  display: block;
  margin-bottom: 6px;
  color: #8fb3a6;
  font-size: 10px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.bubble-body :deep(ul),
.bubble-body :deep(ol) {
  margin: 6px 0;
  padding-left: 20px;
}
.bubble-body :deep(code) {
  padding: 1px 5px;
  border-radius: 4px;
  background: #eef5fa;
  font-size: 12.5px;
}
.bubble-body :deep(pre.code-block code) {
  padding: 0;
  background: transparent;
  color: #e6f1ec;
  white-space: pre;
}
.bubble-body :deep(a) {
  color: #0a7f58;
  text-decoration: underline;
}
.typing .dots {
  display: inline-flex;
  gap: 4px;
}
.typing .dots i {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #0e7c86;
  animation: chat-dot 1.2s infinite ease-in-out;
}
.typing .dots i:nth-child(2) {
  animation-delay: 0.2s;
}
.typing .dots i:nth-child(3) {
  animation-delay: 0.4s;
}
@keyframes chat-dot {
  0%, 60%, 100% { opacity: 0.25; transform: translateY(0); }
  30% { opacity: 1; transform: translateY(-3px); }
}
</style>
