<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { useRouter } from "vue-router";
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

const activeThread = computed(() => threads.value.find((item) => item.id === activeThreadId.value) ?? null);
const canSend = computed(() => draft.value.trim().length > 0 && !sending.value);

const errorCode = ref("");
const needsModelSetup = computed(
  () => errorCode.value === "MODEL_NOT_CONFIGURED" || (context.value?.models.length ?? 1) === 0,
);

function reportError(cause: unknown): void {
  errorCode.value = apiErrorCode(cause) ?? "";
  error.value = cause instanceof Error ? cause.message : "Request failed.";
}

function openMarketplace(): void {
  void router.push("/marketplace");
}

async function loadContext(): Promise<void> {
  if (!props.tenantId || !props.projectId) return;
  try {
    context.value = await getChatContext(props.tenantId, props.projectId);
    if (!selectedModelId.value && context.value.models.length > 0) {
      // The local echo model needs no credential, so it is the safest default
      // until the member authorizes a real platform account.
      const recommended = context.value.models.find((model) => model.recommended);
      selectedModelId.value = (recommended ?? context.value.models[0]).id;
    }
  } catch (cause) {
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
  <section class="agent-chat">
    <header class="chat-toolbar">
      <div class="chat-title">
        <span class="chat-avatar">CA</span>
        <div>
          <h3>Control Agent</h3>
          <p>
            {{ context?.project.name ?? "Project" }} · {{ context?.plugins.length ?? 0 }} plugins ·
            {{ context?.skills.length ?? 0 }} skills
          </p>
        </div>
      </div>
      <div class="chat-actions">
        <el-select v-model="selectedModelId" placeholder="Model" class="model-select">
          <el-option
            v-for="model in context?.models ?? []"
            :key="model.id"
            :label="`${model.display_name} · ${model.provider_name}`"
            :value="model.id"
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
      <aside class="thread-rail">
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
          <p v-if="!activeThreadId" class="transcript-empty">
            No conversation selected. Use “New conversation” to begin.
          </p>
          <article
            v-for="message in messages"
            :key="message.id"
            class="bubble"
            :class="message.role"
          >
            <div class="bubble-head">
              <span>{{ message.role === "user" ? "You" : "Control Agent" }}</span>
              <span class="bubble-time">{{ new Date(message.created_at).toLocaleTimeString() }}</span>
            </div>
            <p class="bubble-body">{{ message.content }}</p>
            <div v-if="message.context.skills?.length || message.context.plugins?.length" class="bubble-context">
              <el-tag v-for="skill in message.context.skills ?? []" :key="skill" size="small" effect="plain">
                skill · {{ skill }}
              </el-tag>
              <el-tag
                v-for="plugin in message.context.plugins ?? []"
                :key="plugin"
                size="small"
                type="success"
                effect="plain"
              >
                plugin · {{ pluginName(plugin) }}
              </el-tag>
            </div>
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
              技能 · {{ skillName(skill) }}
            </el-tag>
            <el-tag
              v-for="plugin in selectedPlugins"
              :key="plugin"
              size="small"
              type="success"
              closable
              @close="togglePlugin(plugin)"
            >
              插件 · {{ pluginName(plugin) }}
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
              <span class="picker-label" title="Project in scope">
                项目 · {{ context?.project.name ?? "—" }}
              </span>
              <el-popover placement="top" trigger="click" :width="320">
                <template #reference>
                  <el-button size="small" text>技能 ({{ selectedSkills.length }})</el-button>
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
                  <el-button size="small" text>插件 ({{ selectedPlugins.length }})</el-button>
                </template>
                <p class="popover-title">Plugins installed in this project</p>
                <div v-for="plugin in context?.plugins ?? []" :key="plugin.id" class="popover-row">
                  <el-checkbox
                    :model-value="selectedPlugins.includes(plugin.id)"
                    @change="togglePlugin(plugin.id)"
                  >
                    {{ plugin.name }} · {{ plugin.version }}
                  </el-checkbox>
                </div>
                <p v-if="!(context?.plugins ?? []).length" class="rail-empty">No plugin installed yet.</p>
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
  height: calc(100vh - 220px);
  min-height: 640px;
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
  width: 260px;
}
.chat-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
.chat-body {
  display: grid;
  grid-template-columns: 260px minmax(0, 1fr);
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
.chat-column {
  display: flex;
  flex-direction: column;
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
  max-width: 72%;
  padding: 10px 12px;
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
  font-size: 13px;
  line-height: 1.6;
  white-space: pre-wrap;
}
.bubble-context {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}
.composer {
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
.composer-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 10px;
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
  padding: 2px 0;
}
</style>
