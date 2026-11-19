from __future__ import annotations

import uuid

from django.db import transaction

from core.models import (
    AgentDefinition,
    AgentTeam,
    AgentTeamVersion,
    WorkflowDefinition,
    WorkflowVersion,
)

MAX_TEAM_MEMBERS = 6
MAX_INSTRUCTIONS_CHARS = 4000
TEAM_STRATEGIES = {"sequential", "coordinator"}


class AgentTeamConfigurationError(ValueError):
    def __init__(self, code: str, detail: str, fields: list[str] | None = None):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.fields = fields or []


def _parse_uuid(value, field: str):
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise AgentTeamConfigurationError(
            "TEAM_AGENT_ID_INVALID",
            f"{field} must be a UUID.",
            [field],
        ) from exc


def _clean_instructions(value, field: str):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise AgentTeamConfigurationError(
            "TEAM_INSTRUCTIONS_INVALID",
            f"{field} must be a string.",
            [field],
        )
    if len(value) > MAX_INSTRUCTIONS_CHARS:
        raise AgentTeamConfigurationError(
            "TEAM_INSTRUCTIONS_TOO_LONG",
            f"{field} cannot exceed {MAX_INSTRUCTIONS_CHARS} characters.",
            [field],
        )
    return value.strip()


def _resolve_agent(team: AgentTeam, agent_id, field: str, project_agents):
    agent = project_agents.get(agent_id)
    if agent is None or agent.is_archived:
        raise AgentTeamConfigurationError(
            "TEAM_AGENT_UNAVAILABLE",
            f"{field} must reference an active agent in this project.",
            [field],
        )
    version = (
        agent.versions.filter(version=agent.current_version).first()
        if agent.current_version > 0
        else None
    )
    if version is None:
        raise AgentTeamConfigurationError(
            "TEAM_AGENT_NOT_PUBLISHED",
            f"{field} references an agent without a published version.",
            [field],
        )
    snapshot = version.config_snapshot or {}
    tool_allowlist = snapshot.get("tool_allowlist", [])
    if not isinstance(tool_allowlist, list) or tool_allowlist:
        raise AgentTeamConfigurationError(
            "TEAM_AGENT_TOOLS_UNSUPPORTED",
            (
                f"{field} grants plugin tools, which are not supported inside a "
                "team run yet. Publish a tool-free version for team use."
            ),
            [field],
        )
    return agent, version


def normalize_team_definition(team: AgentTeam, draft_config):
    if not isinstance(draft_config, dict):
        raise AgentTeamConfigurationError(
            "TEAM_CONFIGURATION_INVALID",
            "Team configuration must be a JSON object.",
        )
    strategy = draft_config.get("strategy", "sequential")
    if strategy not in TEAM_STRATEGIES:
        raise AgentTeamConfigurationError(
            "TEAM_STRATEGY_INVALID",
            "strategy must be sequential or coordinator.",
            ["strategy"],
        )
    members = draft_config.get("members")
    if not isinstance(members, list) or not 1 <= len(members) <= MAX_TEAM_MEMBERS:
        raise AgentTeamConfigurationError(
            "TEAM_MEMBERS_INVALID",
            f"members must contain 1 to {MAX_TEAM_MEMBERS} agents.",
            ["members"],
        )
    project_agents = {
        agent.id: agent
        for agent in AgentDefinition.objects.filter(
            tenant=team.tenant,
            project=team.project,
        ).prefetch_related("versions")
    }
    normalized_members = []
    seen_agent_ids = set()
    for index, member in enumerate(members):
        field = f"members[{index}].agent_id"
        if not isinstance(member, dict):
            raise AgentTeamConfigurationError(
                "TEAM_MEMBER_INVALID",
                f"members[{index}] must be an object.",
                [f"members[{index}]"],
            )
        agent_id = _parse_uuid(member.get("agent_id"), field)
        if agent_id in seen_agent_ids:
            raise AgentTeamConfigurationError(
                "TEAM_AGENT_DUPLICATE",
                "The same agent cannot appear twice in one team.",
                [field],
            )
        seen_agent_ids.add(agent_id)
        agent, version = _resolve_agent(team, agent_id, field, project_agents)
        role = member.get("role", "")
        if not isinstance(role, str) or len(role) > 80:
            raise AgentTeamConfigurationError(
                "TEAM_ROLE_INVALID",
                f"members[{index}].role must be at most 80 characters.",
                [f"members[{index}].role"],
            )
        instructions = _clean_instructions(
            member.get("instructions"),
            f"members[{index}].instructions",
        )
        if not instructions:
            raise AgentTeamConfigurationError(
                "TEAM_INSTRUCTIONS_MISSING",
                f"members[{index}].instructions must describe the member's task.",
                [f"members[{index}].instructions"],
            )
        normalized_members.append(
            {
                "agent_id": str(agent.id),
                "agent_version_id": str(version.id),
                "agent_name": agent.name,
                "agent_version": version.version,
                "role": role.strip(),
                "instructions": instructions,
                "order": index,
            }
        )
    coordinator = None
    coordinator_config = draft_config.get("coordinator")
    if strategy == "coordinator":
        if not isinstance(coordinator_config, dict):
            raise AgentTeamConfigurationError(
                "TEAM_COORDINATOR_MISSING",
                "coordinator strategy requires a coordinator object.",
                ["coordinator"],
            )
        field = "coordinator.agent_id"
        agent_id = _parse_uuid(coordinator_config.get("agent_id"), field)
        if agent_id in seen_agent_ids:
            raise AgentTeamConfigurationError(
                "TEAM_COORDINATOR_IS_MEMBER",
                "The coordinator must be a separate agent from the team members.",
                [field],
            )
        agent, version = _resolve_agent(team, agent_id, field, project_agents)
        instructions = _clean_instructions(
            coordinator_config.get("instructions"),
            "coordinator.instructions",
        )
        if not instructions:
            raise AgentTeamConfigurationError(
                "TEAM_INSTRUCTIONS_MISSING",
                "coordinator.instructions must describe the synthesis task.",
                ["coordinator.instructions"],
            )
        coordinator = {
            "agent_id": str(agent.id),
            "agent_version_id": str(version.id),
            "agent_name": agent.name,
            "agent_version": version.version,
            "role": "coordinator",
            "instructions": instructions,
            "order": len(normalized_members),
        }
    elif coordinator_config not in (None, {}, []):
        raise AgentTeamConfigurationError(
            "TEAM_COORDINATOR_NOT_ALLOWED",
            "sequential strategy cannot define a coordinator.",
            ["coordinator"],
        )
    return {
        "strategy": strategy,
        "members": normalized_members,
        "coordinator": coordinator,
    }


