import { ref, watch } from "vue";
import { zhCN } from "./zh";

export type Locale = "en" | "zh";
const STORAGE_KEY = "ai-agent-studio-locale";

function initialLocale(): Locale {
  const stored = window.localStorage.getItem(STORAGE_KEY);
  if (stored === "zh" || stored === "en") return stored;
  return navigator.language.toLowerCase().startsWith("zh") ? "zh" : "en";
}

export const locale = ref<Locale>(initialLocale());

export function setLocale(next: Locale): void {
  locale.value = next;
}

export function t(source: string): string {
  if (locale.value === "en") return source;
  return zhCN[source.replace(/\s+/g, " ").trim()] ?? source;
}

watch(locale, (next) => {
  window.localStorage.setItem(STORAGE_KEY, next);
  document.documentElement.lang = next === "zh" ? "zh-CN" : "en";
});

const ATTRS = ["placeholder", "aria-label", "title", "label", "description"] as const;
const SKIP_TAGS = new Set(["SCRIPT", "STYLE", "PRE", "CODE", "TEXTAREA"]);
const originalText = new WeakMap<Text, string>();
const originalAttrs = new WeakMap<Element, Map<string, string>>();
let translating = false;
let observer: MutationObserver | null = null;

function normalize(value: string): string {
  return value.replace(/\s+/g, " ").trim();
}

function translateValue(value: string): string {
  const trimmed = value.trim();
  const translated = t(trimmed);
  if (translated === trimmed) return value;
  const start = value.indexOf(trimmed);
  return value.slice(0, start) + translated + value.slice(start + trimmed.length);
}

function translateTextNode(node: Text): void {
  const current = node.data;
  if (!normalize(current)) return;
  if (locale.value === "en") {
    const original = originalText.get(node);
    if (original !== undefined && current !== original) node.data = original;
    return;
  }
  if (zhCN[normalize(current)] === undefined && !/\b[A-Za-z]{3,}\b/.test(normalize(current))) {
    return;
  }
  const original = originalText.get(node);
  if (original === undefined || normalize(current) !== normalize(translateValue(original))) {
    originalText.set(node, current);
  }
  const source = originalText.get(node) ?? current;
  if (zhCN[normalize(source)] === undefined) return;
  const next = translateValue(source);
  if (next !== current) node.data = next;
}

function translateAttributes(element: Element): void {
  let stored = originalAttrs.get(element);
  if (!stored) {
    stored = new Map();
    originalAttrs.set(element, stored);
  }
  for (const name of ATTRS) {
    const current = element.getAttribute(name);
    if (current === null || !current.trim() || current.includes("{{")) continue;
    if (locale.value === "en") {
      const original = stored.get(name);
      if (original !== undefined && current !== original) element.setAttribute(name, original);
      continue;
    }
    if (!stored.has(name)) stored.set(name, current);
    const source = stored.get(name) ?? current;
    const next = translateValue(source);
    if (next !== current) element.setAttribute(name, next);
  }
}

function translateElement(element: Element): void {
  // Attributes still localize on skipped tags: a textarea's placeholder is copy,
  // not user input, and leaving it English breaks the switch.
  translateAttributes(element);
  if (SKIP_TAGS.has(element.tagName)) return;
  for (const child of Array.from(element.childNodes)) {
    if (child.nodeType === Node.TEXT_NODE) translateTextNode(child as Text);
    else if (child.nodeType === Node.ELEMENT_NODE) translateElement(child as Element);
  }
}

export function translateDom(root: ParentNode = document.body): void {
  translating = true;
  try {
    if (root.nodeType === Node.TEXT_NODE) translateTextNode(root as unknown as Text);
    else translateElement(root as Element);
  } finally {
    translating = false;
  }
}

export function installI18n(root: HTMLElement): void {
  document.documentElement.lang = locale.value === "zh" ? "zh-CN" : "en";
  translateDom(root);
  observer = new MutationObserver((mutations) => {
    if (translating || locale.value !== "zh") return;
    translating = true;
    try {
      for (const mutation of mutations) {
        if (mutation.type === "characterData" && mutation.target.nodeType === Node.TEXT_NODE) {
          translateTextNode(mutation.target as Text);
        } else if (mutation.type === "attributes" && mutation.target.nodeType === Node.ELEMENT_NODE) {
          translateAttributes(mutation.target as Element);
        } else {
          mutation.addedNodes.forEach((node) => {
            if (node.nodeType === Node.TEXT_NODE) translateTextNode(node as Text);
            else if (node.nodeType === Node.ELEMENT_NODE) translateElement(node as Element);
          });
        }
      }
    } finally {
      translating = false;
    }
  });
  observer.observe(root, {
    childList: true,
    subtree: true,
    characterData: true,
    attributes: true,
    attributeFilter: [...ATTRS],
  });
  watch(locale, (next) => {
    if (next === "en") {
      translateDom(root);
    } else {
      translateDom(root);
    }
  });
}
