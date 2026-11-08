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

## Knowledge Base APIs and Agent retrieval

The API now supports project-scoped KnowledgeBase creation with a pinned embedding model and chunk settings. Text documents (up to 1 MB per request) are stored in PostgreSQL and queued as `knowledge.ingest` tasks. The Worker chunks and embeds each document, upserts vectors into Qdrant, and records ingestion status, chunk count and errors on the document.

Published Agent versions can pin up to 8 Knowledge Base IDs from the same tenant/project. At execution time the Worker embeds the user query with each selected base's embedding model, retrieves up to three tenant/project-filtered chunks per base, and injects them as explicitly untrusted reference material with document/chunk source IDs. Knowledge Base selection is immutable per published Agent version.

Workflow DAGs also support a `knowledge_search` node. The node pins 1–8 project knowledge bases, accepts a query template and bounded result limit, and emits document/chunk source IDs plus retrieved text. Downstream LLM nodes receive the results with an explicit untrusted-reference instruction. Publish and run both revalidate tenant/project scope and embedding-model capability.

The current document API accepts text content (up to 1 MB per request). Document deletion and Qdrant vector cleanup are implemented: deletion is rejected while ingestion is running, queued ingestion is cancelled, and the source document is retained if vector cleanup fails. PDF/DOCX parsing, binary file upload, reranking, retention controls and ingestion progress streaming are not implemented yet.

## Agent and Workflow integration status

The Agent Worker executes real chat completions and supports bounded OpenAI-compatible function calling. Published Agent versions may pin project-installed connector tool IDs; the Worker validates tool-call names and arguments and dispatches only through the trusted ToolRouter. High/critical-risk Agent tool calls use persisted approval requests bound to the task, tool, installation, connection, plugin version and canonical argument hash. A different project administrator must decide the request; the task is checkpointed and re-queued after approval. Workflow tool nodes still reject high/critical-risk tools because a resumable Human Approval workflow node has not been implemented. Provider-specific live connector handlers are not registered by default, so the ToolRouter fails closed when no trusted handler exists.

Knowledge Base CRUD and ingestion are implemented with PostgreSQL as the source of truth and Qdrant as the vector index. Authenticated APIs provide tenant/project-scoped Knowledge Base and text-document operations. The Worker ingests documents asynchronously, and published Agent versions can pin Knowledge Bases for retrieval. Workflow DAGs provide a `knowledge_search` node with publish/run scope validation and explicitly untrusted retrieved context. The Vue Studio includes Knowledge Base and document management views.

Remaining RAG work includes PDF/DOCX parsing, binary upload, reranking, retention controls, ingestion progress streaming, and broader failure/reconciliation testing for PostgreSQL/Qdrant consistency. Remaining Agent/Workflow work includes streaming responses, token/cost telemetry, model fallback and per-agent model routing, plus a durable Human Approval workflow node.

EPH Agent and Target Cluster Agent remain separate remote execution identities. Each may use a model profile reachable from its own runtime, including a local model endpoint; central Supervisor can use a different model profile. Credentials and model endpoints are configured per execution environment, not sent as plaintext in remote command payloads.
