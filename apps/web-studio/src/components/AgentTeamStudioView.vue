<script setup lang="ts">
import { t, tf } from "../i18n";
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import {
  createAgentTeam,
  createAgentTeamRun,
  getAgentTeamRuns,
  getAgentTeams,
  getAgents,
  publishAgentTeam,
  updateAgentTeam,
  type AgentDraft,
  type AgentTeamDraft,
  type AgentTeamRun,
} from "../services/api";

const props = defineProps<{ tenantId: string; projectId: string }>();

type MemberDraft = {
  agent_id: string;
  role: string;
  instructions: string;
  route_label: string;
};

const teams = ref<AgentTeamDraft[]>([]);
const agents = ref<AgentDraft[]>([]);
const runs = ref<AgentTeamRun[]>([]);
const selectedTeamId = ref("");
const strategy = ref<"sequential" | "coordinator" | "parallel" | "router">("sequential");
const teamName = ref("New agent team");
const teamSlug = ref("new-agent-team");
const teamDescription = ref("");
const members = ref<MemberDraft[]>([]);
const coordinatorAgentId = ref("");
const coordinatorInstructions = ref(
  "Combine teammate outputs into one final answer.",
);
const routerAgentId = ref("");
const routerInstructions = ref(
  "Choose the single specialist that owns this request.",
);
const newMemberAgentId = ref("");
const newMemberRole = ref("specialist");
const newMemberRouteLabel = ref("");
const newMemberInstructions = ref(
  "Handle your part of the task and return a concise result.",
);
const runInput = ref("");
const busy = ref(false);
const error = ref("");
let refreshTimer: number | undefined;

const selectedTeam = computed(
  () => teams.value.find((team) => team.id === selectedTeamId.value) ?? null,
);
const agentNameById = computed(() => {
  const map = new Map<string, string>();
  for (const agent of agents.value) map.set(agent.id, agent.name);
  return map;
});
const availableAgents = computed(() =>
  agents.value.filter(
    (agent) =>
      agent.current_version > 0 &&
      agent.id !== coordinatorAgentId.value &&
      agent.id !== routerAgentId.value &&
      !members.value.some((member) => member.agent_id === agent.id),
  ),
);
const coordinatorCandidates = computed(() =>
  agents.value.filter(
    (agent) =>
      agent.current_version > 0 &&
      agent.id !== routerAgentId.value &&
      !members.value.some((member) => member.agent_id === agent.id),
  ),
);
const routerCandidates = computed(() =>
  agents.value.filter(
    (agent) =>
      agent.current_version > 0 &&
      agent.id !== coordinatorAgentId.value &&
      agent.id !== routerAgentId.value &&
      !members.value.some((member) => member.agent_id === agent.id),
  ),
);

function draftConfig(): Record<string, unknown> {
  const config: Record<string, unknown> = {
    strategy: strategy.value,
    members: members.value.map((member, index) => ({
      agent_id: member.agent_id,
      role: member.role,
      instructions: member.instructions,
      route_label: member.route_label,
      order: index,
    })),
  };
  if (strategy.value === "coordinator" || strategy.value === "parallel") {
    config.coordinator = {
      agent_id: coordinatorAgentId.value,
      instructions: coordinatorInstructions.value,
    };
  }
  if (strategy.value === "router") {
    config.router = {
      agent_id: routerAgentId.value,
      instructions: routerInstructions.value,
    };
  }
  return config;
}

function hydrateTeam(team: AgentTeamDraft | null): void {
  if (!team) return;
  const config = (team.draft_config ?? {}) as {
    strategy?: "sequential" | "coordinator" | "parallel" | "router";
    members?: MemberDraft[];
    coordinator?: { agent_id?: string; instructions?: string };
    router?: { agent_id?: string; instructions?: string };
  };
  strategy.value =
    config.strategy === "coordinator" ||
    config.strategy === "parallel" ||
    config.strategy === "router"
      ? config.strategy
      : "sequential";
  members.value = (config.members ?? []).map((member) => ({
    agent_id: String(member.agent_id),
    role: String(member.role ?? ""),
    instructions: String(member.instructions ?? ""),
    route_label: String(member.route_label ?? ""),
  }));
  coordinatorAgentId.value = String(config.coordinator?.agent_id ?? "");
  coordinatorInstructions.value = String(
    config.coordinator?.instructions ??
      "Combine teammate outputs into one final answer.",
  );
  routerAgentId.value = String(config.router?.agent_id ?? "");
  routerInstructions.value = String(
    config.router?.instructions ??
      "Choose the single specialist that owns this request.",
  );
  teamName.value = team.name;
  teamSlug.value = team.slug;
  teamDescription.value = team.description;
}

