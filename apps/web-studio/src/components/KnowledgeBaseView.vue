<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from "vue";
import {
  createKnowledgeBase,
  createKnowledgeDocument,
  getKnowledgeBases,
  getKnowledgeDocuments,
  getModelConfigs,
  getModelProviders,
  type KnowledgeBase,
  type KnowledgeDocument,
  type ModelConfig,
} from "../services/api";

const props = defineProps<{ tenantId: string; projectId: string }>();
const bases = ref<KnowledgeBase[]>([]);
const docs = ref<KnowledgeDocument[]>([]);
const models = ref<ModelConfig[]>([]);
const baseId = ref("");
const modelId = ref("");
const name = ref("Operations knowledge");
const slug = ref("operations");
const title = ref("");
const content = ref("");
const error = ref("");
const busy = ref(false);
let timer: number | undefined;

async function loadDocs(): Promise<void> {
  if (!baseId.value) {
    docs.value = [];
    return;
  }
  try {
    docs.value = (await getKnowledgeDocuments(baseId.value)).results;
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to load documents.";
  }
}

async function load(): Promise<void> {
  if (!props.tenantId || !props.projectId) return;
  try {
    const [basePage, providers] = await Promise.all([
      getKnowledgeBases(props.tenantId, props.projectId),
      getModelProviders(props.tenantId),
    ]);
    bases.value = basePage.results;
    baseId.value = bases.value[0]?.id ?? "";
    const pages = await Promise.all(
      providers.map((provider) => getModelConfigs(props.tenantId, provider.id)),
    );
    models.value = pages.flat().filter((model) => model.capabilities.includes("embeddings"));
    modelId.value = models.value[0]?.id ?? "";
    await loadDocs();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to load knowledge data.";
  }
}

async function addBase(): Promise<void> {
  if (!modelId.value) return;
  busy.value = true;
  error.value = "";
  try {
    const item = await createKnowledgeBase(props.tenantId, props.projectId, {
      name: name.value.trim(),
      slug: slug.value.trim(),
      description: "",
      embedding_model_id: modelId.value,
      chunk_size: 1200,
      chunk_overlap: 160,
    });
    bases.value = [...bases.value, item];
    baseId.value = item.id;
    await loadDocs();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to create knowledge base.";
  } finally {
    busy.value = false;
  }
}

async function addDocument(): Promise<void> {
  if (!baseId.value || !title.value.trim() || !content.value.trim()) return;
  busy.value = true;
  error.value = "";
  try {
    await createKnowledgeDocument(baseId.value, title.value.trim(), content.value);
    title.value = "";
    content.value = "";
    await loadDocs();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to queue ingestion.";
  } finally {
    busy.value = false;
  }
}

watch(baseId, () => void loadDocs());
watch(() => [props.tenantId, props.projectId], () => void load());
onMounted(() => {
  void load();
  timer = window.setInterval(() => void loadDocs(), 5000);
});
onUnmounted(() => {
  if (timer !== undefined) window.clearInterval(timer);
});
</script>

<template>
  <section class="agent-studio">
    <div class="task-panel-heading">
      <div><p class="eyebrow">RAG KNOWLEDGE</p><h3>Knowledge bases</h3></div>
      <el-tag type="info" effect="plain">{{ bases.length }} bases</el-tag>
    </div>
    <el-alert v-if="error" :title="error" type="error" :closable="true" @close="error = ''" />
    <el-empty v-if="!projectId" description="Select a project first" :image-size="72" />
    <template v-else>
      <div class="agent-builder-grid">
        <div class="agent-builder-form">
          <h4>Create knowledge base</h4>
          <el-input v-model="name" placeholder="Name" />
          <el-input v-model="slug" placeholder="Slug" />
          <el-select v-model="modelId" placeholder="Embedding model" class="full-width">
            <el-option
              v-for="model in models"
              :key="model.id"
              :label="model.display_name"
              :value="model.id"
            />
          </el-select>
          <el-button type="primary" :disabled="!modelId" :loading="busy" @click="addBase">
            Create base
          </el-button>
        </div>
        <div class="agent-builder-form">
          <h4>Ingest text</h4>
          <el-select v-model="baseId" placeholder="Knowledge base" class="full-width">
            <el-option v-for="item in bases" :key="item.id" :label="item.name" :value="item.id" />
          </el-select>
          <el-input v-model="title" placeholder="Document title" />
          <el-input
            v-model="content"
            type="textarea"
            :rows="6"
            maxlength="1000000"
            show-word-limit
            placeholder="Paste a runbook or document (up to 1 MB)."
          />
          <el-button
            type="success"
            :disabled="!baseId || !title.trim() || !content.trim()"
            :loading="busy"
            @click="addDocument"
          >Queue ingestion</el-button>
        </div>
      </div>
      <h4>Documents</h4>
      <el-table v-if="docs.length" :data="docs" size="small">
        <el-table-column prop="title" label="Document" min-width="180" />
        <el-table-column prop="status" label="Status" width="120" />
        <el-table-column prop="chunk_count" label="Chunks" width="90" />
      </el-table>
      <el-empty v-else description="No documents yet" :image-size="72" />
      <p class="task-note">Worker ingestion stores chunks in Qdrant. Only text input is enabled in this milestone.</p>
    </template>
  </section>
</template>
