/**
 * The console's left rail and the chat workspace share one selection: which
 * tenant and project are in scope, which conversation is open, and the
 * conversation list. Keeping it here means the rail and the chat cannot drift
 * apart, and the choice survives a reload the way a chat client's sidebar does.
 */
import { ref } from "vue";
import {
  getChatThreads,
  getProjects,
  getTenants,
  type ChatThread,
  type Project,
  type Tenant,
} from "../services/api";

const STORAGE_KEY = "workspace.selection.v1";

type Persisted = { tenantId: string; projectId: string; threadId: string };

function readPersisted(): Persisted {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return { tenantId: "", projectId: "", threadId: "" };
    const parsed = JSON.parse(raw) as Partial<Persisted>;
    return {
      tenantId: String(parsed.tenantId ?? ""),
      projectId: String(parsed.projectId ?? ""),
      threadId: String(parsed.threadId ?? ""),
    };
  } catch {
    return { tenantId: "", projectId: "", threadId: "" };
  }
}

const persisted = readPersisted();
export const tenants = ref<Tenant[]>([]);
export const projects = ref<Project[]>([]);
export const threads = ref<ChatThread[]>([]);
// A request from the rail ("New conversation") that the chat view consumes;
// the counter makes repeated clicks distinguishable without event plumbing.
export const newChatSignal = ref(0);
export const newChatMode = ref<"chat" | "task">("chat");
export const activeTenantId = ref(persisted.tenantId);
export const activeProjectId = ref(persisted.projectId);
export const activeThreadId = ref(persisted.threadId);

function persist(): void {
  localStorage.setItem(
    STORAGE_KEY,
    JSON.stringify({
      tenantId: activeTenantId.value,
      projectId: activeProjectId.value,
      threadId: activeThreadId.value,
    }),
  );
}

export function setActiveThread(threadId: string): void {
  activeThreadId.value = threadId;
  persist();
}

export async function refreshThreads(): Promise<void> {
  if (!activeTenantId.value || !activeProjectId.value) {
    threads.value = [];
    return;
  }
  try {
    threads.value = (await getChatThreads(activeTenantId.value, activeProjectId.value)).items;
  } catch {
    threads.value = [];
  }
}

export async function loadWorkspace(force = false): Promise<void> {
  if (tenants.value.length && !force) {
    await refreshThreads();
    return;
  }
  const tenantPage = await getTenants();
  tenants.value = tenantPage.results ?? [];
  if (!tenants.value.length) return;
  if (!tenants.value.some((item) => item.id === activeTenantId.value)) {
    activeTenantId.value = tenants.value[0].id;
    activeProjectId.value = "";
    activeThreadId.value = "";
  }
  const projectPage = await getProjects(activeTenantId.value);
  projects.value = projectPage.results ?? [];
  if (!projects.value.some((item) => item.id === activeProjectId.value)) {
    activeProjectId.value = projects.value[0]?.id ?? "";
    activeThreadId.value = "";
  }
  persist();
  await refreshThreads();
}

export async function selectTenant(tenantId: string): Promise<void> {
  if (tenantId === activeTenantId.value) return;
  activeTenantId.value = tenantId;
  activeProjectId.value = "";
  activeThreadId.value = "";
  const projectPage = await getProjects(tenantId);
  projects.value = projectPage.results ?? [];
  activeProjectId.value = projects.value[0]?.id ?? "";
  persist();
  await refreshThreads();
}

export async function selectProject(projectId: string): Promise<void> {
  if (projectId === activeProjectId.value) return;
  activeProjectId.value = projectId;
  activeThreadId.value = "";
  persist();
  await refreshThreads();
}

export function requestNewChat(mode: "chat" | "task" = "chat"): void {
  newChatMode.value = mode;
  newChatSignal.value += 1;
}
