"""The Control Agent turn runtime shared by the console and the scheduler.

Both an interactive message and a scheduled turn run the same steps: build the
system prompt from the thread (project, goal, execution target, attached
skills, plugins and files), append the bounded history window, call the
tenant's model through the gateway and return the reply with the usage the
provider reported. Keeping this in one place means a scheduled run cannot drift
away from what the console does.
"""

from __future__ import annotations

import json
import time

from core import chat_tools
from core.model_gateway import ModelGateway, ModelGatewayError
from core.models import AgentPluginInstallation, ModelProvider
from core.provider_accounts import AccountTokenGuard, ProviderAccountError
from core.token_budget import TenantTokenBudgetExceeded
from core.token_guard import AdHocTokenGuard

#: Effort is a real budget knob, not a label: "low" halves nothing but caps the
#: answer so a quick question does not spend a long-generation budget.
LOW_EFFORT_MAX_TOKENS = 512

EXECUTION_TARGETS = ("cloud", "local")
EFFORTS = ("auto", "low")


class ChatRuntimeError(Exception):
    """A turn that could not be completed, with an API-shaped explanation."""

    def __init__(self, code: str, message: str, http_status: int = 502):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def system_prompt(thread, context) -> str:
    lines = [
        "You are the Control Agent of AI-Agent-Stack, a multi-agent platform.",
        "You own the delivery chain (Cloud Control, Target and EPH agents), the agent marketplace,",
        "the skill catalog and the plugin installs. Answer as that operator.",
        f"Tenant: {thread.tenant.name}",
        f"Project: {thread.project.name}",
    ]
    goal = (thread.goal or "").strip()
    if goal:
        lines.append("Goal of this task: " + goal)
    if thread.plan_first:
        lines.append(
            "Answer with an execution plan first: numbered steps, the owner of each "
            "step, then the risks."
        )
    if thread.execution_target == "local":
        lines.append(
            "Execution environment: the local machine workspace. "
            "Do not assume a cloud cluster is reachable."
        )
    else:
        lines.append(
            "Execution environment: the cloud control plane. Work is dispatched to Cloud Control, "
            "EPH and Target agents."
        )
    if thread.effort == "low":
        lines.append(
            "Reasoning level: low. Be brief, prefer the shortest answer that is still correct."
        )
    if context.get("skills"):
        lines.append("Skills in scope for this conversation: " + ", ".join(context["skills"]))
    plugin_names = [
        installation.plugin.plugin_key
        for installation in AgentPluginInstallation.objects.filter(
            tenant=thread.tenant, project=thread.project
        ).select_related("plugin")
        if str(installation.id) in context.get("plugins", [])
    ]
    if plugin_names:
        lines.append("Plugins in scope: " + ", ".join(plugin_names))
    files = context.get("attachments") or []
    if files:
        lines.append("Files attached by the member: " + ", ".join(files))
    documents = context.get("knowledge_documents") or []
    if documents:
        lines.append("Knowledge documents in scope: " + ", ".join(documents))
    lines.append(
        "Functions are available in this turn. Call one instead of saying you would check "
        "something: the transcript records the calls and their results."
    )
    lines.append("Be concrete: name the resources, the commands and the next step.")
    return "\n".join(lines)


def history(thread, window: int = 20):
    turns = list(thread.messages.order_by("-created_at")[:window])
    turns.reverse()
    result = []
    for message in turns:
        if message.role == "user":
            result.append({"role": "user", "content": message.content})
        elif message.role == "assistant" and not message.error_code:
            result.append({"role": "assistant", "content": message.content})
    return result


def _complete(
    tenant,
    model_config,
    messages,
    *,
    user,
    idempotency_key,
    max_tokens=None,
    tools=None,
    tool_choice=None,
):
    """One provider call through the tenant's model, with the guard that applies."""
    try:
        if model_config.provider.auth_mode == ModelProvider.AuthMode.USER_ACCOUNT:
            from core import provider_accounts

            link = provider_accounts.active_link(user, model_config.provider)
            if link is None:
                raise ChatRuntimeError(
                    "PROVIDER_ACCOUNT_NOT_LINKED",
                    "Link your own account on this platform before chatting through it.",
                    403,
                )
            result = ModelGateway(account=AccountTokenGuard(link=link)).complete(
                model_config,
                messages,
                max_tokens=max_tokens,
                idempotency_key=idempotency_key,
                tools=tools,
                tool_choice=tool_choice,
            )
        else:
            guard = AdHocTokenGuard(tenant=tenant, scope_ref=f"agent-chat:{idempotency_key}")
            result = ModelGateway(guard=guard).complete(
                model_config,
                messages,
                max_tokens=max_tokens,
                idempotency_key=idempotency_key,
                tools=tools,
                tool_choice=tool_choice,
            )
    except ProviderAccountError as exc:
        raise ChatRuntimeError(exc.code, str(exc), exc.http_status) from exc
    except TenantTokenBudgetExceeded as exc:
        raise ChatRuntimeError(
            "TENANT_TOKEN_BUDGET_EXCEEDED",
            f"Tenant {exc.period} token budget would be exceeded.",
            402,
        ) from exc
    except ModelGatewayError as exc:
        http_status = (
            exc.http_status
            or (403 if exc.code == "MODEL_CREDENTIAL_REJECTED" else 502)
        )
        raise ChatRuntimeError(exc.code, str(exc), http_status) from exc
    return result


