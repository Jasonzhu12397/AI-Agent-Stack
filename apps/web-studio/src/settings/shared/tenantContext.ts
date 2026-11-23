import { ref } from "vue";
import { getProjects, getTenants, type Project, type Tenant } from "../../services/api";

const TENANT_KEY = "ai-agent-studio-settings-tenant";
const PROJECT_KEY = "ai-agent-studio-settings-project";

export const tenants = ref<Tenant[]>([]);
export const projects = ref<Project[]>([]);
export const activeTenantId = ref("");
export const activeProjectId = ref("");
export const tenantContextError = ref("");

function readStored(key: string): string {
  try {
    return window.localStorage.getItem(key) ?? "";
  } catch {
    return "";
  }
}

function writeStored(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    /* storage may be unavailable; the in-memory selection still works */
  }
}

export function selectTenant(tenantId: string): void {
  activeTenantId.value = tenantId;
  writeStored(TENANT_KEY, tenantId);
  activeProjectId.value = "";
}

export function selectProject(projectId: string): void {
  activeProjectId.value = projectId;
  writeStored(PROJECT_KEY, projectId);
}

/** Loads the tenant/project context once; falls back to the first tenant when storage is empty. */
export async function ensureTenantContext(): Promise<void> {
  if (tenants.value.length > 0 && activeTenantId.value) return;
  tenantContextError.value = "";
  try {
    const page = await getTenants();
    tenants.value = page.results;
    const stored = readStored(TENANT_KEY);
    const preferred = tenants.value.find((tenant) => tenant.id === stored) ?? tenants.value[0];
    if (preferred) selectTenant(preferred.id);
    if (activeTenantId.value) {
      const projectPage = await getProjects(activeTenantId.value);
      projects.value = projectPage.results;
      const storedProject = readStored(PROJECT_KEY);
      const preferredProject =
        projects.value.find((project) => project.id === storedProject) ?? projects.value[0];
      if (preferredProject) selectProject(preferredProject.id);
    }
  } catch (cause) {
    tenantContextError.value = cause instanceof Error ? cause.message : "Tenant context unavailable.";
  }
}
