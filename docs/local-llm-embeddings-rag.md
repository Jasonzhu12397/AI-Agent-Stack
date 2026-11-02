# Local LLM, Embeddings and RAG Runtime

## Runtime architecture

The platform uses one Model Gateway for chat completions and embeddings. Providers are configured per tenant and models are versioned independently. Agent versions pin a model configuration ID; the runtime resolves that model at execution time and enforces tenant ownership.

Supported provider modes:

- Remote OpenAI-compatible endpoints: HTTPS only, host must be listed in `MODEL_PROVIDER_ALLOWED_HOSTS`, and credentials must be referenced as `env:VARIABLE_NAME`.
- Local OpenAI-compatible endpoints: HTTP is allowed only when the hostname is explicitly listed in `LOCAL_MODEL_ALLOWED_HOSTS`. Credentials are optional for local endpoints; when present, they still use an environment reference. This supports Ollama, vLLM, LM Studio and compatible local inference servers exposing `/v1/chat/completions` and `/v1/embeddings`.

The host allowlist is a security boundary, not a convenience setting. Do not put arbitrary user-controlled hosts into it. In production, use private service DNS names or private IPs, network policies, egress controls and secret references.

## Model capabilities

Configure chat models with the `chat` capability, function-calling models with `tools`, and embedding models with `embeddings`. The embedding gateway validates model capability, response count, vector dimensions and finite numeric values. It normalizes provider responses into a stable response contract.

Example local provider:

- Provider type: `openai_compatible`
- Base URL: `http://ollama:11434/v1`
- Credential reference: empty (or `env:LOCAL_MODEL_API_KEY` if required)
- Local model host allowlist: `ollama`
- Chat model: a model installed in Ollama, such as `qwen2.5:14b`
- Embedding model: a separate embedding model that supports the OpenAI-compatible embeddings endpoint

Chat and embedding model configurations should be separate model records even if they share one provider endpoint.

## Vector database

Qdrant is the default vector store. The adapter supports collection creation, upsert and similarity search. Every point payload includes tenant and project IDs. Every search requires both IDs and injects mandatory Qdrant payload filters; callers cannot request an unscoped search through this adapter.

Qdrant is not the source of truth for tenants, projects, document permissions or document lifecycle. PostgreSQL remains authoritative for those records. Production should add payload indexes for `tenant_id`, `project_id` and `metadata.document_id`, backups, TLS, API-key authentication and network policies. The development Compose profile exposes Qdrant locally without authentication and must not be exposed to untrusted networks.

## RAG pipeline

`core.rag_runtime` provides:

1. Tenant/project and embedding-model scope validation.
2. Deterministic text chunking with configurable overlap.
3. Embedding batches of at most 128 chunks.
4. Qdrant upsert with stable point IDs and document metadata.
5. Query embedding and mandatory tenant/project-filtered retrieval.

The returned retrieval results are context candidates, not trusted instructions. The LLM prompt builder must label retrieved content as untrusted reference material, preserve document/chunk citations and never let retrieved text override system or policy instructions.

## Agent and Workflow integration status

The Agent Worker executes real chat completions and now supports bounded OpenAI-compatible function calling. Published Agent versions may pin project-installed connector tool IDs; the Worker validates tool-call names/arguments and dispatches only through the trusted ToolRouter. Connector-specific handlers are not registered by default, and high/critical-risk tools remain blocked until a human-approval workflow exists. Embedding and Qdrant retrieval primitives are implemented, but they are not yet exposed as Knowledge Base CRUD APIs or automatically injected into Agent/Workflow prompts.

The next integration milestone is:
- Add KnowledgeBase, KnowledgeDocument and ingestion-job persistence in PostgreSQL.
- Add authenticated tenant/project-scoped document upload and ingestion APIs.
- Add retrieval configuration to published Agent and Workflow versions.
- Inject retrieved chunks into LLM context with source citations.
- Add function/tool calling to the Model Gateway and connect approved tool calls to Tool Router.
- Add streaming responses, token/cost telemetry, model fallback and per-agent model routing.

EPH Agent and Target Cluster Agent remain separate remote execution identities. Each may use a model profile reachable from its own runtime, including a local model endpoint; central Supervisor can use a different model profile. Credentials and model endpoints are configured per execution environment, not sent as plaintext in remote command payloads.
