"""The Control Agent turn runtime shared by the console and the scheduler.

Both an interactive message and a scheduled turn run the same steps: build the
system prompt from the thread (project, goal, execution target, attached
skills, plugins and files), append the bounded history window, call the
tenant's model through the gateway and return the reply with the usage the
provider reported. Keeping this in one place means a scheduled run cannot drift
away from what the console does.
"""

from __future__ import annotations

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
    max_tokens = None
    if effort == "low":
        max_tokens = min(LOW_EFFORT_MAX_TOKENS, model_config.max_output_tokens)
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
            )
        else:
            guard = AdHocTokenGuard(tenant=tenant, scope_ref=f"agent-chat:{idempotency_key}")
            result = ModelGateway(guard=guard).complete(
                model_config,
                messages,
                max_tokens=max_tokens,
                idempotency_key=idempotency_key,
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
        http_status = 403 if exc.code == "MODEL_CREDENTIAL_REJECTED" else 502
        raise ChatRuntimeError(exc.code, str(exc), http_status) from exc
    usage = result.as_dict().get("usage") or {}
    usage["effort"] = effort
    usage["execution_target"] = execution_target
    return result.content, usage
