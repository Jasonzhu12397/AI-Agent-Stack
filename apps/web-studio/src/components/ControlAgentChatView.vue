<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
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
  updateChatThread,
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
const transcript = ref<HTMLElement | null>(null);
const chatHeight = ref("");

function fitChat(): void {
  const root = chatRoot.value;
  if (!root) return;
  // Only a landscape phone shorter than a message row gives up on the fixed
  // height; everywhere else the thread fills the window and scrolls inside.
  if (window.innerHeight <= 480) {
    chatHeight.value = "";
    return;
  }
  const top = root.getBoundingClientRect().top;
  chatHeight.value = `calc(100dvh - ${Math.round(top)}px - 16px)`;
}

// A chat client always lands on the newest turn; without this the thread stays
// scrolled to the top after a reply arrives.
function scrollToLatest(): void {
  const el = transcript.value;
  if (el) el.scrollTop = el.scrollHeight;
}

onMounted(() => {
  fitChat();
  window.addEventListener("resize", fitChat);
});

watch(
  () => [messages.value.length, sending.value],
  () => nextTick(scrollToLatest),
);

onUnmounted(() => {
  window.removeEventListener("resize", fitChat);
});

// Assistant replies that failed keep the raw backend message plus its code, so
// the transcript can re-render the failure in the reader's language.
function bubbleText(message: ChatMessage): string {
  if (message.error_code) return tServerError(message.error_code, message.content);
  const usage = message.usage as Record<string, unknown>;
  // A credential rejection can fall back to the local echo provider, whose
  // transcript-only reply is the raw "Tool result: {...}" line. The notice
  // above already names the rejected key, so show a plain status instead of
  // presenting that debugging string as the assistant's answer.
  if (
    usage.fallback_reason === "MODEL_CREDENTIAL_REJECTED" &&
    message.content.startsWith("Tool result:")
  ) {
    return t("The tool ran, but no model answer could be generated.");
  }
  return message.content;
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
      // The API recommends the member's linked account first, a configured
      // shared pool next, and the local echo model last, so a pasted key is
      // used by the next conversation instead of being shadowed by the demo.
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
    await nextTick();
    scrollToLatest();
  } catch (cause) {
    reportError(cause);
  }
}

