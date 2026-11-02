# Architecture baseline

## Source status

The user has established the product requirements in the conversation and supplied the complete Vibe Coding Execution Manual V2.0. The referenced standalone source document, “AI Agent Studio 开发文档与系统架构设计 V1.0（2026-10-29）”, is not present in the repository or current file attachments at this time.

This file therefore records only the architecture requirements explicitly available in the V2.0 execution manual and the project decisions already confirmed. It does not fabricate missing diagrams, API contracts, provider details, deployment topology or CRD fields. When the original V1.0 document is added, reconcile it against this baseline and record conflicts as ADRs before changing scope.

## Confirmed architecture direction

- Modular control plane with FastAPI API and independently running workers.
- React/TypeScript/Vite Studio.
- PostgreSQL is authoritative for durable business state; Redis is auxiliary.
- S3-compatible object storage and replaceable vector storage are later platform capabilities.
- Model Gateway, Agent Runtime, Workflow Engine, Policy Engine, Integration Hub, MCP Client/Server, Skills, Knowledge/RAG, Observability and isolated Plugin Executor are product modules.
- Infrastructure integration is a professional connector: Kubernetes, CAPI, Metal3, Ironic and Redfish. Start read-only, then controlled test-environment operations only after target schemas, versions and permissions are verified.
- Production writes, messaging, infrastructure changes and destructive operations are disabled unless separately authorized for a specific target/action.

## Requirement source

The full phase plan and engineering invariants are maintained in `docs/vibe-coding.md`. Verification evidence and unknowns are maintained in `docs/verification-matrix.md`.
