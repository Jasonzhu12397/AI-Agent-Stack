export type CanvasPosition = { x: number; y: number };
export type CanvasPositions = Record<string, CanvasPosition>;

const MAX_COORDINATE = 100000;
const UI_KEY = "ui";
const POSITIONS_KEY = "node_positions";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function normalizeCoordinate(value: unknown): number | null {
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  return Math.min(MAX_COORDINATE, Math.max(0, Math.round(value)));
}

/**
 * Read persisted canvas positions from a workflow definition.
 *
 * Layout data lives under `ui.node_positions`, never inside a node config, so
 * the runtime graph remains free of presentation-only fields. Entries that do
 * not describe a node in this definition are ignored, which keeps a renamed or
 * deleted node from resurrecting stale coordinates.
 */
export function readNodePositions(definition: unknown): CanvasPositions {
  if (!isRecord(definition)) return {};
  const nodeIds = new Set(
    (Array.isArray(definition.nodes) ? definition.nodes : [])
      .filter(isRecord)
      .map((node) => String(node.id ?? ""))
      .filter(Boolean),
  );
  const ui = isRecord(definition.ui) ? definition.ui : null;
  const raw = ui && isRecord(ui[POSITIONS_KEY]) ? ui[POSITIONS_KEY] : null;
  if (!raw) return {};
  const positions: CanvasPositions = {};
  for (const [nodeId, value] of Object.entries(raw)) {
    if (!nodeIds.has(nodeId) || !isRecord(value)) continue;
    const x = normalizeCoordinate(value.x);
    const y = normalizeCoordinate(value.y);
    if (x === null || y === null) continue;
    positions[nodeId] = { x, y };
  }
  return positions;
}

/**
 * Write canvas positions into a workflow definition without mutating the input.
 * Positions for nodes that are no longer in the graph are dropped, and an empty
 * map removes the `ui` block entirely so drafts without layout stay clean.
 */
export function writeNodePositions(
  definition: Record<string, unknown>,
  positions: CanvasPositions,
): Record<string, unknown> {
  const nodeIds = new Set(
    (Array.isArray(definition.nodes) ? definition.nodes : [])
      .filter(isRecord)
      .map((node) => String(node.id ?? ""))
      .filter(Boolean),
  );
  const nextPositions: CanvasPositions = {};
  for (const [nodeId, value] of Object.entries(positions)) {
    if (!nodeIds.has(nodeId)) continue;
    const x = normalizeCoordinate(value.x);
    const y = normalizeCoordinate(value.y);
    if (x === null || y === null) continue;
    nextPositions[nodeId] = { x, y };
  }
  const next = { ...definition };
  const ui = isRecord(next[UI_KEY]) ? { ...next[UI_KEY] } : {};
  if (Object.keys(nextPositions).length > 0) {
    ui[POSITIONS_KEY] = nextPositions;
    next[UI_KEY] = ui;
  } else {
    delete ui[POSITIONS_KEY];
    if (Object.keys(ui).length > 0) next[UI_KEY] = ui;
    else delete next[UI_KEY];
  }
  return next;
}

/**
 * Move a stored position when a node is renamed. The old key is removed so a
 * later node that reuses the previous id cannot inherit the old coordinates.
 */
export function renameNodePosition(
  positions: CanvasPositions,
  previousId: string,
  nextId: string,
): CanvasPositions {
  if (previousId === nextId || !positions[previousId]) return positions;
  const next = { ...positions };
  next[nextId] = next[previousId];
  delete next[previousId];
  return next;
}

/** Drop a stored position after a node is deleted. */
export function removeNodePosition(
  positions: CanvasPositions,
  nodeId: string,
): CanvasPositions {
  if (!positions[nodeId]) return positions;
  const next = { ...positions };
  delete next[nodeId];
  return next;
}

export type EdgeDraft = Record<string, unknown>;

export type AppendEdgeResult =
  | { ok: true; edges: EdgeDraft[] }
  | { ok: false; reason: "self" | "duplicate" | "missing" };

/**
 * Append an edge with the same guards the server applies to self edges and
 * duplicate routes. The function is pure so the drag gesture can reject an
 * invalid drop before touching the editor text.
 */
export function appendEdge(
  definition: Record<string, unknown>,
  source: string,
  target: string,
  when?: boolean,
): AppendEdgeResult {
  const edges = Array.isArray(definition.edges)
    ? (definition.edges as EdgeDraft[]).filter(isRecord)
    : [];
  const nodeIds = new Set(
    (Array.isArray(definition.nodes) ? definition.nodes : [])
      .filter(isRecord)
      .map((node) => String(node.id ?? ""))
      .filter(Boolean),
  );
  if (!source || !target || !nodeIds.has(source) || !nodeIds.has(target)) {
    return { ok: false, reason: "missing" };
  }
  if (source === target) return { ok: false, reason: "self" };
  if (
    edges.some(
      (edge) => String(edge.source ?? "") === source && String(edge.target ?? "") === target,
    )
  ) {
    return { ok: false, reason: "duplicate" };
  }
  const edge: EdgeDraft = { source, target };
  if (typeof when === "boolean") edge.when = when;
  return { ok: true, edges: [...edges, edge] };
}