async function loadWorkspace(): Promise<void> {
  teams.value = [];
  agents.value = [];
  runs.value = [];
  selectedTeamId.value = "";
  members.value = [];
  coordinatorAgentId.value = "";
  routerAgentId.value = "";
  if (!props.tenantId || !props.projectId) return;
  error.value = "";
  try {
    const [teamList, agentList] = await Promise.all([
      getAgentTeams(props.tenantId, props.projectId),
      getAgents(props.tenantId, props.projectId),
    ]);
    teams.value = teamList;
    agents.value = agentList;
    if (teams.value.length > 0) {
      selectedTeamId.value = teams.value[0].id;
      hydrateTeam(teams.value[0]);
    }
  } catch (cause) {
    error.value =
      cause instanceof Error ? cause.message : "Unable to load Agent Teams.";
  }
}

async function loadRuns(): Promise<void> {
  if (!selectedTeamId.value) {
    runs.value = [];
    return;
  }
  try {
    runs.value = await getAgentTeamRuns(selectedTeamId.value);
  } catch (cause) {
    error.value =
      cause instanceof Error ? cause.message : "Unable to load team runs.";
  }
}

function addMember(): void {
  if (!newMemberAgentId.value || !newMemberInstructions.value.trim()) return;
  if (
    members.value.some((member) => member.agent_id === newMemberAgentId.value)
  )
    return;
  if (members.value.length >= 6) {
    error.value = t("A team can contain at most six members.");
    return;
  }
  members.value = [
    ...members.value,
    {
      agent_id: newMemberAgentId.value,
      role: newMemberRole.value.trim() || "specialist",
      instructions: newMemberInstructions.value.trim(),
      route_label: newMemberRouteLabel.value.trim().toLowerCase(),
    },
  ];
  newMemberAgentId.value = "";
  newMemberRouteLabel.value = "";
  newMemberInstructions.value =
    t("Handle your part of the task and return a concise result.");
}

function removeMember(index: number): void {
  members.value = members.value.filter((_, position) => position !== index);
}

function validateDraft(): string {
  if (!teamName.value.trim() || !teamSlug.value.trim())
    return "Team name and slug are required.";
  if (members.value.length === 0) return "Add at least one specialist agent.";
  if (
    (strategy.value === "coordinator" || strategy.value === "parallel") &&
    !coordinatorAgentId.value
  ) {
    return "Coordinator strategy requires a coordinator agent.";
  }
  if (
    (strategy.value === "coordinator" || strategy.value === "parallel") &&
    !coordinatorInstructions.value.trim()
  ) {
    return "Coordinator instructions are required.";
  }
  if (strategy.value === "router" && !routerAgentId.value) {
    return "Router strategy requires a router agent.";
  }
  if (strategy.value === "router") {
    const labels = members.value.map((member) => member.route_label).filter(Boolean);
    if (labels.some((label) => !/^[A-Za-z0-9_-]{1,40}$/.test(label))) {
      return "Every route label must use letters, numbers, underscore or hyphen.";
    }
    if (labels.length !== members.value.length) {
      return "Every member needs a route label in router mode.";
    }
    if (new Set(labels).size !== labels.length) {
      return "Route labels must be unique.";
    }
    if (members.value.some((member) => member.route_label === "sequential")) {
      return "Route label is reserved.";
    }
  }
  return "";
}

