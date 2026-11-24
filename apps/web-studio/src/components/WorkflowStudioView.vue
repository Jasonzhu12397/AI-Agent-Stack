<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import { t, tf } from "../i18n";
import {
  createWorkflow,
  createWorkflowRun,
  decideWorkflowStep,
  getWorkflowRuns,
  getWorkflowCatalog,
  getWorkflows,
  publishWorkflow,
  validateWorkflow,
  updateWorkflow,
  type WorkflowCatalog,
  type WorkflowDraft,
  type WorkflowRun,
} from "../services/api";

const props = defineProps<{ tenantId: string; projectId: string }>();

// The DAG claims the remaining viewport instead of a fixed 640px box: measure
// where the canvas starts and expose it as a CSS variable for the stylesheet.
const studioRoot = ref<HTMLElement | null>(null);
const canvasScroll = ref<HTMLElement | null>(null);
const studioVars = ref<Record<string, string>>({});
// The DAG is drawn at fixed node sizes, so a wide screen would leave it tiny in
// the corner. Fit it to the viewport box instead, clamped so a two-node draft
// does not turn into billboards.
const canvasScale = ref(1);
// The painted sheet never shrinks below the visible box, otherwise the dot grid
// stops mid-air and the draft looks pasted into the corner of a blank panel.
const paintWidth = ref(0);
const paintHeight = ref(0);
let resizeObserver: ResizeObserver | null = null;
const MIN_ZOOM = 0.25;
const MAX_ZOOM = 2.5;
const VIEWPORT_PREFIX = "studio.viewport.v1";
const canvasFrame = ref<HTMLElement | null>(null);
const viewLeft = ref(0);
const viewTop = ref(0);
const viewWidth = ref(0);
const viewHeight = ref(0);
// Once the user zooms or restores a stored viewport, automatic fit must stop
// fighting them: resizing the window may re-measure the box but not the view.
const viewLocked = ref(false);
const minimapOpen = ref(true);
const isFullscreen = ref(false);
const inspectorOpen = ref(false);
let viewportTimer: number | undefined;

const scaledWidth = computed(() => Math.round((paintWidth.value || canvasLayout.value.width) * canvasScale.value));
const scaledHeight = computed(() => Math.round((paintHeight.value || canvasLayout.value.height) * canvasScale.value));

function fitCanvas(force = false): void {
  const box = canvasScroll.value;
  if (!box || (viewLocked.value && !force)) return;
  const width = box.clientWidth - 16;
  const height = box.clientHeight - 16;
  const layout = canvasLayout.value;
  if (!width || !height || !layout.width || !layout.height) return;
  const raw = Math.min(width / layout.width, height / layout.height);
  const scale = Math.max(0.6, Math.min(2, Number(raw.toFixed(3))));
  canvasScale.value = scale;
  paintWidth.value = Math.max(layout.width, Math.round(width / scale));
  paintHeight.value = Math.max(layout.height, Math.round(height / scale));
}

function fitStudio(): void {
  const root = studioRoot.value;
  if (!root) return;
  // Measure the canvas box itself once it exists; the +150 estimate drifts
  // because the toolbar above the canvas wraps at narrow widths.
  const boxTop = canvasScroll.value?.getBoundingClientRect().top;
  const offset = Math.round(
    typeof boxTop === "number" && boxTop > 0
      ? boxTop
      : root.getBoundingClientRect().top + 150,
  );
  studioVars.value = { "--studio-canvas-height": `calc(100dvh - ${offset}px - 16px)` };
  fitCanvas();
}

function paintScale(scale: number): void {
  const box = canvasScroll.value;
  canvasScale.value = scale;
  paintWidth.value = Math.max(
    canvasLayout.value.width,
    Math.round((box?.clientWidth ?? 0) / scale),
  );
  paintHeight.value = Math.max(
    canvasLayout.value.height,
    Math.round((box?.clientHeight ?? 0) / scale),
  );
}

function zoomTo(next: number): void {
  const scale = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, Number(next.toFixed(3))));
  paintScale(scale);
  viewLocked.value = true;
  void nextTick(syncViewport);
  scheduleSaveViewport();
}

function zoomBy(step: number): void {
  zoomTo(canvasScale.value * step);
}

function fitView(): void {
  viewLocked.value = false;
  fitCanvas(true);
  void nextTick(() => {
    syncViewport();
    scheduleSaveViewport();
  });
}

function syncViewport(): void {
  const el = canvasScroll.value;
  if (!el) return;
  viewLeft.value = el.scrollLeft;
  viewTop.value = el.scrollTop;
  viewWidth.value = el.clientWidth;
  viewHeight.value = el.clientHeight;
}

function viewportKey(): string {
  if (!selectedWorkflowId.value) return "";
  return `${VIEWPORT_PREFIX}:${props.projectId || "no-project"}:${selectedWorkflowId.value}`;
}

function scheduleSaveViewport(): void {
  if (viewportTimer !== undefined) window.clearTimeout(viewportTimer);
  viewportTimer = window.setTimeout(saveViewport, 250);
}

function saveViewport(): void {
  const key = viewportKey();
  if (!key) return;
  try {
    window.localStorage.setItem(
      key,
      JSON.stringify({ scale: canvasScale.value, left: viewLeft.value, top: viewTop.value }),
    );
  } catch {
    /* storage can be full or disabled; the viewport is a convenience, not state */
  }
}

// Restore the stored view of this workflow once its sheet has been painted.
// A workflow without a stored viewport is fitted to the whole graph instead.
function restoreViewport(): void {
  const key = viewportKey();
  const stored = key ? window.localStorage.getItem(key) : null;
  if (!stored) {
    viewLocked.value = false;
    void nextTick(() => {
      fitCanvas(true);
      void nextTick(() => {
        syncViewport();
        scheduleSaveViewport();
      });
    });
    return;
  }
  try {
    const parsed = JSON.parse(stored) as { scale?: number; left?: number; top?: number };
    if (!parsed.scale) throw new Error("no scale");
    paintScale(Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, parsed.scale)));
    viewLocked.value = true;
    window.requestAnimationFrame(() => {
      const el = canvasScroll.value;
      if (el) {
        el.scrollLeft = Math.max(0, parsed.left ?? 0);
        el.scrollTop = Math.max(0, parsed.top ?? 0);
      }
      syncViewport();
    });
  } catch {
    window.localStorage.removeItem(key);
    fitView();
  }
}

function onCanvasScroll(): void {
  syncViewport();
  scheduleSaveViewport();
}

function jumpFromMinimap(event: MouseEvent): void {
  const map = event.currentTarget as HTMLElement;
  const box = map.getBoundingClientRect();
  const ratioX = (event.clientX - box.left) / box.width;
  const ratioY = (event.clientY - box.top) / box.height;
  const el = canvasScroll.value;
  if (!el) return;
  el.scrollLeft = Math.max(0, ratioX * scaledWidth.value - el.clientWidth / 2);
  el.scrollTop = Math.max(0, ratioY * scaledHeight.value - el.clientHeight / 2);
  syncViewport();
  scheduleSaveViewport();
}

