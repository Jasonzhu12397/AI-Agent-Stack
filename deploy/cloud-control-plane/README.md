# Cloud Control Plane deployment

This profile deploys the AI Agent Studio Control Plane on a cloud VM (Alibaba Cloud or Tencent Cloud) using Docker Compose. PostgreSQL, Redis and Qdrant are external managed services; no development database or default credentials are bundled in this production profile. The EPH and Target Agents are deployed separately to their respective Kubernetes clusters using ../agents/deploy.sh.

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

Do not commit .env.cloud. Use a secret manager to provision the file on the VM. Set ALLOWED_HOSTS, CSRF_TRUSTED_ORIGINS and CORS_ALLOWED_ORIGINS to the actual public hostname. The web image uses a same-origin API base path and Nginx proxies /api, /health and /ws to the private Django ASGI service.

## Deploy

~~~bash
docker compose --env-file .env.cloud -f deploy/docker-compose.cloud.yml config --quiet
docker compose --env-file .env.cloud -f deploy/docker-compose.cloud.yml up --build -d
docker compose --env-file .env.cloud -f deploy/docker-compose.cloud.yml ps
docker compose --env-file .env.cloud -f deploy/docker-compose.cloud.yml logs -f api worker web
~~~

The API container applies Django migrations before starting Daphne. The worker starts only after API readiness. Nginx serves the compiled Vue Studio on port 8080 and proxies API and WebSocket requests to the private API service. Bind the service to loopback by default and terminate TLS with the cloud load balancer or a host reverse proxy. If a load balancer connects directly to port 8080, set WEB_BIND_ADDRESS=0.0.0.0 and restrict the security group to the load balancer source ranges.

## Verify

- Open the HTTPS hostname and sign in using the initial administrator provisioned through the supported Django createsuperuser workflow.
- Check /health/live and /health/ready through the same HTTPS hostname.
- Verify API and WebSocket access through the proxy.
- Verify API, worker, PostgreSQL, Redis and Qdrant connectivity from the cloud network.
- Enroll EPH and Target Agents in Studio and deploy them with the two-context Agent deployment helper.

This profile deploys the cloud Control Plane containers only. It does not configure cloud IAM, managed database backups, WAF, TLS certificates, provider-specific private networking, Kubernetes CNI, Ironic/BMC reachability, or physical hardware. Those are environment-specific operational requirements and must be completed before production traffic.