async function saveDraft(): Promise<AgentTeamDraft | null> {
  const validationError = validateDraft();
  if (validationError) {
    error.value = validationError;
    return null;
  }
  if (!props.tenantId || !props.projectId) return null;
  try {
    if (selectedTeam.value) {
      const updated = await updateAgentTeam(selectedTeam.value.id, {
        name: teamName.value.trim(),
        slug: teamSlug.value.trim(),
        description: teamDescription.value,
        draft_config: draftConfig(),
      });
      teams.value = teams.value.map((team) =>
        team.id === updated.id ? updated : team,
      );
      return updated;
    }
    const created = await createAgentTeam(props.tenantId, props.projectId, {
      name: teamName.value.trim(),
      slug: teamSlug.value.trim(),
      description: teamDescription.value,
      draft_config: draftConfig(),
    });
    teams.value = [...teams.value, created];
    selectedTeamId.value = created.id;
    return created;
  } catch (cause) {
    error.value =
      cause instanceof Error ? cause.message : "Unable to save team draft.";
    return null;
  }
}

async function saveOnly(): Promise<void> {
  busy.value = true;
  error.value = "";
  try {
    await saveDraft();
  } finally {
    busy.value = false;
  }
}

async function publishTeam(): Promise<void> {
  busy.value = true;
  error.value = "";
  try {
    const team = await saveDraft();
    if (!team) return;
    const version = await publishAgentTeam(team.id);
    teams.value = teams.value.map((item) =>
      item.id === team.id
        ? { ...item, current_version: version.version }
        : item,
    );
  } catch (cause) {
    error.value =
      cause instanceof Error ? cause.message : "Unable to publish team.";
  } finally {
    busy.value = false;
  }
}

async function runTeam(): Promise<void> {
  if (!selectedTeam.value || !runInput.value.trim()) return;
  busy.value = true;
  error.value = "";
  try {
    await createAgentTeamRun(selectedTeam.value.id, runInput.value.trim());
    runInput.value = "";
    await loadRuns();
  } catch (cause) {
    error.value =
      cause instanceof Error ? cause.message : "Unable to start team run.";
  } finally {
    busy.value = false;
  }
}

watch(selectedTeamId, (teamId) => {
  if (!teamId) return;
  hydrateTeam(teams.value.find((team) => team.id === teamId) ?? null);
  void loadRuns();
});
watch(
  () => [props.tenantId, props.projectId],
  () => void loadWorkspace(),
);
onMounted(() => {
  void loadWorkspace();
  refreshTimer = window.setInterval(() => void loadRuns(), 3000);
});
onUnmounted(() => {
  if (refreshTimer !== undefined) window.clearInterval(refreshTimer);
});
</script>

