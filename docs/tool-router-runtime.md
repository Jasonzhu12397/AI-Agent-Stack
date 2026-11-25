# Tool router runtime (retired)

The legacy `PluginTool` registry and its per-call approval runtime were removed
with migration `0066_delete_legacy_connector_stack`. Published agents no longer
carry `tool_allowlist`; they declare `capability_allowlist`, and the worker
resolves every call through `core.agent_capability_router.invoke_capability`
with project-scoped installs and closed schemas. High-risk capabilities are
refused at publish time because there is no per-call approval checkpoint, and
a capability that later raises `ToolApprovalRequired` fails closed.
