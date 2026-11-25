<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import { t, tf } from "../i18n";
import {
  appendEdge,
  readNodePositions,
  removeNodePosition,
  renameNodePosition,
  writeNodePositions,
} from "../domain/workflowCanvas";
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

// ---------------------------------------------------------------------------
// Canvas engine
//
// The graph is one plane moved by one transform: translate(x, y) scale(k).
// Zoom, pan, fit, minimap and the stored viewport all read and write that
// single state, so nothing here depends on scrollbars, on a second width
// bookkeeping, or on measuring how far down the page the canvas starts.
// ---------------------------------------------------------------------------
const studioRoot = ref<HTMLElement | null>(null);
const viewportEl = ref<HTMLElement | null>(null);
const MIN_ZOOM = 0.15;
const MAX_ZOOM = 2.5;
// Room for the floating dock and the minimap, so a fitted graph never sits under them.
const FIT_INSET = { left: 24, right: 24, top: 24, bottom: 96 };
const DOT_STEP = 24;
const VIEWPORT_PREFIX = "studio.viewport.v1";
const view = ref({ x: 0, y: 0, k: 1 });
// A view created by the user (zoom, pan, stored) outranks automatic fitting
// until Fit is pressed again.
const viewLocked = ref(false);
const minimapOpen = ref(true);
const isFullscreen = ref(false);
const inspectorOpen = ref(false);
// Definition and run history are a drawer, not a second page: the DAG keeps the
// viewport until someone opens them.
const definitionOpen = ref(false);
let viewportTimer: number | undefined;
let resizeObserver: ResizeObserver | null = null;
let pan: { clientX: number; clientY: number; x: number; y: number } | null = null;

const canvasScale = computed(() => view.value.k);
const zoomLabel = computed(() => `${Math.round(view.value.k * 100)}%`);
const canvasTransform = computed(
  () => `translate(${view.value.x}px, ${view.value.y}px) scale(${view.value.k})`,
);
// The dot grid lives on the viewport and follows the same transform, so the
// sheet stays continuous at every zoom and pan position.
const canvasBackdrop = computed(() => ({
  backgroundSize: `${DOT_STEP * view.value.k}px ${DOT_STEP * view.value.k}px`,
  backgroundPosition: `${view.value.x}px ${view.value.y}px`,
}));

function viewportSize(): { width: number; height: number } {
  const el = viewportEl.value;
  return { width: el?.clientWidth ?? 0, height: el?.clientHeight ?? 0 };
}

function clampZoom(value: number): number {
  return Number(Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, value)).toFixed(3));
}

function setView(next: { x: number; y: number; k: number }): void {
  view.value = { x: Math.round(next.x), y: Math.round(next.y), k: clampZoom(next.k) };
}

// Zoom around a point in viewport coordinates: the pointer when the wheel
// drives it, the centre when a toolbar button does.
function zoomAt(nextK: number, clientX?: number, clientY?: number): void {
  const el = viewportEl.value;
  if (!el) return;
  const rect = el.getBoundingClientRect();
  const px = (clientX ?? rect.left + rect.width / 2) - rect.left;
  const py = (clientY ?? rect.top + rect.height / 2) - rect.top;
  const k = clampZoom(nextK);
  const ratio = k / view.value.k;
  setView({ x: px - (px - view.value.x) * ratio, y: py - (py - view.value.y) * ratio, k });
  viewLocked.value = true;
  scheduleSaveViewport();
}

function zoomTo(next: number): void {
  zoomAt(next);
}

function zoomBy(step: number): void {
  zoomAt(view.value.k * step);
}

function fitView(): void {
  const { width, height } = viewportSize();
  const layout = canvasLayout.value;
  const inner = {
    width: width - FIT_INSET.left - FIT_INSET.right,
    height: height - FIT_INSET.top - FIT_INSET.bottom,
  };
  if (!width || !height || !layout.width || !layout.height || inner.width <= 0 || inner.height <= 0) return;
  const k = clampZoom(Math.min(inner.width / layout.width, inner.height / layout.height));
  setView({
    k,
    x: FIT_INSET.left + (inner.width - layout.width * k) / 2,
    y: FIT_INSET.top + (inner.height - layout.height * k) / 2,
  });
  viewLocked.value = false;
  scheduleSaveViewport();
}