<template>
  <section class="agent-team-studio">
    <div class="task-panel-heading">
      <div>
        <p class="eyebrow">MULTI-AGENT ORCHESTRATION</p>
        <h3>Agent Teams</h3>
      </div>
      <el-tag type="info" effect="plain">{{ teams.length }} teams</el-tag>
    </div>
    <el-alert
      v-if="error"
      :title="error"
      type="error"
      :closable="true"
      show-icon
      @close="error = ''"
    />
    <el-empty
      v-if="!projectId"
      description="Select a project to build teams"
      :image-size="72"
    />
    <template v-else>
      <div class="agent-builder-grid">
        <div class="agent-builder-form">
          <h4>Team definition</h4>
          <el-select
            v-model="selectedTeamId"
            placeholder="Select team"
            class="full-width"
          >
            <el-option
              v-for="team in teams"
              :key="team.id"
              :label="`${team.name} · v${team.current_version}`"
              :value="team.id"
            />
          </el-select>
          <el-input v-model="teamName" placeholder="Team name" />
          <el-input v-model="teamSlug" placeholder="URL slug" />
          <el-input
            v-model="teamDescription"
            type="textarea"
            :rows="2"
            placeholder="Description"
          />
          <el-select v-model="strategy" class="full-width">
            <el-option label="Sequential pipeline" value="sequential" />
            <el-option label="Coordinator synthesis" value="coordinator" />
            <el-option label="Parallel fan-out" value="parallel" />
            <el-option label="Router dispatch" value="router" />
          </el-select>
          <div class="team-member-list">
            <div
              v-for="(member, index) in members"
              :key="member.agent_id"
              class="team-member-row"
            >
              <span
                >{{ index + 1 }}.
                {{
                  agentNameById.get(member.agent_id) || member.agent_id
                }}</span
              >
              <el-tag size="small" type="info">{{ member.role }}</el-tag>
              <el-button
                text
                type="danger"
                size="small"
                @click="removeMember(index)"
                >Remove</el-button
              >
            </div>
            <el-empty
              v-if="members.length === 0"
              description="No specialists yet"
              :image-size="56"
            />
          </div>
          <el-select
            v-model="newMemberAgentId"
            placeholder="Add a published agent"
            class="full-width"
          >
            <el-option
              v-for="agent in availableAgents"
              :key="agent.id"
              :label="`${agent.name} · v${agent.current_version}`"
              :value="agent.id"
            />
          </el-select>
          <el-input
            v-model="newMemberRole"
            placeholder="Role, e.g. research or writer"
          />
          <el-input
            v-model="newMemberInstructions"
            type="textarea"
            :rows="2"
            placeholder="Instructions for this member"
          />
          <el-button plain :disabled="!newMemberAgentId" @click="addMember"
            >Add specialist</el-button
          >
          <template v-if="strategy === 'coordinator'">
            <el-divider />
            <h4>Coordinator</h4>
            <el-select
              v-model="coordinatorAgentId"
              placeholder="Select coordinator agent"
              class="full-width"
            >
              <el-option
                v-for="agent in coordinatorCandidates"
                :key="agent.id"
                :label="`${agent.name} · v${agent.current_version}`"
                :value="agent.id"
              />
            </el-select>
            <el-input
              v-model="coordinatorInstructions"
              type="textarea"
              :rows="2"
              placeholder="Synthesis instructions"
            />
          </template>
          <template v-if="strategy === 'router'">
            <el-divider />
            <h4>Router</h4>
            <el-select v-model="routerAgentId" placeholder="Select router agent" class="full-width">
              <el-option
                v-for="agent in routerCandidates"
                :key="agent.id"
                :label="`${agent.name} · v${agent.current_version}`"
                :value="agent.id"
              />
            </el-select>
            <el-input
              v-model="routerInstructions"
              type="textarea"
              :rows="2"
              placeholder="Routing instructions"
            />
            <p class="task-note">
              The router must reply with exactly one member route label. The engine normalizes case
              and whitespace before choosing the branch.
            </p>
          </template>
          <p class="task-note">
            Completing a team creates real Agent, LLM and step records. Members
            with plugin tools are rejected until per-member tool approvals
            exist.
          </p>
          <div class="agent-publish-row">
            <el-button :loading="busy" @click="saveOnly">Save draft</el-button>
            <el-button type="primary" :loading="busy" @click="publishTeam"
              >Publish team</el-button
            >
          </div>
        </div>
        <div class="agent-builder-form">
          <h4>Run team</h4>
          <div class="agent-publish-row">
            <span>Published version</span>
            <el-tag :type="selectedTeam?.current_version ? 'success' : 'info'">
              {{
                selectedTeam?.current_version
                  ? `v${selectedTeam.current_version}`
                  : "Draft only"
              }}
            </el-tag>
          </div>
          <el-input
            v-model="runInput"
            type="textarea"
            :rows="4"
            placeholder="Give the team a task"
          />
          <el-button
            type="success"
            :disabled="
              !selectedTeam || !selectedTeam.current_version || !runInput.trim()
            "
            :loading="busy"
            @click="runTeam"
            >Run agent team</el-button
          >
          <p class="task-note">
            Every run publishes immutable member versions into a Workflow
            version. Token usage, leases, retries and audit events use the
            existing platform runtime.
          </p>
        </div>
      </div>
      <div v-if="runs.length" class="agent-run-list">
        <div class="agent-run-heading">
          <h4>Recent team runs</h4>
          <span class="task-note">Auto-refresh every 3 seconds</span>
        </div>
        <article v-for="run in runs" :key="run.id" class="agent-run-card">
          <div class="agent-run-meta">
            <el-tag
              :type="
                run.task_status === 'succeeded'
                  ? 'success'
                  : run.task_status === 'failed'
                    ? 'danger'
                    : run.task_status === 'running'
                      ? 'warning'
                      : 'info'
              "
              size="small"
              >{{ run.task_status }}</el-tag
            >
            <span>{{ run.id.slice(0, 8) }}</span>
            <span v-if="run.finished_at">finished</span>
          </div>
          <p class="agent-run-input">{{ run.input_text }}</p>
          <p v-if="run.output_text" class="agent-run-output">
            {{ run.output_text }}
          </p>
        </article>
      </div>
      <el-empty v-else description="No team runs yet" :image-size="72" />
    </template>
  </section>
</template>
