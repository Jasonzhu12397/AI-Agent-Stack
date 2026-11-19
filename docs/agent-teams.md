# Agent Teams

Agent Teams turn a project's published Agents into one reusable multi-Agent
runtime. A team is a versioned roster, not a prompt convention: publishing
freezes every member's Agent version and materializes an immutable Workflow
version containing `agent` nodes.

## Model

- `AgentTeam` belongs to one tenant and one project. It stores a draft roster and
  the current published version.
- `AgentTeamVersion` freezes the normalized roster and links to the Workflow
  version generated from it.
- `AgentTeamRun` is the user-facing run record. Each run has one `WorkflowRun`
  and one task, so it inherits leases, retries, cancellation, token budgets, audit
  events and idempotency from the existing execution stack.

## Strategies

`sequential`
: Members run in order. Each member receives the previous member's answer as its
  input. The final member's answer becomes the team output.

`coordinator`
: Members run in order, then a separate coordinator Agent receives the last
member's answer and its own synthesis instructions. The coordinator output is the
team output.

A coordinator cannot also be a member, and the same Agent cannot appear twice in
one roster. Teams are capped at six members.

## Draft configuration

```json
{
  "strategy": "coordinator",
  "members": [
    {
      "agent_id": "00000000-0000-0000-0000-000000000001",
      "role": "research",
      "instructions": "Collect constraints and cite source IDs."
    },
    {
      "agent_id": "00000000-0000-0000-0000-000000000002",
      "role": "writer",
      "instructions": "Turn the research into a concise answer."
    }
  ],
  "coordinator": {
    "agent_id": "00000000-0000-0000-0000-000000000003",
    "instructions": "Merge teammate outputs and remove contradictions."
  }
}
```

Every referenced Agent must have a published current version in the same
project. Credential-shaped keys are rejected in draft configuration; use
credential references instead.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` / `POST` | `/api/v1/tenants/{tenant}/projects/{project}/agent-teams/` | List or create teams |
| `GET` / `PATCH` | `/api/v1/agent-teams/{team}/` | Read or edit a draft |
| `POST` | `/api/v1/agent-teams/{team}/publish/` | Freeze the roster and create a team version |
| `GET` / `POST` | `/api/v1/agent-teams/{team}/runs/` | List runs or start one |
| `GET` | `/api/v1/agent-team-runs/{run}/` | Read one run |

Run creation requires `Idempotency-Key`. Reusing the same key with the same team
and input returns the original run; reusing it with a different payload returns
`409`.

## Execution and limits

Each `agent` Workflow node loads the frozen Agent version, applies its system
prompt plus the member's team instructions, optionally retrieves its allowed
knowledge bases, calls the Agent's published model and records token usage
against the team run's `WorkflowRun`.

The node enforces the same input, output and cumulative token ceilings as LLM
Workflow nodes. Agent answers are capped at 1024 output tokens per node and the
whole run is capped by the existing Workflow budget.

Agent versions that grant plugin tools are rejected at team publish time with
`TEAM_AGENT_TOOLS_UNSUPPORTED`. Plugin calls require per-call approval and
checkpointing that does not yet exist for team members. A tool-free version of
the Agent can still be used in a team.

Parallel members, conditional routing, loops and human approval nodes are not
implemented yet.
