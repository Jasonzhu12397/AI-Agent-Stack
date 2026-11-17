from __future__ import annotations

import ipaddress
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
