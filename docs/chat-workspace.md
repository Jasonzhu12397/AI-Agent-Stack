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

## Local development database

The single-host console runs one daphne process and one worker against the same
SQLite file, and both write: the worker advances `next_run_at`, the API persists
turns. `core/sqlite_tuning.py` switches such connections to WAL and sets a 20 s
busy timeout, so a turn no longer fails with "database is locked" while the
worker claims a job. Production runs PostgreSQL, where this does not apply.

## Quality gates

- `apps/web-studio`: `npx vue-tsc --noEmit`, `node --experimental-strip-types
  --test tests/*.test.mjs` (i18n coverage included), `npx vite build`.
- `tests/test_chat_workspace.py` covers settings validation and storage, usage
  recording, attachment upload/download/ownership, knowledge references,
  schedule creation and dispatch, `run-now` thread creation, once-only claiming
  and pause behaviour.