// A resize must not hide the graph: keep a hand-made view inside the box.
function clampIntoView(): void {
  const { width, height } = viewportSize();
  const layout = canvasLayout.value;
  if (!width || !height || !layout.width || !layout.height) return;
  const spanX = layout.width * view.value.k;
  const spanY = layout.height * view.value.k;
  const x =
    spanX <= width
      ? (width - spanX) / 2
      : Math.min(FIT_INSET.left, Math.max(width - spanX - FIT_INSET.right, view.value.x));
  const y =
    spanY <= height
      ? (height - spanY) / 2
      : Math.min(FIT_INSET.top, Math.max(height - spanY - FIT_INSET.bottom, view.value.y));
  if (x !== view.value.x || y !== view.value.y) setView({ x, y, k: view.value.k });
}

function onCanvasWheel(event: WheelEvent): void {
  // Canvas tools zoom on wheel; holding a modifier only makes the step coarser
  // so a trackpad flick does not jump two zoom levels.
  const factor = Math.exp(-event.deltaY / (event.ctrlKey || event.metaKey ? 240 : 480));
  zoomAt(view.value.k * factor, event.clientX, event.clientY);
}

function startPan(event: PointerEvent): void {
  selectedNodeId.value = "";
  pan = { clientX: event.clientX, clientY: event.clientY, x: view.value.x, y: view.value.y };
  (event.currentTarget as HTMLElement).setPointerCapture?.(event.pointerId);
  viewLocked.value = true;
}

function onViewportPointerMove(event: PointerEvent): void {
  if (!pan) return;
  setView({
    x: pan.x + (event.clientX - pan.clientX),
    y: pan.y + (event.clientY - pan.clientY),
    k: view.value.k,
  });
}

function onViewportPointerUp(): void {
  if (!pan) return;
  pan = null;
  scheduleSaveViewport();
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
    window.localStorage.setItem(key, JSON.stringify({ x: view.value.x, y: view.value.y, k: view.value.k }));
  } catch {
    /* storage can be full or disabled; the viewport is a convenience, not state */
  }
}

// A stored view belongs to the workflow it was made on; anything unreadable
// falls back to fitting the graph.
function restoreViewport(): void {
  const key = viewportKey();
  if (!key) {
    fitView();
    return;
  }
  const stored = window.localStorage.getItem(key);
  if (!stored) {
    fitView();
    return;
  }
  try {
    const parsed = JSON.parse(stored) as { x?: number; y?: number; k?: number };
    if (!parsed.k) throw new Error("no zoom");
    setView({ x: parsed.x ?? 0, y: parsed.y ?? 0, k: parsed.k });
    viewLocked.value = true;
  } catch {
    window.localStorage.removeItem(key);
    fitView();
  }
}

const minimapNodeStyle = computed(() => (node: { x: number; y: number }): Record<string, string> => {
  const width = Math.max(1, canvasLayout.value.width);
  const height = Math.max(1, canvasLayout.value.height);
  return {
    left: `${(node.x / width) * 100}%`,
    top: `${(node.y / height) * 100}%`,
    width: `${(NODE_WIDTH / width) * 100}%`,
  };
});

// The minimap shows the sheet box and the slice of graph space now on screen,
// both derived from the same transform as the canvas itself.
const minimapViewStyle = computed(() => {
  const layout = canvasLayout.value;
  const { width, height } = viewportSize();
  if (!layout.width || !layout.height || !width || !height) return {};
  const k = view.value.k;
  const graphLeft = Math.min(layout.width, Math.max(0, -view.value.x / k));
  const graphTop = Math.min(layout.height, Math.max(0, -view.value.y / k));
  const graphWidth = Math.max(0, Math.min(layout.width - graphLeft, width / k));
  const graphHeight = Math.max(0, Math.min(layout.height - graphTop, height / k));
  return {
    left: `${(graphLeft / layout.width) * 100}%`,
    top: `${(graphTop / layout.height) * 100}%`,
    width: `${(graphWidth / layout.width) * 100}%`,
    height: `${(graphHeight / layout.height) * 100}%`,
  };
});

function jumpFromMinimap(event: MouseEvent): void {
  const map = event.currentTarget as HTMLElement;
  const box = map.getBoundingClientRect();
  const layout = canvasLayout.value;
  const { width, height } = viewportSize();
  if (!box.width || !box.height || !width || !height) return;
  const ratioX = (event.clientX - box.left) / box.width;
  const ratioY = (event.clientY - box.top) / box.height;
  setView({
    x: width / 2 - ratioX * layout.width * view.value.k,
    y: height / 2 - ratioY * layout.height * view.value.k,
    k: view.value.k,
  });
  viewLocked.value = true;
  scheduleSaveViewport();
}

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
  // The box changes size with the mode, so refit only if the view is untouched.
  void nextTick(() => {
    if (viewLocked.value) clampIntoView();
    else fitView();
  });
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
  } else if (event.key === "Escape" && connectionState.value) {
    cancelConnection();
  } else if (event.key === "Escape" && document.fullscreenElement) {
    void document.exitFullscreen();
  }
}

