# Cloud Control Plane deployment

This profile deploys the cloud-side AI Agent Studio Control Plane on a cloud VM (Alibaba Cloud or Tencent Cloud) using Docker Compose. PostgreSQL, Redis and Qdrant are external managed services; no development database or default credentials are bundled in this production profile. The EPH and Target Node Agents are deployed separately to their respective Kubernetes clusters using ../agents/deploy.sh.

## Runtime separation

The cloud deployment builds two independent application images:
- `Dockerfile`: the Studio API, authentication, tenant/project control, durable state, Agent registry, approvals and command API.
- `Dockerfile.cloud-agent`: the cloud-side AI Agent runtime (`worker_app.main`) for model execution, tool routing, RAG, workflow execution, task leasing and orchestration.

The Kubernetes-side Agent is a third, separate artifact built from `Dockerfile.node-agent`. It contains only `apps/node_agent`, PyYAML and the checksum-verified `kubectl`/`clusterctl` binaries. It does not package Django, the cloud worker, cloud API code, model-provider credentials or the platform database client. EPH and Target Agent Pods use this image with distinct node identities, credentials, ServiceAccounts and command allowlists.

The cloud runtime and Node Agent communicate only through the versioned Agent command/heartbeat API. The cloud side owns durable workflow state, LLM routing, approval and scheduling. Node Agents own only local cluster inspection and explicitly allowed cluster-side operations. Neither side executes the other side's entrypoint.

## Prerequisites

- Docker Engine and Docker Compose v2.
- A managed PostgreSQL database, Redis endpoint and Qdrant endpoint reachable from the cloud VM.
- A DNS name and HTTPS reverse proxy or cloud load balancer in front of the web service.
- Security group rules that expose only the HTTPS entry point. Keep API, PostgreSQL, Redis, Qdrant and Agent control traffic private.
- A strong production SECRET_KEY and TLS-enabled database/cache connections.

## Configure

Copy the example environment file and replace every example endpoint, password and hostname with real values from the cloud provider or secret manager:

~~~bash
cp .env.cloud.example .env.cloud
chmod 600 .env.cloud
~~~

Do not commit .env.cloud. Use a secret manager to provision the file on the VM. Set ALLOWED_HOSTS, CSRF_TRUSTED_ORIGINS and CORS_ALLOWED_ORIGINS to the actual public hostname. The web image uses a same-origin API base path and Nginx proxies /api, /health and /ws to the private Django ASGI service. Agent mTLS is available as an opt-in isolated Compose profile. It requires operator-provisioned server/client CA PEM files and explicit activation; see [Agent mutual TLS deployment contract](agent-mtls-deployment.md). Do not set mandatory enforcement until the ingress profile and Agent certificates are ready.

## Build and deploy

~~~bash
bash deploy/cloud-control-plane/deploy.sh
docker compose --env-file .env.cloud -f deploy/docker-compose.cloud.yml exec api python apps/platform-api/manage.py createsuperuser
docker compose --env-file .env.cloud -f deploy/docker-compose.cloud.yml logs -f api worker web
~~~

The API container applies Django migrations before starting Daphne. The cloud Agent worker starts only after API readiness. Nginx serves the compiled Vue Studio on port 8080 and proxies browser API and WebSocket requests to the private API service. To enable the dedicated Agent ingress, configure the certificate paths and network CIDRs in .env.cloud, set COMPOSE_PROFILES=agent-mtls and AGENT_MTLS_REQUIRED=true, then redeploy. HAProxy listens on port 8443 by default and requires verified client certificates. Bind the service to loopback by default and terminate TLS with the cloud load balancer or a host reverse proxy. If a load balancer connects directly to port 8080, set WEB_BIND_ADDRESS=0.0.0.0 and restrict the security group to the load balancer source ranges.

## Verify

- Open the HTTPS hostname and sign in using the initial administrator provisioned through the supported Django createsuperuser workflow.
- Check /health/live and /health/ready through the same HTTPS hostname.
- Verify API and WebSocket access through the proxy.
- Verify API, cloud Agent, PostgreSQL, Redis and Qdrant connectivity from the cloud network.
- Enroll EPH and Target Agents in Studio and deploy them with the two-context Agent deployment helper.

This profile deploys the cloud Control Plane containers only. It does not configure cloud IAM, managed database backups, WAF, TLS certificates, provider-specific private networking, Kubernetes CNI, Ironic/BMC reachability, or physical hardware. Those are environment-specific operational requirements and must be completed before production traffic.
