from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable


class NtpPolicyError(ValueError):
    pass


def validate_ironic_internal_ntp_ip(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise NtpPolicyError("IRONIC_INTERNAL_NTP_IP_INVALID") from exc
    if (
        address.is_unspecified
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
    ):
        raise NtpPolicyError("IRONIC_INTERNAL_NTP_IP_NOT_ROUTABLE")
    return str(address)


def validate_worker_ntp_sources(
    ironic_internal_ip: str,
    configured_sources: Iterable[str],
) -> tuple[str, ...]:
    expected = validate_ironic_internal_ntp_ip(ironic_internal_ip)
    sources = tuple(configured_sources)
    if len(sources) != 1:
        raise NtpPolicyError("WORKER_MUST_HAVE_EXACTLY_ONE_NTP_SOURCE")
    try:
        normalized = tuple(validate_ironic_internal_ntp_ip(source) for source in sources)
    except NtpPolicyError as exc:
        raise NtpPolicyError("WORKER_NTP_SOURCE_INVALID") from exc
    if normalized != (expected,):
        raise NtpPolicyError("WORKER_NTP_SOURCE_MUST_BE_IRONIC_INTERNAL_IP")
    return normalized


def render_worker_chrony_config(ironic_internal_ip: str) -> str:
    endpoint = validate_ironic_internal_ntp_ip(ironic_internal_ip)
    return "\n".join((
        f"server {endpoint} iburst prefer",
        "makestep 1.0 3",
        "rtcsync",
        "driftfile /var/lib/chrony/drift",
        "logdir /var/log/chrony",
        "",
    ))


def render_worker_chrony_pre_kubeadm_command(ironic_internal_ip: str) -> str:
    endpoint = validate_ironic_internal_ntp_ip(ironic_internal_ip)
    return f"""#!/usr/bin/env bash
set -euo pipefail

NTP_SERVER='{endpoint}'
if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y chrony
    CHRONY_CONF=/etc/chrony/chrony.conf
    CHRONY_SERVICE=chrony
elif command -v zypper >/dev/null 2>&1; then
    zypper --non-interactive install chrony
    CHRONY_CONF=/etc/chrony.conf
    CHRONY_SERVICE=chronyd
elif command -v dnf >/dev/null 2>&1; then
    dnf install -y chrony
    CHRONY_CONF=/etc/chrony.conf
    CHRONY_SERVICE=chronyd
elif command -v yum >/dev/null 2>&1; then
    yum install -y chrony
    CHRONY_CONF=/etc/chrony.conf
    CHRONY_SERVICE=chronyd
else
    echo 'Unsupported package manager for chrony' >&2
    exit 1
fi

install -d -m 0755 "$(dirname "$CHRONY_CONF")"
cat > "$CHRONY_CONF" <<EOF
server $NTP_SERVER iburst prefer
makestep 1.0 3
rtcsync
driftfile /var/lib/chrony/drift
EOF

systemctl enable "$CHRONY_SERVICE"
systemctl restart "$CHRONY_SERVICE"

for attempt in $(seq 1 30); do
    if chronyc -n sources | awk -v server="$NTP_SERVER" '
        $1 == "^*" && $2 == server {{ selected = 1 }}
        END {{ exit !selected }}
    ' && chronyc tracking | grep -Eiq '^Leap status[[:space:]]*:[[:space:]]*Normal'; then
        exit 0
    fi
    sleep 2
done

echo "Chrony did not synchronize with the required Ironic internal NTP endpoint" >&2
exit 1
"""


def _validate_ntp_source(value: str) -> str:
    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
        raise NtpPolicyError("CONTROL_PLANE_EXTERNAL_NTP_SOURCE_INVALID")
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        if not re.fullmatch(
            r"(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(?:\.(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*",
            value,
        ):
            raise NtpPolicyError("CONTROL_PLANE_EXTERNAL_NTP_SOURCE_INVALID")
        return value.lower()


def validate_control_plane_ntp_profile(
    configured_external_sources: Iterable[str],
    approved_external_sources: Iterable[str],
    worker_networks: Iterable[str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    configured = tuple(_validate_ntp_source(source) for source in configured_external_sources)
    approved = tuple(_validate_ntp_source(source) for source in approved_external_sources)
    if not configured or not approved:
        raise NtpPolicyError("CONTROL_PLANE_APPROVED_EXTERNAL_NTP_REQUIRED")
    if len(configured) != len(set(configured)) or len(approved) != len(set(approved)):
        raise NtpPolicyError("CONTROL_PLANE_EXTERNAL_NTP_DUPLICATE")
    if set(configured) != set(approved):
        raise NtpPolicyError("CONTROL_PLANE_EXTERNAL_NTP_NOT_APPROVED")
    networks = []
    for value in worker_networks:
        try:
            network = ipaddress.ip_network(value, strict=False)
        except (TypeError, ValueError) as exc:
            raise NtpPolicyError("CONTROL_PLANE_WORKER_NETWORK_INVALID") from exc
        if (
            network.is_loopback
            or network.is_link_local
            or network.is_multicast
            or network.is_unspecified
        ):
            raise NtpPolicyError("CONTROL_PLANE_WORKER_NETWORK_NOT_ROUTABLE")
        networks.append(str(network))
    if not networks or len(networks) != len(set(networks)):
        raise NtpPolicyError("CONTROL_PLANE_WORKER_NETWORK_REQUIRED_OR_DUPLICATE")
    return tuple(sorted(approved)), tuple(sorted(networks))


def render_control_plane_chrony_config(
    approved_external_sources: Iterable[str],
    worker_networks: Iterable[str],
) -> str:
    sources, networks = validate_control_plane_ntp_profile(
        approved_external_sources,
        approved_external_sources,
        worker_networks,
    )
    lines = [f"server {source} iburst" for source in sources]
    lines.extend((
        "makestep 1.0 3",
        "rtcsync",
        "driftfile /var/lib/chrony/drift",
        "port 123",
        "cmdport 0",
    ))
    lines.extend(f"allow {network}" for network in networks)
    return "\n".join(lines) + "\n"


def render_control_plane_chrony_pre_kubeadm_command(
    approved_external_sources: Iterable[str],
    worker_networks: Iterable[str],
) -> str:
    config = render_control_plane_chrony_config(
        approved_external_sources,
        worker_networks,
    )
    return f"""#!/usr/bin/env bash
set -euo pipefail

if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y chrony iproute2
    CHRONY_CONF=/etc/chrony/chrony.conf
    CHRONY_SERVICE=chrony
elif command -v zypper >/dev/null 2>&1; then
    zypper --non-interactive install chrony iproute2
    CHRONY_CONF=/etc/chrony.conf
    CHRONY_SERVICE=chronyd
elif command -v dnf >/dev/null 2>&1; then
    dnf install -y chrony iproute
    CHRONY_CONF=/etc/chrony.conf
    CHRONY_SERVICE=chronyd
elif command -v yum >/dev/null 2>&1; then
    yum install -y chrony iproute
    CHRONY_CONF=/etc/chrony.conf
    CHRONY_SERVICE=chronyd
else
    echo 'Unsupported package manager for chrony' >&2
    exit 1
fi

install -d -m 0755 "$(dirname "$CHRONY_CONF")"
cat > "$CHRONY_CONF" <<'CHRONY_EOF'
{config}CHRONY_EOF

systemctl enable "$CHRONY_SERVICE"
systemctl restart "$CHRONY_SERVICE"

for attempt in $(seq 1 30); do
    if chronyc -n sources | awk '$1 == "^*" {{ selected = 1 }} END {{ exit !selected }}' \\
        && chronyc tracking | grep -Eiq '^Leap status[[:space:]]*:[[:space:]]*Normal' \\
        && ss -lun | grep -Eq '[:.]123[[:space:]]'; then
        exit 0
    fi
    sleep 2
done

echo 'Target Control Plane chronyd is not synchronized or not listening on UDP/123' >&2
exit 1
"""
