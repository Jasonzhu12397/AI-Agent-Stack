from __future__ import annotations

import re
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
TEAM_STRATEGIES = {"sequential", "coordinator", "parallel", "router"}
ROUTE_LABEL_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,40}")


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


def _resolve_actor(team, project_agents, payload, field, excluded_agent_ids):
    if not isinstance(payload, dict):
        raise AgentTeamConfigurationError(
            "TEAM_ACTOR_MISSING",
            f"{field} must be an object.",
            [field],
        )
    agent_field = f"{field}.agent_id"
    agent_id = _parse_uuid(payload.get("agent_id"), agent_field)
    if agent_id in excluded_agent_ids:
        raise AgentTeamConfigurationError(
            "TEAM_AGENT_DUPLICATE",
            f"{field} must reference an agent that is not already used.",
            [agent_field],
        )
    agent, version = _resolve_agent(team, agent_id, agent_field, project_agents)
    instructions = _clean_instructions(payload.get("instructions"), f"{field}.instructions")
    if not instructions:
        raise AgentTeamConfigurationError(
            "TEAM_INSTRUCTIONS_MISSING",
            f"{field}.instructions must describe its task.",
            [f"{field}.instructions"],
        )
    return {
        "agent_id": str(agent.id),
        "agent_version_id": str(version.id),
        "agent_name": agent.name,
        "agent_version": version.version,
        "instructions": instructions,
    }


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
            "strategy must be sequential, coordinator, parallel or router.",
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
    seen_labels = set()
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
        route_label = ""
        if strategy == "router":
            label_field = f"members[{index}].route_label"
            raw_label = member.get("route_label")
            if not isinstance(raw_label, str) or not ROUTE_LABEL_PATTERN.fullmatch(
                raw_label.strip()
            ):
                raise AgentTeamConfigurationError(
                    "TEAM_ROUTE_LABEL_INVALID",
                    (
                        f"{label_field} must be 1 to 40 characters using letters, "
                        "numbers, underscore or hyphen."
                    ),
                    [label_field],
                )
            route_label = raw_label.strip().lower()
            if route_label in seen_labels:
                raise AgentTeamConfigurationError(
                    "TEAM_ROUTE_LABEL_DUPLICATE",
                    "Every router member needs a unique route_label.",
                    [label_field],
                )
            seen_labels.add(route_label)
        normalized_members.append(
            {
                "agent_id": str(agent.id),
                "agent_version_id": str(version.id),
                "agent_name": agent.name,
                "agent_version": version.version,
                "role": role.strip(),
                "instructions": instructions,
                "route_label": route_label,
                "order": index,
            }
        )
    coordinator = None
    coordinator_config = draft_config.get("coordinator")
    if strategy in {"coordinator", "parallel"} and coordinator_config is None:
        raise AgentTeamConfigurationError(
            "TEAM_COORDINATOR_MISSING",
            f"{strategy} strategy requires a coordinator object.",
            ["coordinator"],
        )
    if strategy == "sequential" and coordinator_config not in (None, {}, []):
        raise AgentTeamConfigurationError(
            "TEAM_COORDINATOR_NOT_ALLOWED",
            "sequential strategy cannot define a coordinator.",
            ["coordinator"],
        )
    if coordinator_config not in (None, {}, []):
        coordinator = _resolve_actor(
            team,
            project_agents,
            coordinator_config,
            "coordinator",
            seen_agent_ids,
        )
        coordinator["role"] = "coordinator"
        coordinator["order"] = len(normalized_members)
        seen_agent_ids.add(uuid.UUID(coordinator["agent_id"]))
    router = None
    if strategy == "router":
        router_config = draft_config.get("router")
        if router_config is None:
            raise AgentTeamConfigurationError(
                "TEAM_ROUTER_MISSING",
                "router strategy requires a router object.",
                ["router"],
            )
        router = _resolve_actor(
            team,
            project_agents,
            router_config,
            "router",
            seen_agent_ids,
        )
        router["role"] = "router"
        router["order"] = -1
        seen_agent_ids.add(uuid.UUID(router["agent_id"]))
    elif draft_config.get("router") not in (None, {}, []):
        raise AgentTeamConfigurationError(
            "TEAM_ROUTER_NOT_ALLOWED",
            "router is only valid with the router strategy.",
            ["router"],
        )
    return {
        "strategy": strategy,
        "members": normalized_members,
        "coordinator": coordinator,
        "router": router,
    }


