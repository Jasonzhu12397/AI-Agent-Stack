import re
from collections import deque

from django.db import transaction
from django.utils import timezone

from core.model_gateway import ModelGateway, ModelGatewayError
from core.models import (
    ModelConfig,
    WorkflowRun,
    WorkflowStepRun,
)


class WorkflowValidationError(ValueError):
    pass


def validate_workflow_definition(definition):
    if not isinstance(definition, dict):
        raise WorkflowValidationError("Workflow definition must be a JSON object.")
    nodes = definition.get("nodes")
    edges = definition.get("edges")
    if not isinstance(nodes, list) or not 2 <= len(nodes) <= 50:
        raise WorkflowValidationError("Workflow must contain between 2 and 50 nodes.")
    if not isinstance(edges, list) or len(edges) > 200:
        raise WorkflowValidationError("Workflow edges must be an array of at most 200 items.")
    node_map = {}
    for node in nodes:
        if not isinstance(node, dict):
            raise WorkflowValidationError("Every workflow node must be an object.")
        node_id = node.get("id")
        node_type = node.get("type")
        config = node.get("config", {})
        if not isinstance(node_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", node_id):
            raise WorkflowValidationError("Every node requires a valid stable id.")
        if node_id in node_map:
            raise WorkflowValidationError(f"Duplicate workflow node id: {node_id}")
        if node_type not in {"start", "llm", "condition", "end"}:
            raise WorkflowValidationError(f"Unsupported workflow node type: {node_type}")
        if not isinstance(config, dict):
            raise WorkflowValidationError(f"Node {node_id} config must be an object.")
        if node_type == "llm":
            if not isinstance(config.get("model_config_id"), str):
                raise WorkflowValidationError(f"LLM node {node_id} requires model_config_id.")
            if not isinstance(config.get("prompt_template"), str):
                raise WorkflowValidationError(f"LLM node {node_id} requires prompt_template.")
            if len(config["prompt_template"]) > 12000:
                raise WorkflowValidationError(f"LLM node {node_id} prompt is too long.")
        if node_type == "condition" and "equals" not in config:
            raise WorkflowValidationError(f"Condition node {node_id} requires equals.")
        node_map[node_id] = node
    starts = [node_id for node_id, node in node_map.items() if node["type"] == "start"]
    ends = [node_id for node_id, node in node_map.items() if node["type"] == "end"]
    if len(starts) != 1 or not ends:
        raise WorkflowValidationError("Workflow requires exactly one start and at least one end.")
    incoming = {node_id: [] for node_id in node_map}
    outgoing = {node_id: [] for node_id in node_map}
    seen_edges = set()
    for edge in edges:
        if not isinstance(edge, dict):
            raise WorkflowValidationError("Every workflow edge must be an object.")
        source = edge.get("source")
        target = edge.get("target")
        if source not in node_map or target not in node_map:
            raise WorkflowValidationError("Workflow edge references an unknown node.")
        if source == target or (source, target) in seen_edges:
            raise WorkflowValidationError("Self edges and duplicate edges are not allowed.")
        seen_edges.add((source, target))
        if node_map[source]["type"] == "condition":
            if not isinstance(edge.get("when"), bool):
                raise WorkflowValidationError(
                    "Edges from condition nodes require a boolean when value."
                )
        elif "when" in edge:
            raise WorkflowValidationError("Only condition nodes can have conditional edges.")
        incoming[target].append(edge)
        outgoing[source].append(edge)
    start_id = starts[0]
    if incoming[start_id]:
        raise WorkflowValidationError("Start node cannot have incoming edges.")
    for end_id in ends:
        if outgoing[end_id]:
            raise WorkflowValidationError("End nodes cannot have outgoing edges.")
    for node_id, node in node_map.items():
        if node["type"] == "condition":
            branch_values = {
                edge.get("when")
                for edge in outgoing[node_id]
            }
            if branch_values != {True, False}:
                raise WorkflowValidationError(
                    f"Condition node {node_id} requires true and false outgoing branches."
                )
        if node_id != start_id and not incoming[node_id]:
            raise WorkflowValidationError(f"Node {node_id} is disconnected from the start.")
    indegree = {node_id: len(incoming[node_id]) for node_id in node_map}
    queue = deque([start_id])
    ordered_ids = []
    while queue:
        node_id = queue.popleft()
        ordered_ids.append(node_id)
        for edge in outgoing[node_id]:
            target = edge["target"]
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    if len(ordered_ids) != len(node_map):
        raise WorkflowValidationError("Workflow graph contains a cycle.")
    reachable = set()
    pending = [start_id]
    while pending:
        node_id = pending.pop()
        if node_id in reachable:
            continue
        reachable.add(node_id)
        pending.extend(edge["target"] for edge in outgoing[node_id])
    if reachable != set(node_map):
        raise WorkflowValidationError("Every node must be reachable from the start node.")
    return {
        "nodes": node_map,
        "edges": edges,
        "incoming": incoming,
        "outgoing": outgoing,
        "order": ordered_ids,
        "start_id": start_id,
        "end_ids": ends,
    }


def validate_model_references(graph, tenant):
    model_ids = {
        node["config"]["model_config_id"]
        for node in graph["nodes"].values()
        if node["type"] == "llm"
    }
    for model_id in model_ids:
        model = ModelConfig.objects.select_related("provider").filter(
            pk=model_id,
            enabled=True,
            provider__enabled=True,
        ).first()
        if model is None:
            raise WorkflowValidationError("A workflow LLM node references an unavailable model.")
        if model.provider.tenant_id not in (None, tenant.id):
            raise WorkflowValidationError("Workflow model provider is outside the tenant.")
        if "chat" not in model.capabilities:
            raise WorkflowValidationError("Workflow LLM node model must support chat.")


def _step_input(node, state):
    config = node["config"]
    source_node = config.get("input_from")
    if source_node:
        return state.get(source_node)
    input_key = config.get("input_key", "input")
    if input_key == "input":
        return state.get("input")
    input_data = state.get("input", {})
    if isinstance(input_data, dict):
        return input_data.get(input_key)
    return None


def _execute_node(node, state, input_data):
    node_type = node["type"]
    config = node["config"]
    if node_type == "start":
        return input_data
    if node_type == "condition":
        source_node = config.get("source_node")
        if source_node:
            actual = state.get(source_node)
        else:
            input_key = config.get("input_key", "input")
            actual = input_data.get(input_key) if isinstance(input_data, dict) else None
        return {"matched": actual == config["equals"]}
    if node_type == "end":
        source_node = config.get("source_node")
        return state.get(source_node) if source_node else dict(state)
    if node_type == "llm":
        source_value = _step_input(node, state)
        if isinstance(source_value, (dict, list)):
            source_text = str(source_value)
        else:
            source_text = "" if source_value is None else str(source_value)
        prompt = config["prompt_template"].replace("{{input}}", source_text)
        system_prompt = config.get("system_prompt", "You are a helpful assistant.")
        model = ModelConfig.objects.select_related("provider").get(
            pk=config["model_config_id"],
            enabled=True,
            provider__enabled=True,
        )
        try:
            response = ModelGateway().complete(
                model,
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=config.get("max_tokens", model.max_output_tokens),
                timeout_seconds=config.get("timeout_seconds", 60),
            )
        except ModelGatewayError as exc:
            if exc.retryable:
                raise RuntimeError(exc.code) from exc
            raise WorkflowValidationError(exc.code) from exc
        return {
            "content": response.content,
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "provider": response.provider,
            "model": response.model,
            "latency_ms": response.latency_ms,
        }
    raise WorkflowValidationError(f"Unsupported workflow node type: {node_type}")


def execute_workflow_run(task, worker_id, renew_lease):
    workflow_run = WorkflowRun.objects.select_related(
        "workflow_version",
        "workflow_version__workflow",
    ).get(task=task)
    workflow = workflow_run.workflow_version.workflow
    if workflow.tenant_id != task.tenant_id or workflow.project_id != task.project_id:
        raise WorkflowValidationError("Workflow run scope does not match its task.")
    graph = validate_workflow_definition(workflow_run.workflow_version.definition_snapshot)
    validate_model_references(graph, workflow.tenant)
    state = dict(workflow_run.state or {})
    state.setdefault("input", workflow_run.input_data)
    active_nodes = set()
    step_records = {
        step.node_id: step
        for step in WorkflowStepRun.objects.filter(workflow_run=workflow_run)
    }
    for node_id in graph["order"]:
        node = graph["nodes"][node_id]
        existing = step_records.get(node_id)
        if existing and existing.status == WorkflowStepRun.Status.SUCCEEDED:
            state[node_id] = existing.output_data
            active_nodes.add(node_id)
            continue
        if existing and existing.status == WorkflowStepRun.Status.SKIPPED:
            continue
        if node_id == graph["start_id"]:
            active_incoming = []
        else:
            active_incoming = []
            for edge in graph["incoming"][node_id]:
                source_id = edge["source"]
                if source_id not in active_nodes:
                    continue
                source_node = graph["nodes"][source_id]
                if source_node["type"] == "condition":
                    matched = bool(state[source_id].get("matched"))
                    if edge["when"] != matched:
                        continue
                active_incoming.append(edge)
            if not active_incoming:
                WorkflowStepRun.objects.update_or_create(
                    workflow_run=workflow_run,
                    node_id=node_id,
                    defaults={
                        "node_type": node["type"],
                        "status": WorkflowStepRun.Status.SKIPPED,
                        "finished_at": timezone.now(),
                    },
                )
                continue
        if not renew_lease(task.id, task.lease_token, worker_id):
            raise RuntimeError("Workflow task cancellation requested or lease lost")
        now = timezone.now()
        step, _ = WorkflowStepRun.objects.get_or_create(
            workflow_run=workflow_run,
            node_id=node_id,
            defaults={
                "node_type": node["type"],
                "status": WorkflowStepRun.Status.RUNNING,
                "started_at": now,
            },
        )
        step.status = WorkflowStepRun.Status.RUNNING
        step.node_type = node["type"]
        step.attempt += 1 if step.started_at else 0
        step.started_at = now
        step.finished_at = None
        step.error = None
        step.input_data = {"active_predecessors": [edge["source"] for edge in active_incoming]}
        step.save()
        try:
            output = _execute_node(node, state, workflow_run.input_data)
        except Exception as exc:
            step.status = WorkflowStepRun.Status.FAILED
            step.error = {
                "code": "WORKFLOW_NODE_FAILED",
                "message": type(exc).__name__,
            }
            step.finished_at = timezone.now()
            step.save(update_fields=["status", "error", "finished_at"])
            raise
        step.status = WorkflowStepRun.Status.SUCCEEDED
        step.output_data = output
        step.finished_at = timezone.now()
        step.save(update_fields=["status", "output_data", "finished_at"])
        state[node_id] = output
        active_nodes.add(node_id)
        workflow_run.state = state
        workflow_run.save(update_fields=["state"])
    end_results = [state[node_id] for node_id in graph["end_ids"] if node_id in state]
    if not end_results:
        raise WorkflowValidationError("Workflow execution reached no end node.")
    return {"output": end_results[0], "state": state}
