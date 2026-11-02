<script setup lang="ts">
import { computed, ref, watch } from "vue";
import {
  createFakeProvider,
  createOpenAICompatibleProvider,
  createModelConfig,
  getModelConfigs,
  getModelProviders,
  testModel,
  type ModelConfig,
  type ModelProvider,
  type ModelTestResult,
} from "../services/api";

const props = defineProps<{ tenantId: string }>();
const providers = ref<ModelProvider[]>([]);
const models = ref<ModelConfig[]>([]);
const selectedProviderId = ref("");
const providerName = ref("Local Fake Provider");
const fakeResponse = ref("Hello. I am the local test model.");
const modelName = ref("fake-chat");
const modelDisplayName = ref("Fake Chat");
const openAIProviderName = ref("Local Ollama");
const openAIBaseUrl = ref("http://ollama:11434/v1");
const openAICredentialRef = ref("");
const embeddingModelName = ref("nomic-embed-text");
const embeddingDisplayName = ref("Local Embeddings");
const testPrompt = ref("Explain what an AI Agent is in one sentence.");
const testResult = ref<ModelTestResult | null>(null);
const error = ref("");
const busy = ref(false);
const selectedProvider = computed(
  () => providers.value.find((provider) => provider.id === selectedProviderId.value) ?? null,
);

async function loadProviders(): Promise<void> {
  if (!props.tenantId) {
    providers.value = [];
    models.value = [];
    return;
  }
  try {
    providers.value = await getModelProviders(props.tenantId);
    if (!providers.value.some((provider) => provider.id === selectedProviderId.value)) {
      selectedProviderId.value = providers.value[0]?.id ?? "";
    }
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to load model providers.";
  }
}

async function loadModels(): Promise<void> {
  if (!props.tenantId || !selectedProviderId.value) {
    models.value = [];
    return;
  }
  try {
    models.value = await getModelConfigs(props.tenantId, selectedProviderId.value);
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to load models.";
  }
}

async function addFakeProvider(): Promise<void> {
  busy.value = true;
  error.value = "";
  try {
    const provider = await createFakeProvider(
      props.tenantId,
      providerName.value.trim(),
      fakeResponse.value,
    );
    providers.value = [...providers.value, provider];
    selectedProviderId.value = provider.id;
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to create provider.";
  } finally {
    busy.value = false;
  }
}

async function addOpenAIProvider(): Promise<void> {
  busy.value = true;
  error.value = "";
  try {
    const provider = await createOpenAICompatibleProvider(
      props.tenantId,
      openAIProviderName.value.trim(),
      openAIBaseUrl.value.trim(),
      openAICredentialRef.value.trim(),
    );
    providers.value = [...providers.value, provider];
    selectedProviderId.value = provider.id;
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to create model provider.";
  } finally {
    busy.value = false;
  }
}

async function addEmbeddingModel(): Promise<void> {
  if (!selectedProvider.value || selectedProvider.value.provider_type === "fake") return;
  busy.value = true;
  error.value = "";
  try {
    const model = await createModelConfig(
      props.tenantId,
      selectedProviderId.value,
      embeddingModelName.value.trim(),
      embeddingDisplayName.value.trim(),
      ["embeddings"],
    );
    models.value = [model, ...models.value];
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to create embedding model.";
  } finally {
    busy.value = false;
  }
}

async function addModel(): Promise<void> {
  if (!selectedProviderId.value) return;
  busy.value = true;
  error.value = "";
  try {
    const model = await createModelConfig(
      props.tenantId,
      selectedProviderId.value,
      modelName.value.trim(),
      modelDisplayName.value.trim(),
    );
    models.value = [model, ...models.value];
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to create model.";
  } finally {
    busy.value = false;
  }
}

async function runModelTest(model: ModelConfig): Promise<void> {
  busy.value = true;
  error.value = "";
  testResult.value = null;
  try {
    testResult.value = await testModel(props.tenantId, model.id, testPrompt.value);
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Model test failed.";
  } finally {
    busy.value = false;
  }
}