const zoomLabel = computed(() => `${Math.round(canvasScale.value * 100)}%`);

// The minimap draws the sheet as a percentage box, so it stays correct at any
// zoom level without a second coordinate system.
function minimapNodeStyle(node: { x: number; y: number }): Record<string, string> {
  const width = Math.max(1, canvasLayout.value.width);
  const height = Math.max(1, canvasLayout.value.height);
  return {
    left: `${(node.x / width) * 100}%`,
    top: `${(node.y / height) * 100}%`,
    width: `${(NODE_WIDTH / width) * 100}%`,
  };
}

const minimapViewStyle = computed(() => {
  const width = Math.max(1, scaledWidth.value);
  const height = Math.max(1, scaledHeight.value);
  const left = Math.min(100, (viewLeft.value / width) * 100);
  const top = Math.min(100, (viewTop.value / height) * 100);
  return {
    left: `${left}%`,
    top: `${top}%`,
    width: `${Math.min(100 - left, (viewWidth.value / width) * 100)}%`,
    height: `${Math.min(100 - top, (viewHeight.value / height) * 100)}%`,
  };
});

function toggleFocus(): void {
  const root = studioRoot.value;
  if (!root) return;
  if (document.fullscreenElement) {
    void document.exitFullscreen();
    return;
  }
  void root.requestFullscreen?.();
}

function onFullscreenChange(): void {
  isFullscreen.value = Boolean(document.fullscreenElement);
  void nextTick(() => {
    fitStudio();
    if (!viewLocked.value) fitCanvas(true);
    syncViewport();
  });
  window.setTimeout(fitStudio, 120);
}

function onCanvasKeydown(event: KeyboardEvent): void {
  const target = event.target as HTMLElement | null;
  if (target && /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)) return;
  const meta = event.ctrlKey || event.metaKey;
  if (meta && event.key === "1") {
    event.preventDefault();
    fitView();
  } else if (meta && (event.key === "=" || event.key === "+")) {
    event.preventDefault();
    zoomBy(1.2);
  } else if (meta && event.key === "-") {
    event.preventDefault();
    zoomBy(1 / 1.2);
  } else if (event.shiftKey && event.key === "1") {
    event.preventDefault();
    zoomTo(1);
  } else if (event.shiftKey && event.key === "5") {
    event.preventDefault();
    zoomTo(0.5);
  } else if (event.key === "Escape" && document.fullscreenElement) {
    void document.exitFullscreen();
  }
}

onMounted(() => {
  fitStudio();
  window.addEventListener("resize", fitStudio);
  window.addEventListener("resize", () => fitCanvas());
  // The scroll box only has a height once the measured CSS variable lands,
  // so fit again after paint and whenever the graph changes.
  // Fonts, the workflow toolbar and the draft panel all settle over the first
  // frames and each of them moves the canvas box, so measure a few times.
  void nextTick(() => fitStudio());
  window.setTimeout(() => fitStudio(), 150);
  window.setTimeout(() => fitStudio(), 450);
  const observer = new ResizeObserver(() => {
    fitStudio();
    fitCanvas();
  });
  if (canvasScroll.value) observer.observe(canvasScroll.value);
  // Loading a draft grows the toolbar above the canvas, which moves the box
  // down; observing the section keeps the measured height honest.
  const root = studioRoot.value;
  if (root) {
    observer.observe(root);
    for (const child of Array.from(root.children)) observer.observe(child);
  }
  resizeObserver = observer;
  document.addEventListener("fullscreenchange", onFullscreenChange);
  window.addEventListener("keydown", onCanvasKeydown);
});

onUnmounted(() => {
  window.removeEventListener("resize", fitStudio);
  window.removeEventListener("resize", () => fitCanvas());
  resizeObserver?.disconnect();
  document.removeEventListener("fullscreenchange", onFullscreenChange);
  window.removeEventListener("keydown", onCanvasKeydown);
  if (viewportTimer !== undefined) window.clearTimeout(viewportTimer);
});
const workflows = ref<WorkflowDraft[]>([]);
const selectedWorkflowId = ref("");
const selectedWorkflow = ref<WorkflowDraft | null>(null);
const workflowName = ref("");
const workflowDescription = ref("");
const definitionText = ref("");
const runInputText = ref('{"message":"Hello from Workflow Studio"}');
const runs = ref<WorkflowRun[]>([]);
const loading = ref(false);
const saving = ref(false);
const publishing = ref(false);
const running = ref(false);
const error = ref("");
const notice = ref("");
const validating = ref(false);
const decidingNode = ref("");
const selectedNodeId = ref("");
watch(selectedNodeId, (id) => {
  if (id) inspectorOpen.value = true;
});
const toggleInspector = () => {
  inspectorOpen.value = !inspectorOpen.value;
  nextTick(() => fitCanvas());
};
const nodeIdDraft = ref("");
const nodeTypeDraft = ref("llm");
const nodeConfigText = ref("{}");
const edgeSource = ref("");
const edgeTarget = ref("");
const edgeWhen = ref("none");
const canvasPositions = ref<Record<string, { x: number; y: number }>>({});
const dragState = ref<{
  nodeId: string;
  startX: number;
  startY: number;
  originX: number;
  originY: number;
} | null>(null);
const catalog = ref<WorkflowCatalog>({
  models: [],
  knowledge_bases: [],
  tools: [],
  agents: [],
});
const catalogError = ref("");
const validationResult = ref<{
  is_valid: boolean;
  errors: string[];
  execution_layers: string[][];
  node_count: number;
  edge_count: number;
} | null>(null);

const previewGraph = computed(() => {
  try {
    const parsed = JSON.parse(definitionText.value) as Record<string, unknown>;
    return {
      nodes: Array.isArray(parsed.nodes) ? parsed.nodes as Record<string, unknown>[] : [],
      edges: Array.isArray(parsed.edges) ? parsed.edges as Record<string, unknown>[] : [],
      parseError: "",
    };
  } catch {
    return { nodes: [], edges: [], parseError: "The draft is not valid JSON yet." };
  }
});

type CanvasNode = {
  node: Record<string, unknown>;
  id: string;
  x: number;
  y: number;
};

type CanvasEdge = {
  edge: Record<string, unknown>;
  sourceId: string;
  targetId: string;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  when: string;
};

const NODE_WIDTH = 172;
const NODE_HEIGHT = 78;
const LAYER_GAP = 236;
const ROW_GAP = 108;

