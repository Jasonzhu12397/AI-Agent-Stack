# EPH Live ISO

The EPH runtime is a generic Debian 12 Live ISO. It boots entirely into RAM,
initialises a single-node Kubernetes control plane with `kubeadm`, and then runs
the EPH Agent as a Deployment inside that temporary cluster. Per-node identity,
certificates and network settings are never baked into the ISO; they arrive
through a NoCloud seed volume.

## Contract with the Agent transport

The EPH Agent is started from `packages/agent_transport` and reads its identity
only from the environment produced by
`apps/eph_agent/bootstrap_renderer.py::render_agent_deployment`:

| Variable | Meaning |
| --- | --- |
| `AGENT_CONTROL_URL` | HTTPS base URL of the Cloud Control Plane |
| `AGENT_ROLE` | `eph` |
| `AGENT_ID` | Agent UUID assigned by the control plane |
| `AGENT_INSTALLATION_ID` | Installation UUID |
| `AGENT_INSTANCE_ID` | Instance label, at most 128 characters |
| `AGENT_TLS_CA_FILE` | `/etc/agent/mtls/ca.crt` |
| `AGENT_TLS_CERT_FILE` | `/etc/agent/mtls/tls.crt` |
| `AGENT_TLS_KEY_FILE` | `/etc/agent/mtls/tls.key` |
| `AGENT_POLL_SECONDS` | Command poll interval, `(0, 300]` |
| `AGENT_HEARTBEAT_SECONDS` | Heartbeat interval, `(0, 300]` |
| `AGENT_REQUEST_TIMEOUT_SECONDS` | Per-request timeout, `(0, 300]` |

`AGENT_CLUSTER_ID` is deliberately absent: the EPH Agent is not bound to a
target cluster.

The ISO bootstrap stage (`bootstrap.py`) rejects any Deployment manifest that
lacks one of these variables, and rejects any manifest whose image placeholder
`REPLACE_WITH_REGISTRY/ai-agent-node-agent:VERSION` does not appear exactly
once. A rendered manifest that would fail with a start-up `ConfigurationError`
can therefore never be applied to the node.

## Seed volume

The seed must use the filesystem label `CIDATA` and carry these files at its
root:

* `user-data` -- cloud-config that writes `/etc/ai-agent-studio/eph-bootstrap`
  with `bootstrap.json` and the `tls/` material,
* `meta-data` -- instance ID and hostname,
* `network-config` -- cloud-init v2 network configuration.

`bootstrap.json` holds the non-secret identity:

~~~json
{
  "control_url": "https://control.example.test",
  "agent_id": "0b8a5dc2-6f6f-4b4e-9a4c-3f6a1b7c8d90",
  "installation_id": "1f0c2a44-8b1d-4c3e-8f5a-2b6c9d0e1f22",
  "instance_id": "eph-bootstrap-01",
  "agent_image": "10.0.0.10:5000/ai-agent-stack/eph-agent@sha256:<64 hex characters>",
  "target_agent_image": "10.0.0.10:5000/ai-agent-stack/target-agent@sha256:<64 hex characters>",
  "vbmc_image": "10.0.0.10:5000/ai-agent-stack/vbmc@sha256:<64 hex characters>",
  "sushy_tools_image": "10.0.0.10:5000/ai-agent-stack/sushy-tools@sha256:<64 hex characters>"
}
~~~

`agent_image` must be pinned by digest. `instance_id` is optional and defaults
to the node hostname when the Deployment is rendered.

The `tls/` directory carries:

* `ca.crt` -- trust anchor for the Cloud Control Plane endpoint,
* `agent-ca.crt` -- issuer of the Agent client certificate,
* `tls.crt`, `tls.key` -- the issued Agent client certificate and its key.

Certificates are issued out of band by the control plane. There is no
enrollment endpoint and no one-time bearer token in this contract: the control
plane mints the client certificate and the seed distributes it, so the node
never exchanges credentials at boot. `bootstrap.py` verifies the leaf against
`agent-ca.crt` with `openssl verify -x509_strict -purpose sslclient`, and
refuses to continue when the certificate does not match the private key
(`CLIENT_CERTIFICATE_KEY_MISMATCH`).

