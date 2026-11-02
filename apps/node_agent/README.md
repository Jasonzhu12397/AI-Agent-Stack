# Remote Node Agent

The same small Python agent package supports two enrollment roles:

- `eph_node`: EPH/management-node health, host preflight and cluster-plan dry run.
- `target_cluster`: target-cluster node identity/health heartbeat. Target Kubernetes API adapters are not enabled in this first slice.

## Configuration

Set these environment variables from a protected secret source:

- `AGENT_CONTROL_URL`: control-plane origin, for example `https://studio.example.com`.
- `AGENT_NODE_ID`: UUID returned when the node was enrolled.
- `AGENT_TOKEN`: one-time enrollment token returned by the control plane; do not commit it.
- `AGENT_NODE_TYPE`: `eph_node` or `target_cluster`.
- `APP_ENV=production` enforces HTTPS.

Run one heartbeat/poll/command cycle:

```bash
python -m apps.node_agent.agent --once
```

Run continuously:

```bash
python -m apps.node_agent.agent --poll-seconds 5
```

The agent does not invoke a shell, execute arbitrary Python, apply manifests, or access a kubeconfig. It only runs the locally compiled allowlist. The first version supports node health, EPH host preflight and a validated cluster-plan dry run. CAPI/Metal3/Ironic mutations and target-cluster operations remain disabled until a versioned adapter and target environment compatibility tests are delivered.

For production, replace the development bearer credential with mTLS/short-lived workload identity and install the agent with a dedicated OS user or Kubernetes ServiceAccount.