const canvasLayout = computed(() => {
  const nodes = previewGraph.value.nodes;
  const edges = previewGraph.value.edges;
  const ids = nodes.map((node) => String(node.id ?? "")).filter(Boolean);
  const incoming: Record<string, string[]> = {};
  const outgoing: Record<string, string[]> = {};
  for (const id of ids) {
    incoming[id] = [];
    outgoing[id] = [];
  }
  for (const edge of edges) {
    const source = String(edge.source ?? "");
    const target = String(edge.target ?? "");
    if (source in incoming && target in incoming) {
      incoming[target].push(source);
      outgoing[source].push(target);
    }
  }
  const layers: Record<string, number> = {};
  const queue = ids.filter((id) => incoming[id].length === 0);
  for (const id of queue) layers[id] = 0;
  while (queue.length > 0) {
    const id = queue.shift() as string;
    for (const target of outgoing[id]) {
      layers[target] = Math.max(layers[target] ?? 0, (layers[id] ?? 0) + 1);
      if (ids.indexOf(target) >= 0) {
        incoming[target] = incoming[target].filter((source) => source !== id);
        if (incoming[target].length === 0) queue.push(target);
      }
    }
  }
  for (const id of ids) if (!(id in layers)) layers[id] = 0;
  const rows: Record<number, string[]> = {};
  for (const id of ids) {
    const layer = layers[id] ?? 0;
    rows[layer] = rows[layer] ?? [];
    rows[layer].push(id);
  }
  const positions: Record<string, { x: number; y: number }> = {};
  let maxLayer = 0;
  let maxRows = 1;
  for (const [layerText, rowIds] of Object.entries(rows)) {
    const layer = Number(layerText);
    maxLayer = Math.max(maxLayer, layer);
    maxRows = Math.max(maxRows, rowIds.length);
    rowIds.forEach((id, rowIndex) => {
      positions[id] = canvasPositions.value[id] ?? {
        x: 24 + layer * LAYER_GAP,
        y: 24 + rowIndex * ROW_GAP,
      };
    });
  }
  const canvasNodes: CanvasNode[] = nodes
    .map((node) => {
      const id = String(node.id ?? "");
      return { node, id, x: positions[id]?.x ?? 24, y: positions[id]?.y ?? 24 };
    })
    .filter((item) => item.id);
  const canvasEdges: CanvasEdge[] = [];
  for (const edge of edges) {
    const sourceId = String(edge.source ?? "");
    const targetId = String(edge.target ?? "");
    const source = positions[sourceId];
    const target = positions[targetId];
    if (!source || !target) continue;
    const when = typeof edge.when === "boolean" ? String(edge.when) : "";
    canvasEdges.push({
      edge,
      sourceId,
      targetId,
      x1: source.x + NODE_WIDTH,
      y1: source.y + NODE_HEIGHT / 2,
      x2: target.x,
      y2: target.y + NODE_HEIGHT / 2,
      when,
    });
  }
  return {
    nodes: canvasNodes,
    edges: canvasEdges,
    width: Math.max(680, 24 + (maxLayer + 1) * LAYER_GAP + NODE_WIDTH),
    height: Math.max(260, 48 + maxRows * ROW_GAP),
  };
});

// The graph (and its scroll box) can appear after the first paint, e.g. once
// the draft finishes loading, so refit whenever either one becomes measurable.
watch(canvasLayout, () => nextTick(fitCanvas));
watch(canvasScroll, (el) => {
  if (!el) return;
  nextTick(() => {
    fitStudio();
    fitCanvas();
  });
  resizeObserver?.observe(el);
});

function commitDefinition(definition: Record<string, unknown>): void {
  definitionText.value = JSON.stringify(definition, null, 2);
  validationResult.value = null;
}

function selectNode(node: Record<string, unknown>): void {
  selectedNodeId.value = String(node.id ?? "");
  nodeIdDraft.value = String(node.id ?? "");
  nodeTypeDraft.value = String(node.type ?? "llm");
  nodeConfigText.value = JSON.stringify(node.config ?? {}, null, 2);
}

function startNodeDrag(event: PointerEvent, item: CanvasNode): void {
  selectNode(item.node);
  dragState.value = {
    nodeId: item.id,
    startX: event.clientX,
    startY: event.clientY,
    originX: item.x,
    originY: item.y,
  };
  event.preventDefault();
}

function onCanvasPointerMove(event: PointerEvent): void {
  if (!dragState.value) return;
  // Pointer deltas are screen pixels; the canvas is scaled, so divide them
  // back into layout space or a node drifts away from the cursor.
  const nextX = Math.max(0, dragState.value.originX + (event.clientX - dragState.value.startX) / canvasScale.value);
  const nextY = Math.max(0, dragState.value.originY + (event.clientY - dragState.value.startY) / canvasScale.value);
  canvasPositions.value = {
    ...canvasPositions.value,
    [dragState.value.nodeId]: { x: nextX, y: nextY },
  };
}

function onCanvasPointerUp(): void {
  dragState.value = null;
}

function applyNodeDraft(): void {
  const definition = parseDefinition();
  if (!definition) return;
  const nodes = Array.isArray(definition.nodes)
    ? [...(definition.nodes as Record<string, unknown>[])]
    : [];
  const edges = Array.isArray(definition.edges)
    ? [...(definition.edges as Record<string, unknown>[])]
    : [];
  const index = nodes.findIndex((node) => String(node.id) === selectedNodeId.value);
  if (index < 0) {
    error.value = t("Select a node before applying changes.");
    return;
  }
  const nextId = nodeIdDraft.value.trim();
  if (!/^[A-Za-z0-9_-]{1,120}$/.test(nextId)) {
    error.value = t("Node IDs must match A-Z, a-z, 0-9, underscore or dash, up to 120 characters.");
    return;
  }
  if (nodes.some((node, itemIndex) => itemIndex !== index && String(node.id) === nextId)) {
    error.value = t("Node IDs must be unique.");
    return;
  }
  let config: unknown;
  try {
    config = JSON.parse(nodeConfigText.value || "{}");
  } catch {
    error.value = t("Node config must be valid JSON.");
    return;
  }
  if (typeof config !== "object" || config === null || Array.isArray(config)) {
    error.value = t("Node config must be a JSON object.");
    return;
  }
  const previousId = String(nodes[index].id ?? "");
  nodes[index] = {
    ...nodes[index],
    id: nextId,
    type: nodeTypeDraft.value,
    config,
  };
  const nextEdges = edges.map((edge) => ({
    ...edge,
    source: String(edge.source) === previousId ? nextId : edge.source,
    target: String(edge.target) === previousId ? nextId : edge.target,
  }));
  definition.nodes = nodes;
  definition.edges = nextEdges;
  commitDefinition(definition);
  if (previousId !== nextId && canvasPositions.value[previousId]) {
    const nextPositions = { ...canvasPositions.value, [nextId]: canvasPositions.value[previousId] };
    delete nextPositions[previousId];
    canvasPositions.value = nextPositions;
  }
  selectedNodeId.value = nextId;
  notice.value = tf("Node {0} updated.", nextId);
  error.value = "";
}

