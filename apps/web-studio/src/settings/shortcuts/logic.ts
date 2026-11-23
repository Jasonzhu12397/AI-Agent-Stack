export type ShortcutRegistryEntry = {
  id: string;
  default_combination: string;
  scope: string;
  description: string;
};

export type ShortcutBindings = Record<string, string>;

const MODIFIER_ORDER = ["Ctrl", "Alt", "Shift", "Meta"] as const;
const MODIFIER_ALIASES: Record<string, (typeof MODIFIER_ORDER)[number]> = {
  ctrl: "Ctrl",
  control: "Ctrl",
  ctl: "Ctrl",
  alt: "Alt",
  option: "Alt",
  opt: "Alt",
  shift: "Shift",
  meta: "Meta",
  cmd: "Meta",
  command: "Meta",
  win: "Meta",
  super: "Meta",
};

const NAMED_KEYS = new Set([
  "enter",
  "escape",
  "esc",
  "tab",
  "space",
  "backspace",
  "delete",
  "up",
  "down",
  "left",
  "right",
  "pageup",
  "pagedown",
  "home",
  "end",
  "f1",
  "f2",
  "f3",
  "f4",
  "f5",
  "f6",
  "f7",
  "f8",
  "f9",
  "f10",
  "f11",
  "f12",
]);

/**
 * Canonicalises user input so "shift+ctrl+k" and "Ctrl+Shift+K" collide as intended.
 * Returns "" for anything not recognisable as a shortcut.
 */
export function normalizeCombination(input: string): string {
  if (typeof input !== "string") return "";
  const parts = input
    .split("+")
    .map((part) => part.trim())
    .filter(Boolean);
  if (parts.length === 0) return "";
  const modifiers: string[] = [];
  const keys: string[] = [];
  for (const part of parts) {
    const alias = MODIFIER_ALIASES[part.toLowerCase()];
    if (alias) {
      if (!modifiers.includes(alias)) modifiers.push(alias);
      continue;
    }
    keys.push(part);
  }
  if (keys.length !== 1) return "";
  const rawKey = keys[0];
  const lower = rawKey.toLowerCase();
  let key: string;
  if (rawKey.length === 1) {
    key = rawKey.toUpperCase();
  } else if (NAMED_KEYS.has(lower)) {
    key = lower
      .split(" ")
      .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
      .join(" ");
  } else {
    return "";
  }
  const ordered = MODIFIER_ORDER.filter((modifier) => modifiers.includes(modifier));
  return [...ordered, key].join("+");
}

export function validateCombination(input: string): { ok: boolean; code: string } {
  const normalized = normalizeCombination(input);
  if (!normalized) return { ok: false, code: "SHORTCUT_INVALID_COMBINATION" };
  const hasModifier = MODIFIER_ORDER.some((modifier) => normalized.startsWith(`${modifier}+`) || normalized.includes(`+${modifier}+`));
  const key = normalized.split("+").pop() ?? "";
  if (!hasModifier && key.length === 1) return { ok: false, code: "SHORTCUT_INVALID_COMBINATION" };
  return { ok: true, code: "" };
}

export function findConflicts(bindings: ShortcutBindings): { id: string; combination: string; conflictsWith: string[] }[] {
  const byCombination = new Map<string, string[]>();
  for (const [id, combination] of Object.entries(bindings)) {
    const normalized = normalizeCombination(combination);
    if (!normalized) continue;
    const existing = byCombination.get(normalized) ?? [];
    existing.push(id);
    byCombination.set(normalized, existing);
  }
  const conflicts: { id: string; combination: string; conflictsWith: string[] }[] = [];
  for (const [combination, ids] of byCombination.entries()) {
    if (ids.length < 2) continue;
    for (const id of ids) {
      conflicts.push({ id, combination, conflictsWith: ids.filter((other) => other !== id) });
    }
  }
  return conflicts.sort((left, right) => left.id.localeCompare(right.id));
}

/** Defaults come from the server registry; user overrides replace them only when valid. */
export function buildBindings(registry: ShortcutRegistryEntry[], overrides: ShortcutBindings | null | undefined): ShortcutBindings {
  const bindings: ShortcutBindings = {};
  for (const entry of registry) {
    bindings[entry.id] = normalizeCombination(entry.default_combination) || entry.default_combination;
  }
  if (overrides && typeof overrides === "object") {
    for (const [id, combination] of Object.entries(overrides)) {
      if (!(id in bindings)) continue;
      const normalized = normalizeCombination(combination);
      if (normalized) bindings[id] = normalized;
    }
  }
  return bindings;
}

export function describeError(code: string, details?: Record<string, unknown>): string {
  switch (code) {
    case "SHORTCUT_INVALID_COMBINATION":
      return "That key combination is not valid. Use modifiers plus a key, for example Ctrl+Shift+K.";
    case "SHORTCUT_CONFLICT":
      return "Another shortcut already uses this combination.";
    case "HTTP_403":
      return "Only the signed-in user can change their own shortcuts.";
    default:
      return code ? `Request failed (${code}${details ? `: ${JSON.stringify(details)}` : ""}).` : "Request failed.";
  }
}

export const pageLabels = [
  "Keyboard Shortcuts",
  "Shortcut reference",
  "Default combination",
  "Your combination",
  "Reset to default",
  "Conflicting shortcut",
  "Shortcuts are stored per user",
  "Shortcut registry unavailable",
];
