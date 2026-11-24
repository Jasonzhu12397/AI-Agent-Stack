# Chat workspace and scheduled turns

The Control Agent console is the primary operator surface: one shared left rail
for navigation and conversation history, and one full-width chat column. Task
details, file lists and usage open in a drawer or dialog on demand — there is no
third pane and no permanently reserved column.

## Layout contract

| Region | Contents |
| --- | --- |
| Left rail | New task, New conversation, Workbench, Scheduled tasks, Plugins · Skills · Partners, Knowledge, More (workflow canvas, Agent Studio, Model Manager, marketplace, registry, Metal3 fleet, governance, audit, organization, docs), project and tenant selectors, conversation list, account block |
| Chat column | Conversation title with environment and plan badges, transcript, Markdown and code rendering, Agent execution state, floating composer |
| Composer | `+` menu, cloud/local target, project, skills, plugins, reasoning effort, send/stop |
| On demand | Task details drawer, knowledge picker dialog, goal editor dialog |

Phones collapse the rail into a single swipeable strip of destinations; the
project selector stays in the composer and the conversation list is implied by
the thread title. The chat column keeps the whole viewport height because
`body.app-shell` fixes the shell and the console claims the remaining flex space
instead of measuring itself in JavaScript.

## Thread settings

Every `AgentChatThread` carries four persisted settings, editable through
`PATCH /api/v1/agent-chat/threads/<thread>/`:

| Field | Values | Effect |
| --- | --- | --- |
| `execution_target` | `cloud` (default), `local` | Stated in the system prompt and recorded in message usage; selects which runtime the turn is planned against |
| `effort` | `auto` (default), `low` | `low` clamps the request to `min(512, model.max_output_tokens)` and asks for a short answer |
| `goal` | free text, ≤ 500 chars | Injected into the system prompt as the acceptance condition of the work |
| `plan_first` | boolean | Injects a plan-before-act instruction and shows the "Plan first" badge |

The values are echoed back in `usage` on the assistant message, so the transcript
status line (`Control Agent · Cloud computer · Reasoning auto · tokens · latency`)
is derived from what actually ran, not from form state.

Invalid values are rejected with `INVALID_EXECUTION_TARGET`, `INVALID_EFFORT` or
`INVALID_GOAL`.

## Attachments

`AgentChatAttachment` stores `thread`, `uploader`, `filename`, `content_type`,
`size_bytes` and a `FileField`. Limits: 20 MB per file, 10 attachments per turn.

- `POST /api/v1/agent-chat/threads/<thread>/attachments/` (multipart) uploads.
- `GET /api/v1/agent-chat/attachments/<id>/` downloads; ownership is checked
  against the thread, not the uploader.
- `DELETE /api/v1/agent-chat/attachments/<id>/` removes the row and the stored
  file.
- `context.attachments` on a message must reference attachments of that same
  thread; only the filenames are persisted into the message context so the
  transcript stays readable after a file is deleted.

`MEDIA_ROOT` defaults to `BASE_DIR/.devdata/media` and can be pointed at shared
storage with the `MEDIA_ROOT` environment variable; `MEDIA_URL` serves the files.

## Knowledge references

`context.knowledge_documents` accepts document ids that belong to the active
tenant and project and are not archived. The titles are copied into the message
context, which is what the transcript renders.

## Scheduled turns

`ScheduledChatJob` is a prompt plus a cadence:

```
name, prompt, thread (optional), model_config,
cadence=hourly|daily|weekly, hour, weekday,
execution_target, effort, enabled, next_run_at, last_run_at, last_error_code
```

- `next_occurrence(now, job)` aligns the first run to the tenant timezone so a
  "daily at 09:00" job does not inherit the moment it was created.
- The worker calls `dispatch_due_jobs` every 30 seconds and claims at most three
  jobs per pass; `claim_due_jobs` advances `next_run_at` *before* running, so two
  workers cannot run the same job twice.
- `run_job` creates the thread when the job has none, writes the user turn,
  calls the shared chat runtime, writes the assistant turn (including
  `error_code` on failure) and advances the schedule. `last_error_code` is kept
  on the job so the console can show it without opening the conversation.
- `python manage.py dispatch_scheduled_jobs` runs the same dispatch manually.

API (tenant scoped, project scoped):

| Method | Path |
| --- | --- |
| `GET` / `POST` | `/api/v1/tenants/<tenant>/projects/<project>/scheduled-jobs/` |
| `PATCH` / `DELETE` | `…/scheduled-jobs/<id>/` |
| `POST` | `…/scheduled-jobs/<id>/run-now/` |

`PATCH` accepts `enabled`, which pauses or resumes and recomputes `next_run_at`.
`run-now` executes synchronously and returns `409` with the job payload when the
turn fails, so the console can show `last_error_code` instead of a spinner.

## Shared runtime

`core/chat_runtime.py` owns the system prompt, history assembly and model
generation for both the interactive endpoint and the scheduler. The system
prompt is the single place where goal, plan-first, execution target and low
effort are expressed; `ChatRuntimeError` carries the error code the API and the
worker both persist. Keeping one runtime means a scheduled turn behaves exactly
like a turn the operator typed.

## Tool calls

The selected skills and plugins are not just prompt text. `core/chat_tools.py`
builds an OpenAI-style tool list for every turn and `chat_runtime.run_turn`
runs the tool loop the model drives:

- Platform tools always ship: `stack_list_nodes`, `stack_plan_deployment`,
  `stack_get_deployment`,
  `stack_list_deployments`,
  `stack_list_commands`, `stack_list_registry_images`,
  `stack_list_scheduled_jobs` and `stack_create_command`. The read tools query
  the control plane; `stack_get_deployment` returns one deployment with its
  preflight and plan phases; `stack_plan_deployment` files the same preflight the
  deployment form files (spec validation, network readiness gate and stored
  `preflight_result`) for an online EPH node. It takes `cluster_name` and
  `kubernetes_version` (plus optional replica counts) rather than a nested spec,
  and omitting `eph_node_id` uses the newest online EPH node; `stack_create_command` writes a
  real `AgentCommand`, refusing a command the node's role never implements so a
  queued row cannot sit undeliverable;
  (version upgrades and rollbacks land in `waiting_approval`).
- A selected skill opens exactly one capability of the installation that
  granted it; a selected plugin opens every capability the installation was
  granted. Capability calls go through `agent_capability_router`, so the
  connector transport, credential resolution, risk policy and invocation
  audit rows are the same ones the worker uses.
- Model capabilities decide whether tools are offered at all. A model without
  `tools` still answers, and the turn records
  `usage.tools_note = "model does not support tool calls"` so the console can
  say why nothing was invoked.
- Budgets are fixed at four model steps and sixteen calls per turn; results
  are clipped to 8,000 characters and the turn ends with
  `usage.truncated = true` when the step budget is spent.

Every call is persisted as `AgentChatMessage.tool_calls`: name, identifier,
kind, arguments, `ok`, error `code` and `duration_ms`. The transcript renders
one chip per call, red when the call failed, so an operator can see which
capability ran and what the connector said. Scheduled turns store the same
shape from their own runtime pass.

The demo seed marks the offline `Local echo` model as tool-capable with
`provider.config.tool_call`, so a fresh console can show a real tool turn
without provisioning a provider credential. It calls one tool with arguments
filled from the user's sentence; a plugin whose endpoint is not reachable
reports a real error code (`CAPABILITY_INVOCATION_FAILED`) rather than a
fabricated success.

## Local execution

The composer's `Cloud computer` / `Local computer` switch is a real execution
choice, not a label on the prompt:

- A thread with `execution_target=local` adds `local_system_info` and
  `local_disk_usage` to the tool list and the prompt stops assuming a cloud
  cluster.
- Those tools queue a `local.*` **AgentCommand** for the project's enrolled
  `local_runner` node and block until that node reports the result, so the
  answer contains numbers produced on the operator's machine.
- With no runner online the call fails with `LOCAL_RUNNER_OFFLINE`; a lease
  that dies leaves `LOCAL_COMMAND_FAILED` / `LOCAL_COMMAND_TIMEOUT`, never a
  fabricated value.
- `apps/local-agent/local_agent.py` is the agent that serves it: it heartbeats,
  leases commands over `/api/v1/agent-runtime/*`, runs read-only probes with
  the standard library and posts the result. `manage.py enroll_local_runner`
  registers the machine and writes its bearer token; `.devcontainer/start.py`
  starts both in a development deployment.

## Connector catalog

`deploy/agent-plugins/catalog.json` declares the connectors the plugin menu
offers — 企业微信, 地图, 天气 and anything appended later — as data: plugin key,
protocol, capability list and the environment variable that carries its HTTPS
origin. `manage.py sync_plugin_catalog` upserts one definition per tenant and
is run by the deployment after seeding.

A connector whose variable is unset is still registered but disabled, and the
composer lists it with the variable it needs instead of a dead button. Set
`WECOM_CONNECTOR_URL` (plus a credential reference for the auth-required ones),
re-run the command and the row becomes installable; installation goes through
the ordinary `agent-plugin-installations` endpoint, so grants, credential
validation and the invocation audit are the same as any other plugin.

地图 and 天气 are declared `"builtin": true` with protocol `open_data`: their
capabilities run inside the platform (`core/open_data.py`) against keyless
providers — the Open-Meteo geocoder for place lookup, the OSRM demo router for
driving routes and wttr.in for current conditions and the daily forecast — so
they are enabled without an operator endpoint or API key. The catalog declares
each capability's real `input_schema`, `query_params` and `output_schema`, so
the chat tool carries typed arguments (`place`, `origin`, `destination`, `days`)
rather than one opaque prompt string. A place named inside a sentence ("帮我查
一下西安天气") is resolved by stripping the request words before geocoding, and
each upstream call retries a couple of times because these public services
answer 503 for bursty datacenter traffic.

## Session tokens for proxied consoles

An access proxy or in-app webview can drop or partition the session cookie,
which used to turn every request after sign-in into a 401 that the shell
reported as "登录已过期". `POST /api/v1/auth/login/` now also returns
`session_token` (the same server-side session key), the console stores it and
sends `X-Session-Token` on every call, and `core/api_auth.py` resolves that
header against the session store. CSRF is not re-checked for the header path:
the token travels in a custom header that a cross-site page cannot set.

## Local development database

The single-host console runs one daphne process and one worker against the same
SQLite file, and both write: the worker advances `next_run_at`, the API persists
turns. `core/sqlite_tuning.py` switches such connections to WAL and sets a 20 s
busy timeout, so a turn no longer fails with "database is locked" while the
worker claims a job. Production runs PostgreSQL, where this does not apply.

## Quality gates

- `apps/web-studio`: `npx vue-tsc --noEmit`, `node --experimental-strip-types
  --test tests/*.test.mjs` (i18n coverage included), `npx vite build`.
- `tests/test_chat_tools.py` covers the tool catalogue, skill/plugin
  scoping, platform reads, command idempotency and the step budget.
- `tests/test_chat_workspace.py` covers settings validation and storage, usage
  recording, attachment upload/download/ownership, knowledge references,
  schedule creation and dispatch, `run-now` thread creation, once-only claiming
  and pause behaviour.