function removeSelectedNode(): void {
  if (!selectedNodeId.value) return;
  const definition = parseDefinition();
  if (!definition) return;
  const nodes = Array.isArray(definition.nodes)
    ? (definition.nodes as Record<string, unknown>[])
    : [];
  const edges = Array.isArray(definition.edges)
    ? (definition.edges as Record<string, unknown>[])
    : [];
  definition.nodes = nodes.filter((node) => String(node.id) !== selectedNodeId.value);
  definition.edges = edges.filter(
    (edge) =>
      String(edge.source) !== selectedNodeId.value &&
      String(edge.target) !== selectedNodeId.value,
  );
  commitDefinition(definition);
  selectedNodeId.value = "";
  edgeSource.value = "";
  edgeTarget.value = "";
  notice.value = t("Node removed.");
  error.value = "";
}

function addEdge(): void {
  if (!edgeSource.value || !edgeTarget.value) {
    error.value = t("Choose a source and target node.");
    return;
  }
  if (edgeSource.value === edgeTarget.value) {
    error.value = t("Self edges are not allowed.");
    return;
  }
  const definition = parseDefinition();
  if (!definition) return;
  const edges = Array.isArray(definition.edges)
    ? [...(definition.edges as Record<string, unknown>[])]
    : [];
  if (
    edges.some(
      (edge) =>
        String(edge.source) === edgeSource.value &&
        String(edge.target) === edgeTarget.value,
    )
  ) {
    error.value = t("That edge already exists.");
    return;
  }
  const edge: Record<string, unknown> = {
    source: edgeSource.value,
    target: edgeTarget.value,
  };
  if (edgeWhen.value === "true" || edgeWhen.value === "false") {
    edge.when = edgeWhen.value === "true";
  }
  edges.push(edge);
  definition.edges = edges;
  commitDefinition(definition);
  notice.value = t("Edge added.");
  error.value = "";
}

function removeEdge(index: number): void {
  const definition = parseDefinition();
  if (!definition) return;
  const edges = Array.isArray(definition.edges)
    ? [...(definition.edges as Record<string, unknown>[])]
    : [];
  edges.splice(index, 1);
  definition.edges = edges;
  commitDefinition(definition);
  notice.value = t("Edge removed.");
  error.value = "";
}

function nodeLabel(node: Record<string, unknown>): string {
  return String(node.name ?? node.id ?? "Unnamed node");
}

function nodeType(node: Record<string, unknown>): string {
  return String(node.type ?? "unknown").toUpperCase();
}

function nodeDetail(node: Record<string, unknown>): string {
  if (!node.config || typeof node.config !== "object" || Array.isArray(node.config)) return "";
  const config = node.config as Record<string, unknown>;
  return String(config.tool_id ?? config.model_config_id ?? config.arguments_from ?? "");
}

function edgeLabel(edge: Record<string, unknown>): string {
  return String(edge.source ?? "?") + " → " + String(edge.target ?? "?");
}

type InsertableNodeType =
  | "llm"
  | "knowledge_search"
  | "tool"
  | "condition"
  | "agent"
  | "approval"
  | "loop";

async function loadCatalog(): Promise<void> {
  if (!props.tenantId || !props.projectId) return;
  catalogError.value = "";
  try {
    catalog.value = await getWorkflowCatalog(props.tenantId, props.projectId);
  } catch (cause) {
    catalogError.value =
      cause instanceof Error ? cause.message : "Unable to load workflow references.";
  }
}

/** Config as an object, so a picker writes one key instead of hand-editing JSON. */
const nodeConfig = computed<Record<string, unknown>>(() => {
  try {
    const parsed = JSON.parse(nodeConfigText.value || "{}");
    return typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : {};
  } catch {
    return {};
  }
});

function configValue(key: string): unknown {
  return nodeConfig.value[key];
}

function setConfigValue(key: string, value: unknown): void {
  nodeConfigText.value = JSON.stringify({ ...nodeConfig.value, [key]: value }, null, 2);
}

function insertableConfigFor(type: InsertableNodeType): Record<string, unknown> {
  if (type === "llm") return { model_config_id: "", prompt_template: "{{input}}" };
  if (type === "knowledge_search") {
    return { knowledge_base_ids: [], query_template: "{{input}}", limit: 5 };
  }
  if (type === "approval") return { instructions: "Confirm this workflow step before it continues" };
  if (type === "condition") return { input_key: "input", equals: "" };
  if (type === "agent") return { agent_version_id: "", instructions: "" };
  if (type === "loop") {
    return {
      max_iterations: 3,
      body: {
        type: "llm",
        config: { model_config_id: "", prompt_template: "Refine: {{input}}" },
      },
    };
  }
  return { tool_id: "", arguments: {} };
}

/**
 * Keep the draft insertable regardless of what the editor holds.
 *
 * The previous version refused to act unless the draft already had exactly one
 * end node with exactly one incoming edge, so a fresh or hand-edited draft
 * only ever produced an error. Repairing the skeleton here matches the server
 * rules (one start, at least one end, no dead ends) instead of asking the user
 * to fix JSON by hand.
 */
function repairSkeleton(
  nodes: Record<string, unknown>[],
  edges: Record<string, unknown>[],
): string[] {
  const repairs: string[] = [];
  if (nodes.length === 0) {
    nodes.push({ id: "start", type: "start", config: {} });
    repairs.push("added the missing start node");
  }
  const starts = nodes.filter((node) => node.type === "start");
  const ends = nodes.filter((node) => node.type === "end");
  if (starts.length > 1) {
    nodes.splice(nodes.indexOf(starts[1]), starts.length - 1);
    repairs.push("removed " + String(starts.length - 1) + " duplicate start node(s)");
  }
  if (ends.length === 0) {
    const used = new Set(nodes.map((node) => String(node.id ?? "")));
    let endId = "end";
    let suffix = 2;
    while (used.has(endId)) {
      endId = "end_" + String(suffix);
      suffix += 1;
    }
    nodes.push({ id: endId, type: "end", config: {} });
    const sinks = nodes.filter((node) => {
      const id = String(node.id ?? "");
      if (node.type === "end" || node.type === "start") return false;
      return !edges.some((edge) => edge.source === id);
    });
    const attach = sinks.length > 0 ? sinks : nodes.filter((node) => node.type === "start");
    for (const node of attach) {
      edges.push({ source: String(node.id ?? ""), target: endId });
    }
    repairs.push("added the missing end node and connected it");
  } else if (ends.length > 1) {
    const keep = ends[0];
    for (const duplicate of ends.slice(1)) {
      const duplicateId = String(duplicate.id ?? "");
      for (const edge of edges.filter((item) => item.target === duplicateId)) {
        edge.target = String(keep.id ?? "");
      }
      for (let index = edges.length - 1; index >= 0; index -= 1) {
        if (edges[index].source === duplicateId) edges.splice(index, 1);
      }
      nodes.splice(nodes.indexOf(duplicate), 1);
    }
    repairs.push("merged " + String(ends.length - 1) + " extra end node(s) into one");
  }
  const orphanEnds = nodes.filter(
    (node) => node.type === "end" && !edges.some((edge) => edge.target === String(node.id ?? "")),
  );
  for (const orphan of orphanEnds) {
    const candidates = nodes.filter(
      (node) =>
        node !== orphan && node.type !== "end" && !edges.some((edge) => edge.source === String(node.id ?? "")),
    );
    const source = candidates.length > 0 ? candidates[candidates.length - 1] : nodes.find((node) => node.type === "start");
    if (source) {
      edges.push({ source: String(source.id ?? ""), target: String(orphan.id ?? "") });
      repairs.push("connected the unreachable end node");
    }
  }
  return repairs;
}