Never commit real certificates, private keys or kubeconfigs. Treat the seed as
secret material and destroy it after the node is provisioned.

## Pod-local registry and BMC services

A digest-pinned Agent image requires a `registry` block in the bootstrap spec.
The renderer writes Kubernetes Deployment manifests and
`eph-iso/local_registry.py` runs after `kubeadm init`. The ISO must install that
script at `/usr/local/lib/ai-agent-stack/local_registry.py` and carry local image
archives; there is no Docker Compose stack or external registry pull.

`registry`, `vbmc`, `sushy-tools` and `httpd-infra` are Deployments in namespace
`ai-agent-system`. The EPH Agent is a Deployment in the same cluster, and BMH,
CAPI, BMO and Ironic resources are queried from that cluster directly. Target
Agent is a Deployment in the target Kubernetes cluster. The registry Pod uses host networking on the provisioning NIC
and a hostPath data volume. The BMC Pod container and Pod specs come from the
seed. The runtime performs this sequence:

1. Wait for the EPH API, import the registry archive into containerd, apply the
   registry Deployment and wait for rollout and `/v2/` readiness.
2. Import, tag and push the EPH Agent, Target Agent, vbmc, sushy-tools and
   httpd-infra archives to the registry Pod.
3. Read each published manifest digest and require an exact pinned match.
4. Apply the vbmc, sushy-tools and httpd-infra Deployments and wait for all rollouts.

The same registry endpoint is written into the target control-plane and worker
Kubeadm `files` as containerd `hosts.toml`. Remote nodes therefore pull Agent and
workload images from the EPH provisioning NIC. The default listener is HTTP on
the isolated provisioning network; no public registry is required.

## Boot sequence

1. cloud-init consumes the NoCloud seed and writes the bootstrap material with
   root ownership and `0600` permissions.
2. kubelet, containerd and `kubeadm init` bring up the single-node control plane.
3. The registry Pod starts, all ISO image archives are published and digest
   checked, and the vbmc/sushy-tools/httpd-infra Pods become ready.
4. `bootstrap.py` waits for `kubectl get --raw=/readyz` to return `ok`, then
   waits for the node to report `Ready`.
5. The three TLS files are copied into a temporary directory, verified locally,
   and stored as the `eph-agent-mtls` Secret in namespace `ai-agent-system`.
6. `namespace.yaml`, `rbac.yaml` and the rendered `eph-node-agent.yaml` are
   applied, then `rollout status deployment/eph-agent` and a Pod readiness
   check confirm the Agent joined the control plane.

Optional CAPI pivot kubeconfigs are accepted only as a complete
`capi/source.kubeconfig` + `capi/target.kubeconfig` pair; a single file is
rejected with `CAPI_PIVOT_KUBECONFIG_SET_INCOMPLETE`.

## Failure codes

Validation failures raise `BootstrapError` with a stable code, including
`BOOTSTRAP_CONFIG_NOT_FOUND`, `CONTROL_URL_INVALID`, `AGENT_ID_INVALID`,
`INSTALLATION_ID_INVALID`, `AGENT_IMAGE_NOT_PINNED_BY_DIGEST`,
`BOOTSTRAP_SECRET_FILE_MISSING`, `CLIENT_CERTIFICATE_VALIDATION_FAILED`,
`EPH_AGENT_DEPLOYMENT_ENVIRONMENT_INCOMPLETE` and `EPH_AGENT_POD_NOT_READY`.
`python3 bootstrap.py` prints the code to stderr and exits non-zero.

## Manifests

`bootstrap.py` reads `namespace.yaml`, `rbac.yaml` and `eph-node-agent.yaml`
from `/usr/share/ai-agent-studio/eph-manifests`, which the ISO build installs
from the control plane renderer output. The Deployment is applied only after
the image placeholder has been replaced with the digest-pinned
`agent_image` from `bootstrap.json`.