onMounted(() => {
  const observer = new ResizeObserver(() => {
    if (viewLocked.value) clampIntoView();
    else fitView();
  });
  if (viewportEl.value) observer.observe(viewportEl.value);
  resizeObserver = observer;
  document.addEventListener("fullscreenchange", onFullscreenChange);
  window.addEventListener("keydown", onCanvasKeydown);
  window.addEventListener("pointermove", onViewportPointerMove);
  window.addEventListener("pointerup", onViewportPointerUp);
});

onUnmounted(() => {
  resizeObserver?.disconnect();
  document.removeEventListener("fullscreenchange", onFullscreenChange);
  window.removeEventListener("keydown", onCanvasKeydown);
  window.removeEventListener("pointermove", onViewportPointerMove);
  window.removeEventListener("pointerup", onViewportPointerUp);
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
  nextTick(() => {
    if (viewLocked.value) clampIntoView();
    else fitView();
  });
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
// A connection gesture starts on a node's output port and ends on another node.
// The line is tracked in canvas coordinates so it stays attached while panning
// or zooming during the gesture.
const connectionState = ref<{
  sourceId: string;
  cursorX: number;
  cursorY: number;
} | null>(null);
const connectionTargetId = ref("");
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
    width: Math.max(
      680,
      24 + (maxLayer + 1) * LAYER_GAP + NODE_WIDTH,
      ...Object.values(canvasPositions.value).map((position) => position.x + NODE_WIDTH + 24),
    ),
    height: Math.max(
      260,
      48 + maxRows * ROW_GAP,
      ...Object.values(canvasPositions.value).map((position) => position.y + NODE_HEIGHT + 48),
    ),
  };
});

const connectionLine = computed(() => {
  const state = connectionState.value;
  if (!state) return null;
  const source = canvasLayout.value.nodes.find((item) => item.id === state.sourceId);
  if (!source) return null;
  return {
    x1: source.x + NODE_WIDTH,
    y1: source.y + NODE_HEIGHT / 2,
    x2: state.cursorX,
    y2: state.cursorY,
  };
});