function addWorkflowNode(type: InsertableNodeType): void {
  if (!selectedWorkflowId.value) {
    error.value = t("Select or create a workflow draft before inserting nodes.");
    return;
  }
  const definition = parseDefinition();
  if (!definition) return;
  const nodes = Array.isArray(definition.nodes)
    ? [...definition.nodes as Record<string, unknown>[]]
    : [];
  const edges = Array.isArray(definition.edges)
    ? [...definition.edges as Record<string, unknown>[]]
    : [];
  const repairs = repairSkeleton(nodes, edges);
  const endNode = nodes.filter((node) => node.type === "end")[0];
  const endId = String(endNode.id ?? "");
  const incoming = edges.filter((edge) => edge.target === endId);
  if (incoming.length === 0) {
    error.value = tf("The end node has no incoming path; add an edge from a node to {0} first.", endId);
    return;
  }
  // Every incoming branch gets its own processing node. A single join node
  // cannot be used here: a condition node may not send both guarded branches
  // to the same target, so two rewired edges would be a duplicate edge and the
  // draft would be rejected at validation.
  const insertedIds: string[] = [];
  const newNodes: Record<string, unknown>[] = [];
  for (const edge of incoming) {
    const nodeId = type + "_" + crypto.randomUUID().replace(/-/g, "").slice(0, 12);
    const node = { id: nodeId, type, config: insertableConfigFor(type) };
    edge.target = nodeId;
    edges.push({ source: nodeId, target: endId });
    insertedIds.push(nodeId);
    newNodes.push(node);
  }
  nodes.splice(nodes.indexOf(endNode), 0, ...newNodes);
  definition.nodes = nodes;
  definition.edges = edges;
  definitionText.value = JSON.stringify(definition, null, 2);
  validationResult.value = null;
  const prefix = repairs.length > 0 ? tf("Draft repaired ({0}). ", repairs.join("; ")) : "";
  const multiple = incoming.length > 1
    ? tf(
      " One node was inserted on each of the {0} branches into {1}, because guarded branches cannot share a single join node.",
      incoming.length,
      endId,
    )
    : "";
  notice.value =
    prefix +
    tf("{0} node inserted before {1} ({2}).", type.toUpperCase(), endId, insertedIds.join(", ")) +
    multiple +
    t(" Pick its references in Node inspector, then validate.");
  error.value = "";
}

async function decideStep(runId: string, nodeId: string, decision: "approve" | "reject"): Promise<void> {
  decidingNode.value = runId + ":" + nodeId;
  error.value = "";
  notice.value = "";
  try {
    await decideWorkflowStep(runId, nodeId, decision, "");
    notice.value = tf(
      decision === "approve" ? "Workflow step {0} approved." : "Workflow step {0} rejected.",
      nodeId,
    );
    await loadRuns();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to decide the workflow step.";
  } finally {
    decidingNode.value = "";
  }
}

async function validateDraft(): Promise<void> {
  if (!selectedWorkflowId.value) return;
  validating.value = true;
  error.value = "";
  validationResult.value = null;
  try {
    validationResult.value = await validateWorkflow(selectedWorkflowId.value);
    if (validationResult.value.is_valid) {
      notice.value = tf(
        "DAG validation passed. {0} nodes and {1} edges.",
        validationResult.value.node_count,
        validationResult.value.edge_count,
      );
    } else {
      error.value = validationResult.value.errors.join("; ");
    }
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Workflow validation failed.";
  } finally {
    validating.value = false;
  }
}
let refreshTimer: number | undefined;

function initialDefinition(): Record<string, unknown> {
  return {
    nodes: [
      { id: "start", type: "start", config: {} },
      { id: "end", type: "end", config: {} },
    ],
    edges: [{ source: "start", target: "end" }],
  };
}

function selectWorkflow(workflowId: string): void {
  selectedWorkflowId.value = workflowId;
  selectedWorkflow.value =
    workflows.value.find((item) => item.id === workflowId) ?? null;
  if (selectedWorkflow.value) {
    workflowName.value = selectedWorkflow.value.name;
    workflowDescription.value = selectedWorkflow.value.description;
    definitionText.value = JSON.stringify(
      selectedWorkflow.value.draft_definition,
      null,
      2,
    );
  } else {
    definitionText.value = "";
  }
  restoreViewport();
  runs.value = [];
  validationResult.value = null;
  if (workflowId) void loadRuns();
}

async function loadWorkflows(): Promise<void> {
  if (!props.tenantId || !props.projectId) {
    workflows.value = [];
    selectedWorkflow.value = null;
    selectedWorkflowId.value = "";
    runs.value = [];
    return;
  }
  loading.value = true;
  error.value = "";
  try {
    workflows.value = await getWorkflows(props.tenantId, props.projectId);
    const current = workflows.value.find(
      (item) => item.id === selectedWorkflowId.value,
    );
    if (current) {
      selectWorkflow(current.id);
    } else if (workflows.value.length > 0) {
      selectWorkflow(workflows.value[0].id);
    } else {
      selectWorkflow("");
    }
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to load workflows.";
  } finally {
    loading.value = false;
  }
}

async function loadRuns(): Promise<void> {
  if (!selectedWorkflowId.value) return;
  try {
    runs.value = await getWorkflowRuns(selectedWorkflowId.value);
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to load workflow runs.";
  }
}

async function createDraft(): Promise<void> {
  if (!props.tenantId || !props.projectId) return;
  saving.value = true;
  error.value = "";
  notice.value = "";
  try {
    const created = await createWorkflow(props.tenantId, props.projectId, {
      name: "New workflow",
      slug: "workflow-" + crypto.randomUUID().slice(0, 8),
      description: "Created in Workflow Studio",
      draft_definition: initialDefinition(),
    });
    workflows.value = [...workflows.value, created];
    selectWorkflow(created.id);
    notice.value = t("Draft created. Configure the workflow definition and save it.");
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to create workflow.";
  } finally {
    saving.value = false;
  }
}