def _member_node(index, member, input_from=None):
    config = {
        "agent_version_id": member["agent_version_id"],
        "instructions": (f"Role: {member['role']}.\\n" if member["role"] else "")
        + member["instructions"],
    }
    if input_from is not None:
        config["input_from"] = input_from
    return {"id": f"member_{index}", "type": "agent", "config": config}


def build_team_graph(normalized):
    strategy = normalized["strategy"]
    nodes = [{"id": "start", "type": "start", "config": {}}]
    edges = []
    members = normalized["members"]
    coordinator = normalized.get("coordinator")

    if strategy in {"sequential", "coordinator"}:
        previous = "start"
        for index, member in enumerate(members, start=1):
            node = _member_node(index, member, None if previous == "start" else previous)
            nodes.append(node)
            edges.append({"source": previous, "target": node["id"]})
            previous = node["id"]
    elif strategy == "parallel":
        for index, member in enumerate(members, start=1):
            node = _member_node(index, member)
            nodes.append(node)
            edges.append({"source": "start", "target": node["id"]})
        previous = "start"
    else:
        router = normalized["router"]
        labels = [member["route_label"] for member in members]
        router_instructions = (
            "Role: router.\\n"
            + router["instructions"]
            + "\\n\\nReply with exactly one route label and no other text. "
            + "Allowed labels: "
            + ", ".join(labels)
            + "."
        )
        nodes.append(
            {
                "id": "router",
                "type": "agent",
                "config": {
                    "agent_version_id": router["agent_version_id"],
                    "instructions": router_instructions,
                },
            }
        )
        edges.append({"source": "start", "target": "router"})
        previous = "router"
        if len(members) == 1:
            previous = "router"
        else:
            for index in range(1, len(members)):
                condition_id = f"route_{index}"
                nodes.append(
                    {
                        "id": condition_id,
                        "type": "condition",
                        "config": {
                            "input_from": "router",
                            "equals": members[index - 1]["route_label"],
                            "normalize": True,
                        },
                    }
                )
                edges.append({"source": previous, "target": condition_id})
                previous = condition_id
        for index, member in enumerate(members, start=1):
            node = _member_node(index, member)
            nodes.append(node)
            if len(members) == 1:
                edges.append({"source": "router", "target": node["id"]})
            elif index == len(members):
                edges.append(
                    {"source": f"route_{index - 1}", "target": node["id"], "when": False}
                )
            else:
                edges.append(
                    {"source": f"route_{index}", "target": node["id"], "when": True}
                )

    if coordinator is not None:
        gather_from = [f"member_{index}" for index in range(1, len(members) + 1)]
        coordinator_config = {
            "agent_version_id": coordinator["agent_version_id"],
            "instructions": "Role: coordinator.\\n" + coordinator["instructions"],
            "gather_from": gather_from,
        }
        nodes.append({"id": "coordinator", "type": "agent", "config": coordinator_config})
        for index in range(1, len(members) + 1):
            edges.append({"source": f"member_{index}", "target": "coordinator"})
        previous = "coordinator"
        end_config = {"source_node": "coordinator"}
    else:
        end_config = {
            "gather_from": [f"member_{index}" for index in range(1, len(members) + 1)]
        }
    nodes.append({"id": "end", "type": "end", "config": end_config})
    if coordinator is not None:
        edges.append({"source": "coordinator", "target": "end"})
    else:
        for index in range(1, len(members) + 1):
            edges.append({"source": f"member_{index}", "target": "end"})
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