watch(() => props.tenantId, () => void loadProviders(), { immediate: true });
watch(selectedProviderId, () => void loadModels());
</script>

<template>
  <section class="model-manager">
    <div class="task-panel-heading">
      <div>
        <p class="eyebrow">MODEL GATEWAY</p>
        <h3>Model manager</h3>
      </div>
      <el-tag type="info" effect="plain">{{ providers.length }} providers</el-tag>
    </div>
    <el-alert v-if="error" :title="error" type="error" :closable="true" show-icon @close="error = ''" />
    <div v-if="tenantId" class="model-manager-grid">
      <div class="model-manager-form">
        <h4>Provider registry</h4>
        <el-select v-model="selectedProviderId" placeholder="Select provider" class="full-width">
          <el-option
            v-for="provider in providers"
            :key="provider.id"
            :label="`${provider.name} · ${provider.provider_type}`"
            :value="provider.id"
          />
        </el-select>
        <el-input v-model="providerName" placeholder="New fake provider name" />
        <el-input v-model="fakeResponse" type="textarea" :rows="2" placeholder="Deterministic fake response" />
        <el-button type="primary" plain :loading="busy" @click="addFakeProvider">Add fake provider</el-button>
        <el-divider content-position="left">OpenAI-compatible provider</el-divider>
        <el-input v-model="openAIProviderName" placeholder="Provider name" />
        <el-input v-model="openAIBaseUrl" placeholder="Base URL, e.g. http://ollama:11434/v1" />
        <el-input v-model="openAICredentialRef" placeholder="Credential reference, e.g. env:OLLAMA_KEY (optional locally)" />
        <el-button
          type="primary"
          plain
          :loading="busy"
          :disabled="!openAIProviderName.trim() || !openAIBaseUrl.trim()"
          @click="addOpenAIProvider"
        >Add OpenAI-compatible provider</el-button>
        <p class="task-note">The server must allowlist the endpoint host. Never enter API key values here; use env: references.</p>
      </div>
      <div class="model-manager-form">
        <h4>Model configuration</h4>
        <el-input v-model="modelName" placeholder="Model identifier" />
        <el-input v-model="modelDisplayName" placeholder="Display name" />
        <el-button type="primary" :disabled="!selectedProviderId" :loading="busy" @click="addModel">Add chat model version</el-button>
        <el-divider content-position="left">Embedding model</el-divider>
        <el-input v-model="embeddingModelName" placeholder="Embedding model identifier" />
        <el-input v-model="embeddingDisplayName" placeholder="Embedding display name" />
        <el-button
          type="success"
          plain
           :disabled="!selectedProvider || selectedProvider.provider_type === 'fake'"
          :loading="busy"
          @click="addEmbeddingModel"
        >Add embedding model</el-button>
        <p class="task-note">Embedding models require an OpenAI-compatible provider. Model versions are immutable and credentials remain server-side references.</p>
      </div>
    </div>
    <div v-if="models.length" class="model-list">
      <div v-for="model in models" :key="model.id" class="model-row">
        <div>
          <strong>{{ model.display_name }}</strong>
          <p>{{ model.model_name }} · v{{ model.version }} · {{ model.context_window }} context</p>
        </div>
        <el-button
          v-if="model.capabilities.includes('chat')"
          size="small"
          :loading="busy"
          @click="runModelTest(model)"
        >Test model</el-button>
      </div>
    </div>
    <el-empty v-else description="Select a provider or add a local fake provider" :image-size="72" />
    <div v-if="testResult" class="model-test-result">
      <div class="model-test-meta">
        <el-tag type="success">Connection successful</el-tag>
        <span>{{ testResult.provider }} / {{ testResult.model }}</span>
        <span>{{ testResult.latency_ms }} ms</span>
      </div>
      <p>{{ testResult.content }}</p>
      <span class="task-note">Tokens: {{ testResult.input_tokens }} input / {{ testResult.output_tokens }} output</span>
    </div>
  </section>
</template>