function parseDefinition(): Record<string, unknown> | null {
  try {
    const parsed: unknown = JSON.parse(definitionText.value);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      throw new Error("Workflow definition must be a JSON object.");
    }
    return parsed as Record<string, unknown>;
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Invalid JSON definition.";
    return null;
  }
}

async function saveDraft(): Promise<void> {
  if (!selectedWorkflowId.value) return;
  const definition = parseDefinition();
  if (!definition) return;
  saving.value = true;
  error.value = "";
  notice.value = "";
  try {
    const updated = await updateWorkflow(selectedWorkflowId.value, {
      name: workflowName.value.trim(),
      description: workflowDescription.value.trim(),
      draft_definition: definition,
    });
    workflows.value = workflows.value.map((item) =>
      item.id === updated.id ? updated : item,
    );
    selectedWorkflow.value = updated;
    validationResult.value = null;
    notice.value = t("Draft saved. Published versions remain immutable.");
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to save workflow.";
  } finally {
    saving.value = false;
  }
}

async function publishDraft(): Promise<void> {
  if (!selectedWorkflowId.value) return;
  await saveDraft();
  if (error.value) return;
  publishing.value = true;
  error.value = "";
  notice.value = "";
  try {
    const version = await publishWorkflow(selectedWorkflowId.value);
    await loadWorkflows();
    notice.value = tf("Published immutable version v{0}.", version.version);
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Workflow publish failed.";
  } finally {
    publishing.value = false;
  }
}

async function runWorkflow(): Promise<void> {
  if (!selectedWorkflowId.value || !selectedWorkflow.value?.current_version) return;
  let input: unknown;
  try {
    input = JSON.parse(runInputText.value);
  } catch {
    error.value = t("Run input must be valid JSON.");
    return;
  }
  if (typeof input !== "object" || input === null || Array.isArray(input)) {
    error.value = t("Run input must be a JSON object.");
    return;
  }
  running.value = true;
  error.value = "";
  notice.value = "";
  try {
    const run = await createWorkflowRun(
      selectedWorkflowId.value,
      input as Record<string, unknown>,
    );
    runs.value = [run, ...runs.value.filter((item) => item.id !== run.id)];
    notice.value = tf(
      "Workflow run {0} queued. The worker starts it within a second; Execution history shows each node's result.",
      run.id.slice(0, 8),
    );
    await loadRuns();
    window.setTimeout(() => void loadRuns(), 1000);
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to start workflow.";
  } finally {
    running.value = false;
  }
}

watch(definitionText, () => {
  validationResult.value = null;
});

watch(
  () => [props.tenantId, props.projectId],
  () => {
    void loadWorkflows();
    void loadCatalog();
  },
);

onMounted(() => {
  void loadWorkflows();
  void loadCatalog();
  window.addEventListener("pointermove", onCanvasPointerMove);
  window.addEventListener("pointerup", onCanvasPointerUp);
  refreshTimer = window.setInterval(() => void loadRuns(), 5000);
});
onUnmounted(() => {
  if (refreshTimer !== undefined) window.clearInterval(refreshTimer);
  window.removeEventListener("pointermove", onCanvasPointerMove);
  window.removeEventListener("pointerup", onCanvasPointerUp);
});
</script>