// The graph can arrive after the first paint, e.g. once the draft finishes
// loading, so a new layout is fitted only while the view is still automatic.
watch(canvasLayout, () => nextTick(() => {
  if (!viewLocked.value) fitView();
}));
watch(viewportEl, (el) => {
  if (!el) return;
  nextTick(() => restoreViewport());
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

function startConnection(event: PointerEvent, item: CanvasNode): void {
  connectionState.value = {
    sourceId: item.id,
    cursorX: item.x + NODE_WIDTH,
    cursorY: item.y + NODE_HEIGHT / 2,
  };
  connectionTargetId.value = "";
  selectNode(item.node);
  event.preventDefault();
  event.stopPropagation();
}

function nodeIdAtPoint(clientX: number, clientY: number): string {
  const element = document.elementFromPoint(clientX, clientY);
  const node = element?.closest?.("[data-node-id]");
  return node?.getAttribute("data-node-id") ?? "";
}

function canvasPointFromEvent(event: PointerEvent): { x: number; y: number } {
  const viewport = viewportEl.value;
  if (!viewport) return { x: 0, y: 0 };
  const rect = viewport.getBoundingClientRect();
  return {
    x: (event.clientX - rect.left - view.value.x) / view.value.k,
    y: (event.clientY - rect.top - view.value.y) / view.value.k,
  };
}

function onCanvasPointerMove(event: PointerEvent): void {
  if (connectionState.value) {
    const point = canvasPointFromEvent(event);
    connectionState.value = {
      ...connectionState.value,
      cursorX: point.x,
      cursorY: point.y,
    };
    const targetId = nodeIdAtPoint(event.clientX, event.clientY);
    connectionTargetId.value = targetId === connectionState.value.sourceId ? "" : targetId;
    return;
  }
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

function persistCanvasPositions(): void {
  const definition = parseDefinition();
  if (!definition) return;
  commitDefinition(writeNodePositions(definition, canvasPositions.value));
}

function finishConnection(targetId: string): void {
  const state = connectionState.value;
  connectionState.value = null;
  connectionTargetId.value = "";
  if (!state || !targetId) return;
  const definition = parseDefinition();
  if (!definition) return;
  const nodes = Array.isArray(definition.nodes)
    ? (definition.nodes as Record<string, unknown>[])
    : [];
  const sourceNode = nodes.find((node) => String(node.id) === state.sourceId);
  const edges = Array.isArray(definition.edges)
    ? (definition.edges as Record<string, unknown>[])
    : [];
  let when: boolean | undefined;
  if (String(sourceNode?.type ?? "") === "condition") {
    if (!edges.some((edge) => String(edge.source) === state.sourceId && edge.when === true)) {
      when = true;
    } else if (!edges.some((edge) => String(edge.source) === state.sourceId && edge.when === false)) {
      when = false;
    } else {
      error.value = t("A condition node already has true and false branches.");
      return;
    }
  }
  const result = appendEdge(definition, state.sourceId, targetId, when);
  if (!result.ok) {
    error.value =
      result.reason === "self"
        ? t("A node cannot connect to itself.")
        : result.reason === "duplicate"
          ? t("That edge already exists.")
          : t("Choose a source and target node.");
    return;
  }
  definition.edges = result.edges;
  commitDefinition(definition);
  notice.value = tf("Connected {0} to {1}.", state.sourceId, targetId);
  error.value = "";
}

function cancelConnection(): void {
  connectionState.value = null;
  connectionTargetId.value = "";
}

function onCanvasPointerUp(event?: PointerEvent): void {
  if (dragState.value) {
    dragState.value = null;
    persistCanvasPositions();
    return;
  }
  if (connectionState.value) {
    const targetId = event ? nodeIdAtPoint(event.clientX, event.clientY) : "";
    finishConnection(targetId);
  }
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
  const nextPositions = renameNodePosition(canvasPositions.value, previousId, nextId);
  canvasPositions.value = nextPositions;
  commitDefinition(writeNodePositions(definition, nextPositions));
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
  const removedId = selectedNodeId.value;
  const nextPositions = removeNodePosition(canvasPositions.value, removedId);
  canvasPositions.value = nextPositions;
  commitDefinition(writeNodePositions(definition, nextPositions));
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
  return String(config.capability ?? config.model_config_id ?? config.arguments_from ?? "");
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
  return { capability: "", arguments: {} };
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
    canvasPositions.value = readNodePositions(selectedWorkflow.value.draft_definition);
  } else {
    definitionText.value = "";
    canvasPositions.value = {};
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
  <section ref="studioRoot" class="workflow-studio">
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="true" @close="error = ''" />
    <el-alert v-if="notice" :title="notice" type="success" :closable="true" @close="notice = ''" />
    <el-empty v-if="!props.projectId" description="Select a project to manage workflows" :image-size="72" />
    <div v-else-if="!loading && workflows.length === 0" class="workflow-empty-state">
      <el-empty description="Create a workflow draft to get started" :image-size="72" />
      <el-button type="primary" plain size="small" :loading="saving" @click="createDraft">
        ＋ {{ t("New workflow") }}
      </el-button>
    </div>
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
        <el-button type="primary" plain size="small" :loading="saving" @click="createDraft">
          ＋ {{ t("New workflow") }}
        </el-button>
      </div>
      <section class="workflow-pipeline">
        <el-alert v-if="previewGraph.parseError" :title="previewGraph.parseError" type="warning" :closable="false" />
        <div v-else class="workflow-canvas-grid" :class="{ solo: !inspectorOpen }">
          <div class="workflow-canvas-shell">
          <div
            ref="viewportEl"
            class="workflow-canvas-viewport"
            :style="canvasBackdrop"
            @wheel.prevent="onCanvasWheel"
            @pointerdown.self="startPan"
            @pointerup.self="onViewportPointerUp"
          >
          <div
            class="workflow-canvas-plane"
            :style="{ width: canvasLayout.width + 'px', height: canvasLayout.height + 'px', transform: canvasTransform }"
            @pointerdown.self="startPan"
            @pointerup.self="onViewportPointerUp"
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
              <line
                v-if="connectionLine"
                class="workflow-canvas-connection"
                :x1="connectionLine.x1"
                :y1="connectionLine.y1"
                :x2="connectionLine.x2"
                :y2="connectionLine.y2"
                stroke="#3f8cff"
                stroke-width="2"
                stroke-dasharray="6 4"
              />
            </svg>
            <article
              v-for="item in canvasLayout.nodes"
              :key="item.id"
              class="workflow-canvas-node"
              :class="{
                selected: selectedNodeId === item.id,
                'connection-target': connectionTargetId === item.id,
              }"
              :data-node-id="item.id"
              :style="{ left: item.x + 'px', top: item.y + 'px', width: NODE_WIDTH + 'px' }"
              @pointerdown.stop="startNodeDrag($event, item)"
            >
              <el-tag size="small" effect="plain">{{ nodeType(item.node) }}</el-tag>
              <strong>{{ nodeLabel(item.node) }}</strong>
              <code>{{ item.id }}</code>
              <small v-if="nodeDetail(item.node)">{{ nodeDetail(item.node) }}</small>
              <button
                type="button"
                class="workflow-node-port"
                :title="t('Drag from this port onto another node to connect them.')"
                @pointerdown.stop="startConnection($event, item)"
              />
            </article>
          </div>
          </div>
<div class="canvas-dock">
            <el-popover placement="bottom-start" :width="200" trigger="click" :disabled="!selectedWorkflowId">
              <template #reference>
                <button type="button" class="canvas-tool canvas-tool-text" :disabled="!selectedWorkflowId">
                  ＋ {{ t("Node") }}
                </button>
              </template>
              <div class="canvas-node-menu">
                <el-button size="small" @click="addWorkflowNode('llm')">{{ t("LLM") }}</el-button>
                <el-button size="small" @click="addWorkflowNode('knowledge_search')">{{ t("Knowledge Search") }}</el-button>
                <el-button size="small" @click="addWorkflowNode('tool')">{{ t("Connector Tool") }}</el-button>
                <el-button size="small" @click="addWorkflowNode('condition')">{{ t("Condition") }}</el-button>
                <el-button size="small" @click="addWorkflowNode('agent')">{{ t("Agent") }}</el-button>
                <el-button size="small" @click="addWorkflowNode('approval')">{{ t("Approval Gate") }}</el-button>
                <el-button size="small" @click="addWorkflowNode('loop')">{{ t("Bounded Loop") }}</el-button>
              </div>
            </el-popover>
            <button type="button" class="canvas-tool" :title="t('Zoom out')" @click="zoomBy(1 / 1.2)">－</button>
            <span class="canvas-zoom">{{ zoomLabel }}</span>
            <button type="button" class="canvas-tool" :title="t('Zoom in')" @click="zoomBy(1.2)">＋</button>
            <button type="button" class="canvas-tool canvas-tool-text" :title="t('Fit view')" @click="fitView">
              {{ t("Fit") }}
            </button>
            <button type="button" class="canvas-tool canvas-tool-text canvas-tool-preset" @click="zoomTo(1)">100%</button>
            <button type="button" class="canvas-tool canvas-tool-text canvas-tool-preset" @click="zoomTo(0.5)">50%</button>
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
            <button
              type="button"
              class="canvas-tool canvas-tool-text"
              :class="{ on: definitionOpen }"
              :title="t('Definition and runs')"
              @click="definitionOpen = !definitionOpen"
            >
              {{ t("Definition") }}
            </button>
            <button
              type="button"
              class="canvas-tool canvas-tool-text canvas-tool-preset"
              :disabled="!selectedWorkflowId || validating"
              @click="validateDraft"
            >
              {{ t("Validate") }}
            </button>
            <el-tag class="canvas-count" type="info" effect="plain" size="small">
              {{ tf("{0} nodes · {1} edges", previewGraph.nodes.length, previewGraph.edges.length) }}
            </el-tag>
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
                <span>Capability</span>
                <el-select
                  :model-value="String(configValue('capability') ?? '')"
                  size="small"
                  filterable
                  placeholder="Select an installed capability"
                  @update:model-value="(value: unknown) => setConfigValue('capability', value)"
                >
                  <el-option v-for="tool in catalog.tools" :key="tool.id" :label="tool.name + ' · ' + tool.plugin + ' · ' + tool.risk_level" :value="tool.id" />
                </el-select>
                <span v-if="catalog.tools.length === 0" class="task-note">No capability is installed in this project.</span>
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
      </section>
      <div v-if="definitionOpen" class="workflow-editor-grid workflow-definition-panel">
        <div class="workflow-editor-form">
          <h4>Definition</h4>
          <el-input v-model="workflowName" placeholder="Workflow name" />
          <el-input v-model="workflowDescription" placeholder="Description" />
          <el-input v-model="definitionText" type="textarea" :rows="16" resize="vertical" spellcheck="false" />
          <p class="workflow-help">
            Supported execution nodes: start, llm, knowledge_search, tool, condition, agent, approval, loop and end. Tool nodes reference an installed project capability by name; configure arguments or arguments_from. High-risk tools are rejected until the human-approval execution gate is implemented. Knowledge search retrieves only from pinned project knowledge bases.
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
