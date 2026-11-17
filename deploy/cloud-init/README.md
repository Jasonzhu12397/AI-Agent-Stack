# Site NTP and DNS cloud-init

This is a required deployment input for both the temporary EPH Live OS and the
final Target OS. It is deliberately separate from the Agent Pod manifests:
Pods inherit the host clock and DNS path; installing chronyd inside a Pod does
not synchronize the Kubernetes node clock.

## Render user-data

Use the same site-approved NTP and DNS endpoints for all three Target masters
and every EPH host unless the site network design specifies different resolvers.
The following public endpoints are examples for isolated connectivity tests
only; replace them with your organization's approved NTP and DNS services for
production.

```bash
python3 deploy/cloud-init/render_site_cloud_init.py \
  --role eph-live-os \
  --ntp-server time.cloudflare.com \
  --ntp-server time.google.com \
  --dns-server 1.1.1.1 \
  --dns-server 8.8.8.8 \
  --dns-test-name time.cloudflare.com \
  --search-domain lab.example \
  --output build/cloud-init/eph-live-os.user-data

python3 deploy/cloud-init/render_site_cloud_init.py \
  --role target-os \
  --ntp-server time.cloudflare.com \
  --ntp-server time.google.com \
  --dns-server 1.1.1.1 \
  --dns-server 8.8.8.8 \
  --dns-test-name time.cloudflare.com \
  --search-domain lab.example \
  --output build/cloud-init/target-os.user-data
```

The renderer requires explicit NTP and DNS values. It does not silently inject
public resolvers into a production deployment. It validates NTP hostnames/IPs,
DNS IP addresses, the DNS probe hostname and search domains, and writes cloud-init user-data that:
- installs the `chrony` package;
- configures NTP servers with `iburst`, `makestep` and `rtcsync`;
- enables the distro's `chrony.service` or `chronyd.service`;
- configures systemd-resolved DNS and search domains;
- points the host resolver at systemd-resolved's upstream resolver file (not the 127.0.0.53 stub), so kubelet and CoreDNS can use the configured upstream DNS servers without a forwarding loop;
- waits for chrony synchronization and resolves the configured DNS probe name;
- fails cloud-init if chrony or systemd-resolved cannot be enabled or the probe fails.

The current profile explicitly requires systemd and systemd-resolved. Validate
that the selected EPH Live OS and Target OS images include systemd-resolved.
Do not use this profile unchanged on an image using NetworkManager-only,
netconfig, or another resolver manager; add and test a dedicated backend first.

## Injection points

- **EPH Live OS:** pass `eph-live-os.user-data` as the user-data consumed by
  the RAM-boot/cloud-init stage before the temporary Kubernetes bootstrap
  cluster and EPH Agent start.
- **Target OS:** merge `target-os.user-data` into the cloud-init user-data
  rendered by the selected CAPI infrastructure/bootstrap provider for each
  control-plane and worker machine. The three Target masters must receive the
  same approved time/DNS policy. Do not apply it only to the Target Agent Pods.
- If the provider owns cloud-init generation (for example, Metal3DataTemplate
  or a provider-specific KubeadmConfigTemplate), merge these files and
  commands into that provider's supported user-data mechanism. Do not replace
  provider-generated kubeadm bootstrap data with this standalone file.

## Required post-boot verification

Run on every EPH host and every Target master (and preferably every worker):

```bash
systemctl is-active chronyd || systemctl is-active chrony
chronyc -n tracking
chronyc -n sources -v
timedatectl show -p NTPSynchronized --value
resolvectl status
getent hosts kubernetes.default.svc
```

A deployment preflight must fail if chronyd is not active, the clock is not
synchronized, configured DNS servers/search domains are absent, or required
control-plane/registry/Ironic endpoints cannot resolve. NTP reachability alone
is not proof of synchronization. Kubernetes certificates, etcd and CAPI
controllers depend on correct host time.

## Network policy

Permit outbound UDP/123 from EPH and Target nodes to the configured NTP servers,
and UDP/TCP 53 to the configured DNS resolvers. Add TCP/443 or the required
site-specific ports for the cloud Control Plane, image registry, Kubernetes
API and infrastructure services. Do not assume public NTP/DNS are reachable
from a restricted data-center network.


## Use the same profile that is hashed into the Cluster Delivery Plan

The EPH `cluster.plan` payload must include a versioned `time_dns_profile`.
The planner rejects a profile that omits chrony, does not cover both EPH Live OS
and Target OS, has invalid NTP/DNS endpoints, or uses an unsupported resolver
backend. The canonical cluster plan hash includes this profile.

Example profile:

```json
{
  "version": "v1",
  "chrony_required": true,
  "resolver_backend": "systemd-resolved",
  "apply_to": ["eph_live_os", "target_os"],
  "ntp_servers": ["time.cloudflare.com", "time.google.com"],
  "dns_servers": ["1.1.1.1", "8.8.8.8"],
  "search_domains": ["lab.example"],
  "dns_probe_name": "time.cloudflare.com"
}
```

Save the exact approved profile returned with the deployment plan, then render
both role-specific user-data files from it:

```bash
python3 deploy/cloud-init/render_site_cloud_init.py \
  --role eph-live-os \
  --profile-json build/time-dns-profile.json \
  --output build/cloud-init/eph-live-os.user-data

python3 deploy/cloud-init/render_site_cloud_init.py \
  --role target-os \
  --profile-json build/time-dns-profile.json \
  --output build/cloud-init/target-os.user-data
```

This prevents the operator from using a different NTP/DNS configuration than
the one reviewed and hashed in the Cluster Delivery Plan. The profile is a
configuration contract; the generated user-data still has to be injected by
the EPH boot service and the selected CAPI/Metal3 bootstrap provider.