<template>
  <section ref="studioRoot" class="workflow-studio" :style="studioVars">
    <div class="workflow-heading">
      <div>
        <p class="eyebrow">WORKFLOW ORCHESTRATION</p>
        <h3>Workflow Studio</h3>
      </div>
      <el-button type="primary" plain :disabled="!props.projectId" :loading="saving" @click="createDraft">
        ＋ New workflow
      </el-button>
    </div>
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="true" @close="error = ''" />
    <el-alert v-if="notice" :title="notice" type="success" :closable="true" @close="notice = ''" />
    <el-empty v-if="!props.projectId" description="Select a project to manage workflows" :image-size="72" />
    <el-empty v-else-if="!loading && workflows.length === 0" description="Create a workflow draft to get started" :image-size="72" />
    <template v-else-if="selectedWorkflow">
      <div class="workflow-selector">
        <el-select :model-value="selectedWorkflowId" placeholder="Select workflow" class="full-width" @change="selectWorkflow">
          <el-option
            v-for="workflow in workflows"
            :key="workflow.id"
            :label="workflow.name + ' · v' + workflow.current_version"
            :value="workflow.id"
          />
        </el-select>
        <el-tag type="info" effect="plain">
          {{ selectedWorkflow.is_archived ? t("ARCHIVED") : selectedWorkflow.current_version ? tf("PUBLISHED v{0}", selectedWorkflow.current_version) : t("DRAFT ONLY") }}
        </el-tag>
      </div>
      <section class="workflow-pipeline">
        <div class="workflow-run-heading">
          <div>
            <h4>Workflow DAG</h4>
            <p class="task-note">Live visual preview of the editable draft. Server validation remains authoritative.</p>
          </div>
          <el-tag type="info" effect="plain">{{ tf("{0} nodes · {1} edges", previewGraph.nodes.length, previewGraph.edges.length) }}</el-tag>
        </div>
        <div class="workflow-node-toolbar">
          <span class="task-note">
            {{ selectedWorkflowId ? "Insert node before the end node:" : "Select or create a draft to insert nodes:" }}
          </span>
          <el-button size="small" :disabled="!selectedWorkflowId" @click="addWorkflowNode('llm')">＋ LLM</el-button>
          <el-button size="small" :disabled="!selectedWorkflowId" @click="addWorkflowNode('knowledge_search')">＋ Knowledge Search</el-button>
          <el-button size="small" :disabled="!selectedWorkflowId" @click="addWorkflowNode('tool')">＋ Connector Tool</el-button>
          <el-button size="small" :disabled="!selectedWorkflowId" @click="addWorkflowNode('condition')">＋ Condition</el-button>
          <el-button size="small" :disabled="!selectedWorkflowId" @click="addWorkflowNode('agent')">＋ Agent</el-button>
          <el-button size="small" :disabled="!selectedWorkflowId" @click="addWorkflowNode('approval')">＋ Approval Gate</el-button>
          <el-button size="small" :disabled="!selectedWorkflowId" @click="addWorkflowNode('loop')">＋ Bounded Loop</el-button>
        </div>
        <el-alert v-if="previewGraph.parseError" :title="previewGraph.parseError" type="warning" :closable="false" />
        <div v-else class="workflow-canvas-grid" :class="{ solo: !inspectorOpen }">
          <div class="workflow-canvas-shell">
          <div ref="canvasScroll" class="workflow-canvas-scroll" @scroll.passive="onCanvasScroll">
          <div ref="canvasFrame" class="workflow-canvas-frame" :style="{ width: scaledWidth + 'px', height: scaledHeight + 'px' }">
          <div
            class="workflow-canvas"
            :style="{ width: (paintWidth || canvasLayout.width) + 'px', height: (paintHeight || canvasLayout.height) + 'px', transform: 'scale(' + canvasScale + ')' }"
            @pointerdown.self="selectedNodeId = ''"
          >
            <svg
              class="workflow-canvas-edges"
              :width="canvasLayout.width"
              :height="canvasLayout.height"
            >
              <defs>
                <marker
                  id="workflow-arrow"
                  viewBox="0 0 10 10"
                  refX="9"
                  refY="5"
                  markerWidth="6"
                  markerHeight="6"
                  orient="auto-start-reverse"
                >
                  <path d="M 0 0 L 10 5 L 0 10 z" fill="#5d7398" />
                </marker>
              </defs>
              <line
                v-for="(edge, index) in canvasLayout.edges"
                :key="index"
                :x1="edge.x1"
                :y1="edge.y1"
                :x2="edge.x2"
                :y2="edge.y2"
                stroke="#5d7398"
                stroke-width="1.5"
                marker-end="url(#workflow-arrow)"
              />
              <text
                v-for="(edge, index) in canvasLayout.edges.filter((item) => item.when)"
                :key="'when-' + index"
                :x="(edge.x1 + edge.x2) / 2"
                :y="(edge.y1 + edge.y2) / 2 - 4"
                fill="#8ea2c4"
                font-size="10"
                text-anchor="middle"
              >
                when {{ edge.when }}
              </text>
            </svg>
            <article
              v-for="item in canvasLayout.nodes"
              :key="item.id"
              class="workflow-canvas-node"
              :class="{ selected: selectedNodeId === item.id }"
              :style="{ left: item.x + 'px', top: item.y + 'px', width: NODE_WIDTH + 'px' }"
              @pointerdown.stop="startNodeDrag($event, item)"
            >
              <el-tag size="small" effect="plain">{{ nodeType(item.node) }}</el-tag>
              <strong>{{ nodeLabel(item.node) }}</strong>
              <code>{{ item.id }}</code>
              <small v-if="nodeDetail(item.node)">{{ nodeDetail(item.node) }}</small>
            </article>
          </div>
          </div>
          </div>
          <div class="canvas-toolbar">
            <button type="button" class="canvas-tool" :title="t('Zoom out')" @click="zoomBy(1 / 1.2)">－</button>
            <span class="canvas-zoom">{{ zoomLabel }}</span>
            <button type="button" class="canvas-tool" :title="t('Zoom in')" @click="zoomBy(1.2)">＋</button>
            <button type="button" class="canvas-tool canvas-tool-text" :title="t('Fit view')" @click="fitView">
              {{ t("Fit") }}
            </button>
            <button type="button" class="canvas-tool canvas-tool-text" @click="zoomTo(1)">100%</button>
            <button type="button" class="canvas-tool canvas-tool-text" @click="zoomTo(0.5)">50%</button>
            <button
              type="button"
              class="canvas-tool canvas-tool-text"
              :class="{ on: minimapOpen }"
              :title="t('Minimap')"
              @click="minimapOpen = !minimapOpen"
            >
              ▦
            </button>
            <button
              type="button"
              class="canvas-tool"
              :class="{ on: inspectorOpen }"
              :title="t('Inspector')"
              @click="toggleInspector()"
            >
              ▤
            </button>
            <button type="button" class="canvas-tool" :title="t('Fullscreen')" @click="toggleFocus">
              {{ isFullscreen ? "⤡" : "⤢" }}
            </button>
          </div>
          <div v-if="minimapOpen" class="canvas-minimap" :title="t('Minimap')" @click="jumpFromMinimap">
            <span
              v-for="node in canvasLayout.nodes"
              :key="'map-' + node.id"
              class="minimap-node"
              :style="minimapNodeStyle(node)"
            />
            <span class="minimap-view" :style="minimapViewStyle" />
          </div>
          </div>
          <aside v-if="inspectorOpen" class="workflow-node-inspector">
            <div class="workflow-run-heading">
              <h4>Node inspector</h4>
              <el-tag v-if="selectedNodeId" size="small" effect="plain">{{ selectedNodeId }}</el-tag>
            </div>
            <el-empty v-if="!selectedNodeId" description="Select a node on the canvas" :image-size="48" />
            <template v-else>
              <label class="workflow-field">
                <span>Node ID</span>
                <el-input v-model="nodeIdDraft" size="small" spellcheck="false" />
              </label>
              <label class="workflow-field">
                <span>Node type</span>
                <el-select v-model="nodeTypeDraft" size="small">
                  <el-option v-for="type in ['start','llm','condition','knowledge_search','tool','agent','approval','loop','end']" :key="type" :label="type" :value="type" />
                </el-select>
              </label>
              <div v-if="catalogError" class="task-note">{{ catalogError }}</div>
              <label v-if="nodeTypeDraft === 'llm' || nodeTypeDraft === 'loop'" class="workflow-field">
                <span>Model config</span>
                <el-select
                  :model-value="String(configValue('model_config_id') ?? '')"
                  size="small"
                  filterable
                  placeholder="Select a model"
                  @update:model-value="(value: unknown) => setConfigValue('model_config_id', value)"
                >
                  <el-option v-for="model in catalog.models" :key="model.id" :label="model.label + ' · ' + model.provider" :value="model.id" />
                </el-select>
                <span v-if="catalog.models.length === 0" class="task-note">No model config in this tenant yet; add one in Model Manager.</span>
              </label>
              <label v-if="nodeTypeDraft === 'knowledge_search'" class="workflow-field">
                <span>Knowledge bases</span>
                <el-select
                  :model-value="(configValue('knowledge_base_ids') as string[]) ?? []"
                  size="small"
                  multiple
                  filterable
                  placeholder="Select 1-8 knowledge bases"
                  @update:model-value="(value: unknown) => setConfigValue('knowledge_base_ids', value)"
                >
                  <el-option v-for="base in catalog.knowledge_bases" :key="base.id" :label="base.name" :value="base.id" />
                </el-select>
              </label>
              <label v-if="nodeTypeDraft === 'tool'" class="workflow-field">
                <span>Connector tool</span>
                <el-select
                  :model-value="String(configValue('tool_id') ?? '')"
                  size="small"
                  filterable
                  placeholder="Select an installed tool"
                  @update:model-value="(value: unknown) => setConfigValue('tool_id', value)"
                >
                  <el-option v-for="tool in catalog.tools" :key="tool.id" :label="tool.name + ' · ' + tool.plugin + ' · ' + tool.risk_level" :value="tool.id" />
                </el-select>
                <span v-if="catalog.tools.length === 0" class="task-note">No approved tool is installed in this project.</span>
              </label>
              <label v-if="nodeTypeDraft === 'agent'" class="workflow-field">
                <span>Agent version</span>
                <el-select
                  :model-value="String(configValue('agent_version_id') ?? '')"
                  size="small"
                  filterable
                  placeholder="Select a published agent"
                  @update:model-value="(value: unknown) => setConfigValue('agent_version_id', value)"
                >
                  <el-option v-for="agent in catalog.agents" :key="agent.id" :label="agent.name + ' · v' + agent.version" :value="agent.id" />
                </el-select>
              </label>
              <label class="workflow-field">
                <span>Config (JSON)</span>
                <el-input v-model="nodeConfigText" type="textarea" :rows="7" resize="vertical" spellcheck="false" />
              </label>
              <div class="workflow-actions">
                <el-button size="small" type="primary" plain @click="applyNodeDraft">Apply node</el-button>
                <el-button size="small" type="danger" plain @click="removeSelectedNode">Delete node</el-button>
              </div>
            </template>
            <div class="workflow-edge-editor">
              <h4>Edges</h4>
              <div class="workflow-edge-form">
                <el-select v-model="edgeSource" size="small" placeholder="Source">
                  <el-option v-for="node in previewGraph.nodes" :key="'s-' + String(node.id)" :label="String(node.id)" :value="String(node.id)" />
                </el-select>
                <el-select v-model="edgeTarget" size="small" placeholder="Target">
                  <el-option v-for="node in previewGraph.nodes" :key="'t-' + String(node.id)" :label="String(node.id)" :value="String(node.id)" />
                </el-select>
                <el-select v-model="edgeWhen" size="small" placeholder="when">
                  <el-option label="no guard" value="none" />
                  <el-option label="when true" value="true" />
                  <el-option label="when false" value="false" />
                </el-select>
                <el-button size="small" @click="addEdge">Add edge</el-button>
              </div>
              <div class="workflow-edge-list">
                <span v-for="(edge, index) in previewGraph.edges" :key="index">
                  {{ edgeLabel(edge) }}
                  <button class="workflow-edge-remove" type="button" @click="removeEdge(index)">×</button>
                </span>
              </div>
            </div>
          </aside>
        </div>
        <div v-if="validationResult" class="workflow-validation-result">
          <div class="workflow-run-heading">
            <strong>Server validation</strong>
            <el-tag :type="validationResult.is_valid ? 'success' : 'danger'">
              {{ validationResult.is_valid ? "VALID" : "INVALID" }}
            </el-tag>
          </div>
          <div v-if="validationResult.is_valid" class="workflow-edge-list">
            <span v-for="(layer, index) in validationResult.execution_layers" :key="index">
              Layer {{ index + 1 }}: {{ layer.join(", ") }}
            </span>
          </div>
          <el-alert v-for="(item, index) in validationResult.errors" :key="index" :title="item" type="error" :closable="false" />
        </div>
        <div class="workflow-actions">
          <el-button :loading="validating" :disabled="!selectedWorkflowId" @click="validateDraft">Validate DAG</el-button>
        </div>
      </section>
      <div class="workflow-editor-grid">
        <div class="workflow-editor-form">
          <h4>Definition</h4>
          <el-input v-model="workflowName" placeholder="Workflow name" />
          <el-input v-model="workflowDescription" placeholder="Description" />
          <el-input v-model="definitionText" type="textarea" :rows="16" resize="vertical" spellcheck="false" />
          <p class="workflow-help">
            Supported execution nodes: start, llm, knowledge_search, tool, condition, agent, approval, loop and end. Tool nodes reference an installed, approved project connector tool by tool_id; configure arguments or arguments_from. High-risk tools are rejected until the human-approval execution gate is implemented. Knowledge search retrieves only from pinned project knowledge bases.
            Loop nodes repeat an inline llm/tool/knowledge_search/agent body up to max_iterations (2–10) and persist each iteration in the run state; a body output of exactly {"continue": false} stops early. Approval nodes suspend the run in task status waiting_approval until a project administrator approves or rejects them from the execution history. The server validates graph connectivity, cycles, model and connector scope, replay safety and credential fields before publishing.
          </p>
          <div class="workflow-actions">
            <el-button :loading="saving" @click="saveDraft">Save draft</el-button>
            <el-button type="primary" :loading="publishing" :disabled="selectedWorkflow.is_archived" @click="publishDraft">
              Publish version
            </el-button>
          </div>
        </div>
        <div class="workflow-runner">
          <h4>Run published version</h4>
          <el-input v-model="runInputText" type="textarea" :rows="5" resize="vertical" spellcheck="false" />
          <el-button type="success" :loading="running" :disabled="!selectedWorkflow.current_version" @click="runWorkflow">
            Run workflow
          </el-button>
          <div class="workflow-run-list">
            <div class="workflow-run-heading">
              <h4>Execution history</h4>
              <el-button text @click="loadRuns">Refresh</el-button>
            </div>
            <el-empty v-if="runs.length === 0" description="No workflow runs yet" :image-size="56" />
            <article v-for="run in runs" :key="run.id" class="workflow-run-card">
              <div class="workflow-run-meta">
                <code>{{ run.id.slice(0, 8) }}</code>
                <el-tag size="small" :type="run.task_status === 'succeeded' ? 'success' : run.task_status === 'failed' ? 'danger' : 'info'">
                  {{ run.task_status }}
                </el-tag>
                <span>{{ new Date(run.created_at).toLocaleString() }}</span>
              </div>
              <el-alert
                v-if="run.task_error"
                :title="run.task_error.code ? run.task_error.code + ': ' + (run.task_error.message || '') : String(run.task_error)"
                type="error"
                :closable="false"
                show-icon
              />
              <pre class="workflow-run-output">{{ run.output_data === null ? "No output yet" : JSON.stringify(run.output_data, null, 2) }}</pre>
              <div class="workflow-step-list">
                <span v-for="step in run.steps" :key="step.id" class="workflow-step-entry">
                  {{ step.node_id }} · {{ step.status }}
                  <template v-if="step.status === 'awaiting_approval'">
                    <el-button
                      size="small"
                      type="success"
                      :loading="decidingNode === run.id + ':' + step.node_id"
                      @click="decideStep(run.id, step.node_id, 'approve')"
                    >Approve</el-button>
                    <el-button
                      size="small"
                      type="danger"
                      :loading="decidingNode === run.id + ':' + step.node_id"
                      @click="decideStep(run.id, step.node_id, 'reject')"
                    >Reject</el-button>
                  </template>
                </span>
              </div>
            </article>
          </div>
        </div>
      </div>
    </template>
  </section>
</template>