def build_team_graph(normalized):
    nodes = [{"id": "start", "type": "start", "config": {}}]
    edges = []
    previous = "start"
    for index, member in enumerate(normalized["members"], start=1):
        node_id = f"member_{index}"
        config = {
            "agent_version_id": member["agent_version_id"],
            "instructions": (
                f"Role: {member['role']}.\\n" if member["role"] else ""
            )
            + member["instructions"],
        }
        if previous != "start":
            config["input_from"] = previous
        nodes.append({"id": node_id, "type": "agent", "config": config})
        edges.append({"source": previous, "target": node_id})
        previous = node_id
    coordinator = normalized.get("coordinator")
    if coordinator is not None:
        nodes.append(
            {
                "id": "coordinator",
                "type": "agent",
                "config": {
                    "agent_version_id": coordinator["agent_version_id"],
                    "instructions": "Role: coordinator.\\n" + coordinator["instructions"],
                    "input_from": previous,
                },
            }
        )
        edges.append({"source": previous, "target": "coordinator"})
        previous = "coordinator"
    nodes.append({"id": "end", "type": "end", "config": {"source_node": previous}})
    edges.append({"source": previous, "target": "end"})
    return {"nodes": nodes, "edges": edges}


def team_workflow_slug(team: AgentTeam):
    return f"agent-team-{team.id.hex}"[:80]


def publish_team(team: AgentTeam, actor):
    with transaction.atomic():
        team = AgentTeam.objects.select_for_update().get(pk=team.pk)
        normalized = normalize_team_definition(team, team.draft_config)
        graph = build_team_graph(normalized)
        definition, _ = WorkflowDefinition.objects.get_or_create(
            tenant=team.tenant,
            project=team.project,
            slug=team_workflow_slug(team),
            defaults={
                "created_by": actor,
                "name": f"Agent team: {team.name}"[:120],
                "description": "Generated from an agent team definition.",
                "draft_definition": graph,
            },
        )
        next_workflow_version = (
            WorkflowVersion.objects.filter(workflow=definition)
            .order_by("-version")
            .values_list("version", flat=True)
            .first()
            or 0
        ) + 1
        workflow_version = WorkflowVersion.objects.create(
            workflow=definition,
            version=next_workflow_version,
            definition_snapshot=graph,
            created_by=actor,
        )
        next_team_version = (
            AgentTeamVersion.objects.filter(team=team)
            .order_by("-version")
            .values_list("version", flat=True)
            .first()
            or 0
        ) + 1
        team_version = AgentTeamVersion.objects.create(
            team=team,
            version=next_team_version,
            definition_snapshot={
                **normalized,
                "workflow_definition_id": str(definition.id),
                "workflow_version": next_workflow_version,
            },
            workflow_version=workflow_version,
            created_by=actor,
        )
        team.current_version = next_team_version
        team.save(update_fields=["current_version", "updated_at"])
        return team_version
