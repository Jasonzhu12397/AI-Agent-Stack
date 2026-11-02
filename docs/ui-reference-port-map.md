# AI-Stack-Agent-GUI to Vue Studio port map

The design reference is [Jasonzhu12397/AI-Stack-Agent-GUI](https://github.com/Jasonzhu12397/AI-Stack-Agent-GUI). Its current frontend is React 19 + TypeScript + Vite with Tailwind CSS and lucide-react. The platform's selected frontend remains Vue 3 + TypeScript + Vite + Element Plus + Pinia. The reference's React runtime, Express demo server, mock API and local in-memory store are not copied into this project.

## Reuse and porting map

| Reference UI / module | Vue Studio destination | Porting rule |
|---|---|---|
| `Header.tsx` | `apps/web-studio/src/App.vue` navigation and account shell | Port tab hierarchy, active navigation, health/account affordances into Vue; use the Django session as the real identity source |
| `DashboardView.tsx` | Workspace Overview | Preserve dark dashboard/card hierarchy; bind only to real API values and never show mock success metrics |
| `AgentStudioView.tsx` | Existing `AgentStudioView.vue` | Keep Vue component and Django Agent endpoints; port useful layout and configuration affordances |
| `WorkflowStudioView.tsx` | Existing `WorkflowStudioView.vue` | Keep platform-owned JSON DAG contract and server publish validation; future graph canvas must use the same contract |
| `IntegrationHubView.tsx` | `IntegrationHubView.vue` | Port catalog/detail/review/install/connection-reference patterns onto Django PluginDefinition/Installation/Connection APIs; no mock connector execution is shown |
| `GovernanceView.tsx` | Infrastructure command approval and future Approval Center | Backend remains authoritative; approval must bind a canonical hash and require a different approver |
| `OrgMembersView.tsx` | Tenant/project membership pages | Use Django membership endpoints and server-derived roles, not reference demo principals |
| `AuditView.tsx` | Planned Vue Audit Trail | Bind to persistent redacted AuditEvent records; do not use browser-side demo audit data |
| `DocsAndMatrixView.tsx` | Documentation and verification matrix | Read the platform's actual Markdown documents and CI evidence |
| `domain/dagValidator.ts`, `stateMachine.ts`, `policyEngine.ts`, `sanitizer.ts` | Python domain services plus TypeScript presentation helpers | Reuse contracts and test cases where useful, but server-side Python is authoritative for validation, state transitions, policy and redaction |

## Current Vue navigation

- Overview: tenant/project workspace and durable task queue.
- Model Manager: provider/model configuration.
- Agent Studio: draft, publish and run current Agent capability.
- Workflow DAG: JSON definition, publish, run and step history.
- Integration Hub: connector catalog, tool contract metadata, admin review, project installation, permission grants and credential-reference records.
- Infrastructure Agents: EPH/Target enrollment, heartbeat, command queue, lease state and hash-bound approval.

Integration Hub now has registry and installation metadata APIs/screens. General Governance Center, organization member management and Audit Trail remain unimplemented. Plugin execution is not implied by an installation record.

## Visual system

The port keeps the reference's dark slate background, indigo active navigation, compact monospace metadata, bordered panels, responsive two-column workspace and high-density operational tables. Vue/Element Plus components replace React JSX and Tailwind-only interactions. No React bundle is shipped in the Studio.


## Knowledge Base and model setup

- `KnowledgeBaseView.vue` is the Knowledge Bases workspace. It creates project-scoped bases, selects an embedding-capable model, accepts text documents up to 1 MB and displays ingestion state/chunk counts.
- `ModelManagerView.vue` can register an OpenAI-compatible provider using a server-side `env:` credential reference, then create chat/tool or embedding model configurations. Endpoint hosts must already be present in the server's allowlist; the browser never receives credential values.
- `AgentStudioView.vue` loads project Knowledge Bases and tools from installed connectors. A new Agent draft can pin Knowledge Base IDs and installed PluginTool IDs. The published version freezes those selections.
- RAG chunks are injected as untrusted user-context material and Qdrant retrieval is filtered by tenant, project and the selected Knowledge Base ID.
- Provider-specific connector handlers, high-risk human approval, file parsers and document/vector deletion remain outside this milestone.