async function newThread(title = "New conversation"): Promise<void> {
  if (!props.tenantId || !props.projectId) return;
  try {
    const thread = await createChatThread(props.tenantId, props.projectId, {
      title,
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
    if (messages.value.filter((message) => message.role === "user").length === 1) {
      const named = text.replace(/\s+/g, " ").slice(0, 48);
      try {
        const updated = await updateChatThread(threadId, { title: named });
        threads.value = threads.value.map((item) => (item.id === threadId ? { ...item, title: updated.title } : item));
      } catch {
        /* a title is cosmetic: never fail the turn over it */
      }
    }
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

type SheetName = "" | "scope" | "skills" | "plugins" | "model";

// Pickers live in a bottom sheet, the pattern the reference mobile app uses:
// chips above the composer open a sheet over the thread instead of a dropdown.
const sheet = ref<SheetName>("");

function openSheet(name: SheetName): void {
  sheet.value = name;
}

const scopeLabel = computed(() => context.value?.project.name ?? t("Project"));
const currentModelLabel = computed(
  () => context.value?.models.find((model) => model.id === selectedModelId.value)?.display_name ?? t("Model"),
);
const SHEET_TITLES: Record<Exclude<SheetName, "">, () => string> = {
  scope: () => t("Workspace"),
  skills: () => t("Skills"),
  plugins: () => t("Plugins"),
  model: () => t("Model"),
};

const sheetTitle = computed(() => (sheet.value ? SHEET_TITLES[sheet.value]() : ""));

// One letter per row keeps the sheet scannable without an icon set.
function mark(name: string): string {
  return (name.trim()[0] ?? "?").toUpperCase();
}

function formatTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
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
function fallbackNotice(message: ChatMessage): string {
  const usage = message.usage as Record<string, unknown>;
  if (typeof usage.fallback_provider !== "string") return "";
  const from = typeof usage.fallback_from_provider === "string" ? usage.fallback_from_provider : t("the selected model");
  if (usage.fallback_reason === "MODEL_CREDENTIAL_REJECTED") {
    return t("{from} rejected your API key. Replace it in the integration center; this answer temporarily uses {provider}.")
      .replace("{provider}", usage.fallback_provider)
      .replace("{from}", from);
  }
  return t("The selected provider failed, so this answer uses {provider} instead (source: {from}).")
    .replace("{provider}", usage.fallback_provider)
    .replace("{from}", from);
}

</script>

<template>
  <section ref="chatRoot" class="agent-chat" :style="chatHeight ? { height: chatHeight } : undefined">
    <header class="chat-bar">
      <button class="bar-icon bar-rail" type="button" :title="t('Conversations')" @click="railOpen = !railOpen">
        ☰
      </button>
      <div class="bar-title">
        <span class="bar-avatar">CA</span>
        <div class="bar-text">
          <h3>{{ t("Control Agent") }}</h3>
          <p>
            {{ tf("{0} · {1} plugins · {2} skills", scopeLabel, context?.plugins.length ?? 0, context?.skills.length ?? 0) }}
          </p>
        </div>
      </div>
      <button class="bar-icon" type="button" :title="t('New conversation')" @click="newThread()">＋</button>
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
        {{ t("Authorize a platform account") }}
      </el-button>
    </el-alert>

    <div class="chat-body">
      <aside class="thread-rail" :class="{ open: railOpen }">
        <p class="rail-label">{{ t("Conversations") }}</p>
        <article
          v-for="thread in threads"
          :key="thread.id"
          class="thread-item"
          :class="{ active: thread.id === activeThreadId }"
          @click="openThread(thread.id)"
        >
          <div>
            <strong>{{ thread.title }}</strong>
            <p>{{ thread.last_message?.content || t("No messages yet") }}</p>
          </div>
          <el-button text size="small" @click.stop="removeThread(thread.id)">✕</el-button>
        </article>
        <p v-if="threads.length === 0" class="rail-empty">{{ t("Start a conversation to talk to the Control Agent.") }}</p>
      </aside>

      <div class="chat-column">
        <div ref="transcript" class="transcript">
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

          <article v-for="message in messages" :key="message.id" class="msg" :class="message.role">
            <span v-if="message.role !== 'user'" class="msg-avatar">CA</span>
            <div class="msg-card">
              <div class="bubble-body" v-html="bubbleHtml(message)" />
              <p v-if="fallbackNotice(message)" class="bubble-fallback">
                {{ fallbackNotice(message) }}
              </p>
              <div
                v-if="message.context.skills?.length || message.context.plugins?.length"
                class="bubble-context"
              >
                <el-tag v-for="skill in message.context.skills ?? []" :key="skill" size="small" effect="plain">
                  {{ t("Skill") }} · {{ skillName(skill) }}
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
              <time class="msg-time">{{ formatTime(message.created_at) }}</time>
            </div>
          </article>

          <article v-if="sending" class="msg assistant">
            <span class="msg-avatar">CA</span>
            <div class="msg-card typing">
              <span class="dots"><i /><i /><i /></span>
            </div>
          </article>
        </div>

        <footer class="composer">
          <div class="chip-bar">
            <button type="button" class="chip" @click="openSheet('scope')">
              <span class="chip-mark">▦</span>
              {{ scopeLabel }}
            </button>
            <button
              type="button"
              class="chip"
              :class="{ on: selectedSkills.length > 0 }"
              @click="openSheet('skills')"
            >
              {{ t("Skills") }}<span v-if="selectedSkills.length" class="chip-count">{{ selectedSkills.length }}</span>
            </button>
            <button
              type="button"
              class="chip"
              :class="{ on: selectedPlugins.length > 0 }"
              @click="openSheet('plugins')"
            >
              {{ t("Plugins") }}<span v-if="selectedPlugins.length" class="chip-count">{{ selectedPlugins.length }}</span>
            </button>
            <button type="button" class="chip chip-model" @click="openSheet('model')">
              {{ currentModelLabel }}
            </button>
          </div>

          <div v-if="selectedSkills.length || selectedPlugins.length" class="composer-chips">
            <el-tag v-for="skill in selectedSkills" :key="skill" size="small" closable @close="toggleSkill(skill)">
              {{ skillName(skill) }}
            </el-tag>
            <el-tag
              v-for="plugin in selectedPlugins"
              :key="plugin"
              size="small"
              type="success"
              closable
              @close="togglePlugin(plugin)"
            >
              {{ pluginName(plugin) }}
            </el-tag>
          </div>

          <div class="composer-input">
            <el-input
              v-model="draft"
              type="textarea"
              :autosize="{ minRows: 1, maxRows: 4 }"
              resize="none"
              :placeholder="t('Ask the Control Agent to inspect a cluster, run a skill, or wire a plugin…')"
              @keydown.enter.exact.prevent="send"
            />
            <button type="button" class="send" :disabled="!canSend" :title="t('Send')" @click="send">↑</button>
          </div>
        </footer>
      </div>
    </div>

    <div v-if="sheet" class="sheet-layer" @click.self="sheet = ''">
      <section class="sheet" role="dialog" :aria-label="sheetTitle">
        <span class="sheet-grip" />
        <header class="sheet-head">
          <h4>{{ sheetTitle }}</h4>
          <button type="button" class="sheet-close" @click="sheet = ''">✕</button>
        </header>

        <div class="sheet-body">
          <template v-if="sheet === 'scope'">
            <p class="sheet-hint">{{ t("Choose the workspace this chat runs in.") }}</p>
            <slot name="context" />
          </template>

          <template v-else-if="sheet === 'skills'">
            <p v-if="!(context?.skills ?? []).length" class="sheet-empty">{{ t("No skills available yet.") }}</p>
            <button
              v-for="skill in context?.skills ?? []"
              :key="skill.id"
              type="button"
              class="sheet-row"
              @click="toggleSkill(skill.id)"
            >
              <span class="row-mark">{{ mark(skill.name) }}</span>
              <span class="row-text">
                <strong>{{ skill.name }}</strong>
                <small>{{ t("Granted by an installed plugin") }}</small>
              </span>
              <span class="row-check" :class="{ on: selectedSkills.includes(skill.id) }">✓</span>
            </button>
          </template>

          <template v-else-if="sheet === 'plugins'">
            <p v-if="!(context?.plugins ?? []).length" class="sheet-empty">{{ t("No plugins installed yet.") }}</p>
            <button
              v-for="plugin in context?.plugins ?? []"
              :key="plugin.id"
              type="button"
              class="sheet-row"
              :class="{ muted: pluginNeedsAuth(plugin) }"
              @click="pluginNeedsAuth(plugin) ? openPlugin(plugin.key) : togglePlugin(plugin.id)"
            >
              <span class="row-mark">{{ mark(plugin.name) }}</span>
              <span class="row-text">
                <strong>{{ plugin.name }}</strong>
                <small>
                  {{ plugin.version }} ·
                  {{ pluginNeedsAuth(plugin) ? t("Requires authorization") : t("Ready") }}
                </small>
              </span>
              <span v-if="pluginNeedsAuth(plugin)" class="row-cta">{{ t("Authorize") }} ↗</span>
              <span v-else class="row-check" :class="{ on: selectedPlugins.includes(plugin.id) }">✓</span>
            </button>
          </template>

          <template v-else>
            <p v-if="!(context?.models ?? []).length" class="sheet-empty">{{ t("No model is available yet.") }}</p>
            <button
              v-for="model in context?.models ?? []"
              :key="model.id"
              type="button"
              class="sheet-row"
              :class="{ muted: model.ready === false }"
              :disabled="model.ready === false"
              @click="selectedModelId = model.id"
            >
              <span class="row-mark">{{ mark(model.display_name) }}</span>
              <span class="row-text">
                <strong>{{ model.display_name }}</strong>
                <small>
                  {{ model.auth_mode === "platform_pool" ? t("Included with the workspace") : model.provider_name }}
                </small>
              </span>
              <span class="row-check" :class="{ on: selectedModelId === model.id }">✓</span>
            </button>
          </template>
        </div>

        <div class="sheet-foot">
          <el-button v-if="sheet === 'plugins'" text size="small" @click="openMarketplace">
            {{ t("Browse the plugin marketplace") }} ↗
          </el-button>
          <el-button v-else-if="sheet === 'scope'" text size="small" @click="openProviderAccounts">
            {{ t("Platform accounts") }} ↗
          </el-button>
          <span class="sheet-spacer" />
          <el-button type="primary" @click="sheet = ''">{{ t("Done") }}</el-button>
        </div>
      </section>
    </div>
  </section>
</template>

<style scoped>
.agent-chat {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 0;
  /* A chat is a window, not a card that grows with the thread: claim the height
     left below the shell and let the transcript scroll inside it. */
  height: calc(100dvh - 232px);
  min-height: 520px;
  overflow: hidden;
  background: #fff;
  border: 1px solid #e3eaf2;
  border-radius: 12px;
}
.agent-chat > .el-alert {
  margin: 8px 12px 0;
}

/* Single compact bar: menu, identity, new chat. The model and the scope moved
   into the composer chips, the way mobile chat clients do it. */
.chat-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 12px;
  border-bottom: 1px solid #eef2f6;
  background: #fff;
}
.bar-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  border: none;
  border-radius: 50%;
  background: transparent;
  color: #2a3b48;
  font-size: 17px;
  line-height: 1;
  cursor: pointer;
}
.bar-icon:hover {
  background: #f1f5f8;
}
.bar-rail {
  display: none;
}
.bar-title {
  display: flex;
  align-items: center;
  gap: 9px;
  flex: 1;
  min-width: 0;
}
.bar-avatar {
  display: grid;
  place-items: center;
  width: 32px;
  height: 32px;
  flex: 0 0 auto;
  border-radius: 9px;
  background: #0e7c86;
  color: #fff;
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.06em;
}
.bar-text {
  min-width: 0;
}
.bar-text h3 {
  margin: 0;
  font-size: 15px;
  color: #121f28;
}
.bar-text p {
  margin: 1px 0 0;
  overflow: hidden;
  color: #7c8a97;
  font-size: 11.5px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.chat-body {
  position: relative;
  display: grid;
  grid-template-columns: 260px minmax(0, 1fr);
  flex: 1;
  min-height: 0;
}
.thread-rail {
  padding: 12px 10px;
  overflow-y: auto;
  border-right: 1px solid #eef2f6;
  background: #fbfcfd;
}
.rail-label {
  margin: 0 0 8px;
  color: #7c8a97;
  font-size: 10.5px;
  letter-spacing: 0.1em;
}
.thread-item {
  display: flex;
  justify-content: space-between;
  gap: 6px;
  padding: 8px 10px;
  border: 1px solid transparent;
  border-radius: 8px;
  cursor: pointer;
}
.thread-item:hover {
  background: #f2f6fa;
}
.thread-item.active {
  border-color: #0e7c86;
  background: #effafa;
}
.thread-item strong {
  display: block;
  font-size: 13px;
}
.thread-item p {
  margin: 2px 0 0;
  overflow: hidden;
  color: #7c8a97;
  font-size: 11.5px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.rail-empty {
  margin: 8px 0 0;
  color: #8b97a5;
  font-size: 12px;
}

.chat-column {
  display: flex;
  flex-direction: column;
  width: 100%;
  min-height: 0;
}
.transcript {
  display: flex;
  flex: 1 1 auto;
  flex-direction: column;
  gap: 14px;
  min-height: 0;
  padding: 16px 18px;
  overflow-y: auto;
  background: #f6f8fa;
}

/* Messages: avatar plus a soft card for the assistant, a tinted card on the
   right for the reader. */
.msg {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  max-width: min(760px, 86%);
}
.msg.user {
  align-self: flex-end;
  flex-direction: row-reverse;
}
.msg-avatar {
  display: grid;
  place-items: center;
  width: 28px;
  height: 28px;
  flex: 0 0 auto;
  border-radius: 8px;
  background: #0e7c86;
  color: #fff;
  font-size: 10.5px;
  font-weight: 700;
  letter-spacing: 0.05em;
}
.bubble-fallback {
  margin: 8px 0 0;
  padding: 6px 10px;
  border-radius: 8px;
  background: var(--el-color-warning-light-9);
  color: var(--el-color-warning-dark-2);
  font-size: 12px;
  line-height: 1.4;
}
.msg-card {
  padding: 10px 14px;
  border: 1px solid #e9eff4;
  border-radius: 14px 14px 14px 4px;
  background: #fff;
  box-shadow: 0 1px 2px rgba(16, 32, 44, 0.04);
  min-width: 0;
}
.msg.user .msg-card {
  border-color: #cfe9df;
  border-radius: 14px 14px 4px 14px;
  background: #e8f6f0;
}
.bubble-body {
  font-family: "DM Sans", system-ui, -apple-system, "Segoe UI", sans-serif;
  font-size: 14px;
  line-height: 1.7;
  color: #16232d;
  white-space: normal;
}
.msg-time {
  display: block;
  margin-top: 6px;
  color: #93a1ad;
  font-size: 10.5px;
}
.msg.user .msg-time {
  text-align: right;
}
.bubble-context {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  margin-top: 8px;
}

.composer {
  display: flex;
  flex: 0 0 auto;
  flex-direction: column;
  gap: 8px;
  padding: 8px 12px 10px;
  border-top: 1px solid #eef2f6;
  background: #fff;
}
.chip-bar {
  display: flex;
  gap: 6px;
  padding: 2px 0;
  overflow-x: auto;
  scrollbar-width: none;
}
.chip-bar::-webkit-scrollbar {
  display: none;
}
.chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 30px;
  padding: 0 11px;
  flex: 0 0 auto;
  border: 1px solid #e4eaf0;
  border-radius: 15px;
  background: #fff;
  color: #33434f;
  font: inherit;
  font-size: 12.5px;
  white-space: nowrap;
  cursor: pointer;
}
.chip:hover {
  border-color: #bfe3d0;
  background: #f4fbf8;
}
.chip.on {
  border-color: #8fd3b4;
  background: #effbf5;
  color: #0a6b4a;
}
.chip-mark {
  color: #0e7c86;
  font-size: 12px;
}
.chip-count {
  display: inline-grid;
  place-items: center;
  min-width: 16px;
  height: 16px;
  padding: 0 4px;
  border-radius: 8px;
  background: #0e7c86;
  color: #fff;
  font-size: 10px;
}
.chip-model {
  max-width: 148px;
  overflow: hidden;
  text-overflow: ellipsis;
}
.composer-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
}
.composer-input {
  display: flex;
  align-items: flex-end;
  gap: 8px;
  padding: 6px 6px 6px 12px;
  border: 1px solid #dfe7ee;
  border-radius: 16px;
  background: #fff;
}
.composer-input:focus-within {
  border-color: #96d7c5;
  box-shadow: 0 0 0 3px rgba(18, 184, 134, 0.12);
}
.composer-input :deep(.el-textarea__inner) {
  padding: 6px 0;
  border: none;
  box-shadow: none;
  background: transparent;
  font-family: inherit;
  font-size: 14px;
  line-height: 1.6;
}
.send {
  display: grid;
  place-items: center;
  width: 34px;
  height: 34px;
  flex: 0 0 auto;
  border: none;
  border-radius: 50%;
  background: #12b886;
  color: #fff;
  font-size: 17px;
  line-height: 1;
  cursor: pointer;
}
.send:disabled {
  background: #cfd8e0;
  cursor: not-allowed;
}
.send:hover:not(:disabled) {
  background: #0aa678;
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
  color: #121f28;
  font-size: 20px;
}
.welcome-sub {
  margin: 0;
  color: #7c8a97;
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
  border: 1px solid #e4eaf0;
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

/* Bottom sheet: the phone pattern the reference app uses for plugin, skill and
   project pickers — it slides over the thread instead of pushing it around. */
.sheet-layer {
  position: fixed;
  inset: 0;
  z-index: 2400;
  display: flex;
  align-items: flex-end;
  justify-content: center;
  background: rgba(9, 18, 26, 0.42);
}
.sheet {
  display: flex;
  flex-direction: column;
  width: min(560px, 100%);
  max-height: 78dvh;
  border-radius: 18px 18px 0 0;
  background: #fff;
  box-shadow: 0 -18px 48px rgba(5, 8, 15, 0.18);
  animation: sheet-in 0.18s ease-out;
}
@keyframes sheet-in {
  from {
    transform: translateY(28px);
    opacity: 0.6;
  }
  to {
    transform: none;
    opacity: 1;
  }
}
.sheet-grip {
  width: 38px;
  height: 4px;
  margin: 8px auto 0;
  border-radius: 2px;
  background: #dbe4ec;
}
.sheet-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 16px 2px;
}
.sheet-head h4 {
  margin: 0;
  font-size: 15px;
  color: #121f28;
}
.sheet-close {
  border: none;
  background: none;
  color: #7c8a97;
  font-size: 15px;
  cursor: pointer;
}
.sheet-body {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 6px 10px 10px;
  overflow-y: auto;
}
.sheet-hint,
.sheet-empty {
  margin: 4px 6px 8px;
  color: #7c8a97;
  font-size: 12px;
}
.sheet-body :deep(.context-select) {
  width: 100%;
  margin-bottom: 8px;
}
.sheet-row {
  display: flex;
  align-items: center;
  gap: 12px;
  width: 100%;
  padding: 9px 10px;
  border: none;
  border-radius: 10px;
  background: transparent;
  color: #16232d;
  font: inherit;
  text-align: left;
  cursor: pointer;
}
.sheet-row:hover {
  background: #f5f8fb;
}
.sheet-row.muted {
  opacity: 0.62;
}
.row-mark {
  display: grid;
  place-items: center;
  width: 34px;
  height: 34px;
  flex: 0 0 auto;
  border-radius: 9px;
  background: #eef4f9;
  color: #0e7c86;
  font-size: 12px;
  font-weight: 700;
}
.row-text {
  display: flex;
  flex: 1;
  flex-direction: column;
  min-width: 0;
}
.row-text strong {
  font-size: 13.5px;
  font-weight: 600;
}
.row-text small {
  overflow: hidden;
  color: #7c8a97;
  font-size: 11.5px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.row-check {
  display: grid;
  place-items: center;
  width: 20px;
  height: 20px;
  flex: 0 0 auto;
  border: 1.5px solid #d4dde6;
  border-radius: 50%;
  color: transparent;
  font-size: 11px;
}
.row-check.on {
  border-color: #12b886;
  background: #12b886;
  color: #fff;
}
.row-cta {
  flex: 0 0 auto;
  color: #c26a00;
  font-size: 11.5px;
  white-space: nowrap;
}
.sheet-foot {
  display: flex;
  flex-wrap: nowrap;
  align-items: center;
  gap: 8px;
  padding: 8px 14px calc(10px + env(safe-area-inset-bottom));
  border-top: 1px solid #eef2f6;
}
.sheet-spacer {
  flex: 1;
}
.sheet-foot :deep(.el-button) {
  margin-left: 0;
}

@media (max-width: 1200px) {
  .bar-rail {
    display: inline-flex;
  }
  .chat-body {
    grid-template-columns: minmax(0, 1fr);
  }
  .thread-rail {
    display: none;
  }
  .thread-rail.open {
    position: absolute;
    top: 0;
    bottom: 0;
    left: 0;
    z-index: 6;
    display: block;
    width: min(320px, 84%);
    border-right: 1px solid #eef2f6;
    box-shadow: 24px 0 48px rgba(5, 8, 15, 0.18);
  }
  .thread-item {
    border-color: #e9eff4;
  }
}

@media (max-width: 900px) {
  .agent-chat {
    border-radius: 10px;
  }
  .transcript {
    padding: 12px;
    gap: 10px;
  }
  .msg {
    max-width: 92%;
  }
  .composer {
    padding: 6px 10px calc(8px + env(safe-area-inset-bottom));
  }
  .sheet {
    max-height: 82dvh;
  }
}

@media (max-width: 560px) {
  .chat-bar {
    padding: 8px 8px;
    gap: 8px;
  }
  .bar-text p {
    display: none;
  }
  .msg {
    max-width: 100%;
  }
  .chat-welcome h2 {
    font-size: 17px;
  }
  .suggestions {
    grid-template-columns: 1fr 1fr;
  }
  .msg-avatar {
    width: 26px;
    height: 26px;
  }
}

/* Markdown inside a bubble. */
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
  border-radius: 8px;
  background: #0f1b24;
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