def generate(
    tenant,
    model_config,
    messages,
    *,
    user,
    idempotency_key,
    effort="auto",
    execution_target="cloud",
):
    """Run one completion turn and return ``(content, usage)``.

    Raises ``ChatRuntimeError`` for every failure the caller should surface;
    nothing here decides HTTP shapes, so the scheduler can log the same error
    a console member would have seen.
    """
    max_tokens = _max_tokens(model_config, effort)
    result = _complete(
        tenant,
        model_config,
        messages,
        user=user,
        idempotency_key=idempotency_key,
        max_tokens=max_tokens,
    )
    usage = result.as_dict().get("usage") or {}
    usage["effort"] = effort
    usage["execution_target"] = execution_target
    return result.content, usage


def _max_tokens(model_config, effort):
    if effort == "low":
        return min(LOW_EFFORT_MAX_TOKENS, model_config.max_output_tokens)
    return None


def _merge_usage(total: dict, reported: dict) -> None:
    for key in ("input_tokens", "output_tokens"):
        total[key] = int(total.get(key, 0)) + int(reported.get(key, 0) or 0)
    total["latency_ms"] = int(total.get("latency_ms", 0)) + int(
        reported.get("latency_ms", 0) or 0
    )


def run_turn(
    thread,
    model_config,
    *,
    user,
    messages,
    context,
    effort="auto",
    execution_target="cloud",
    idempotency_key,
):
    """Run one conversational turn, executing the tools the model selects.

    The catalog is the member's scope: the platform control-plane actions plus
    the capabilities of the skills and plugins picked in the composer. The loop
    is bounded (``chat_tools.MAX_TOOL_STEPS``) and every call is recorded so the
    transcript can show what actually ran, not just what the model said.
    """
    tools = chat_tools.build_tools(thread, context)
    supports_tools = "tools" in (model_config.capabilities or [])
    offered = chat_tools.to_openai_tools(tools) if tools and supports_tools else []
    by_name = {tool.name: tool for tool in tools}
    total_usage: dict = {}
    tool_log: list[dict] = []
    steps = 0
    while True:
        result = _complete(
            thread.tenant,
            model_config,
            messages,
            user=user,
            idempotency_key=f"{idempotency_key}:{steps}",
            max_tokens=_max_tokens(model_config, effort),
            tools=offered or None,
            tool_choice="auto" if offered else None,
        )
        _merge_usage(total_usage, result.as_dict().get("usage") or {})
        if not result.tool_calls:
            total_usage["effort"] = effort
            total_usage["execution_target"] = execution_target
            total_usage["tools_offered"] = len(offered)
            total_usage["tool_calls"] = len(tool_log)
            if tools and not supports_tools:
                total_usage["tools_note"] = "model does not support tool calls"
            return result.content, total_usage, tool_log
        if not offered:
            raise ChatRuntimeError(
                "MODEL_TOOL_CALLS_WITHOUT_TOOLS",
                "The model returned tool calls but this turn offered none.",
                502,
            )
        try:
            calls = chat_runtime_parse(result.tool_calls, offered)
        except ValueError as exc:
            raise ChatRuntimeError("MODEL_TOOL_CALL_INVALID", str(exc), 502) from exc
        if len(tool_log) + len(calls) > chat_tools.MAX_TOOL_CALLS:
            raise ChatRuntimeError(
                "MODEL_TOOL_CALL_BUDGET",
                "This turn exceeded the tool-call budget.",
                502,
            )
        messages.append(
            {
                "role": "assistant",
                "content": result.content or None,
                "tool_calls": result.tool_calls,
            }
        )
        for call in calls:
            tool = by_name[call["name"]]
            started = time.monotonic()
            payload = chat_tools.run_tool(tool, call["arguments"])
            tool_log.append(
                {
                    "name": tool.name,
                    "identifier": tool.identifier,
                    "kind": tool.kind,
                    "arguments": call["arguments"],
                    "ok": bool(payload.get("ok")),
                    "code": payload.get("code"),
                    "duration_ms": int((time.monotonic() - started) * 1000),
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps(payload, ensure_ascii=False, default=str),
                }
            )
        steps += 1
        if steps >= chat_tools.MAX_TOOL_STEPS:
            total_usage["effort"] = effort
            total_usage["execution_target"] = execution_target
            total_usage["tools_offered"] = len(offered)
            total_usage["tool_calls"] = len(tool_log)
            total_usage["truncated"] = True
            return (
                "Stopped after the tool-call budget; the transcript lists what ran.",
                total_usage,
                tool_log,
            )


def chat_runtime_parse(raw_calls, offered):
    from core.llm_tools import parse_openai_tool_calls

    return parse_openai_tool_calls({"tool_calls": raw_calls}, offered)
