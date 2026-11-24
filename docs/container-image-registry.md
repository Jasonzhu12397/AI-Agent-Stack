# Central Container Image Registry

The **Central Container Image Registry** is the platform's single supply chain
for every container image that runs on behalf of an installation: the SaaS
services themselves, the three infrastructure Agents, the Metal3/Ironic/CAPI
stack, Kubernetes components, networking, observability and tenant workloads.

It is a **mandatory platform capability**, not an optional plugin. Agents and
Kubernetes clusters are not allowed to resolve images from the public internet
in place. They consume the registry's digest-pinned catalog entry, and EPH /
Target nodes pull from the internal registry host (or from the EPH local
registry that mirrors it for disconnected sites).

```text
code commit → CI/CD build → security scan → central registry → image promotion
            → EPH bootstrap → target Kubernetes → worker deployment → monitoring
```

## Model

Two tables in `apps/platform-api/core/registry_models.py` back the catalog.

| Model | Purpose |
| --- | --- |
| `RegistryImage` | One immutable image revision: repository path, digest, category, size, scan state, signature state, promotion stage, source repository and build reference. Unique per `(project, name, digest)`. |
| `RegistryEvent` | Append-only trail: `registered`, `scanned`, `signed`, `promoted`, `pulled`, `retired`, with actor and detail payload. |

Fields that matter for enforcement:

- `digest` — must match `sha256:<64 hex>`. A tag is rejected: tags move, digests
  do not. The API never accepts `latest` or a mutable reference.
- `category` — `platform`, `agent`, `infrastructure`, `kubernetes`, `network`,
  `observability` or `workload`.
- `status` — `building`, `scanning`, `ready`, `blocked`, `retired`. A new image
  starts in `scanning`; a failed scan moves it to `blocked`.
- `stage` — `dev`, `staging`, `production`.
- `scan_status`, `scan_findings`, `sbom_ref`, `signed`, `promoted_at`.

## Gates

1. **Scan before use.** An image is `ready` only after `scan_status=passed`.
   A failed scan makes it `blocked`, and blocked images never appear in a pull
   plan.
2. **Signature before production.** Promoting to `production` requires
   `scan_status=passed` **and** `signed=true`.
3. **No stage skipping.** `dev → staging → production` only. A direct
   `dev → production` promotion is refused.
4. **Digest-only deployment.** `pull-plan` returns internal references of the
   form `<PLATFORM_REGISTRY_HOST>/<name>@sha256:<digest>` and reports
   `digest_pinned: true`. It lists only `ready` images.
5. **Internal host only.** The host comes from `PLATFORM_REGISTRY_HOST`
   (default `registry.ai-agent-stack.internal`), so a deployment manifest can
   be checked for accidental public references.

## Ownership

- **Cloud Control Agent** coordinates build and release: it drives the promotion
  transitions and holds the installation-wide view of what is eligible.
- **EPH-Agent** consumes the pull plan during bootstrap: it seeds or imports the
  images into the EPH local registry before deploying the remaining workloads.
- **Target-Agent** consumes the same plan for the target control plane and
  workers, and reports usage state back through the command channel.

The catalog does not replace the offline image factory
(`deploy/offline/README.md`). The factory builds and packages archives for
disconnected sites; the catalog is the authority on which digest is allowed to
run at which stage.

## API

Base path: `/api/v1/tenants/<tenant_id>/projects/<project_id>/registry/`.

| Method | Path | Behaviour |
| --- | --- | --- |
| `GET` | `images/?category=&stage=&status=` | Filtered catalog list |
| `POST` | `images/` | Register an image; rejects tags, unknown categories (`400`) and duplicates (`409`) |
| `GET` | `images/<id>/` | Image detail |
| `POST` | `images/<id>/scan/` | Record scan result; `passed` → `ready`, `failed` → `blocked` |
| `POST` | `images/<id>/sign/` | Mark the image signed; requires a passed scan |
| `POST` | `images/<id>/promote/` | Promote to `dev`, `staging` or `production` under the gates above |
| `GET` | `images/<id>/events/` | Lifecycle trail for one image |
| `GET` | `summary/` | Counts by category, stage and status, plus `signed`, `scan_passed`, `registry_host` |
| `GET` | `pull-plan/?category=` | Digest-pinned internal references for agents and clusters |

Configuration:

```bash
PLATFORM_REGISTRY_HOST=registry.ai-agent-stack.internal   # default
```

## Web Studio

`Image Registry` in the operator workspace
(`apps/web-studio/src/components/RegistryView.vue`) renders the same data:
catalog counts, category/stage/status filters, per-image scan and signature
actions, promotion to the next allowed stage, per-image history, and the pull
plan that EPH and Target consume. The page reloads when the workspace switches
tenant or project, so the selection in the sidebar always matches the data.

## Tests

`tests/test_registry.py` covers the contract: tag rejection, unknown category,
duplicate conflict, ladder enforcement, production signature gate, blocked
images after a failed scan, internal-only digest-pinned pull plan, summary
counts and the event trail.
