from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable


class EPHNtpPolicyError(ValueError):
    pass


def _normalize_source(value: str) -> str:
    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
        raise EPHNtpPolicyError("EPH_NTP_SOURCE_INVALID")
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        hostname = (
            r"(?=.{1,253}$)"
            r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)"
            r"(?:\.(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*"
        )
        if not re.fullmatch(hostname, value):
            raise EPHNtpPolicyError("EPH_NTP_SOURCE_INVALID") from None
        return value.lower()
    if (
        address.is_unspecified
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
    ):
        raise EPHNtpPolicyError("EPH_NTP_SOURCE_NOT_ROUTABLE")
    return str(address)


def validate_eph_bootstrap_ntp_sources(
    configured_sources: Iterable[str],
    approved_sources: Iterable[str],
) -> tuple[str, ...]:
    configured = tuple(_normalize_source(item) for item in configured_sources)
    approved = tuple(_normalize_source(item) for item in approved_sources)
    if not configured or not approved:
        raise EPHNtpPolicyError("EPH_APPROVED_BOOTSTRAP_NTP_REQUIRED")
    if len(configured) != len(set(configured)) or len(approved) != len(set(approved)):
        raise EPHNtpPolicyError("EPH_NTP_SOURCE_DUPLICATE")
    if set(configured) != set(approved):
        raise EPHNtpPolicyError("EPH_NTP_SOURCE_NOT_APPROVED")
    return tuple(sorted(approved))


def render_eph_chrony_config(approved_sources: Iterable[str]) -> str:
    sources = validate_eph_bootstrap_ntp_sources(approved_sources, approved_sources)
    lines = [f"server {source} iburst" for source in sources]
    lines.extend((
        "makestep 1.0 3",
        "rtcsync",
        "driftfile /var/lib/chrony/drift",
        "logdir /var/log/chrony",
    ))
    return "\n".join(lines) + "\n"


def render_eph_chrony_pre_kubeadm_command(approved_sources: Iterable[str]) -> str:
    sources = validate_eph_bootstrap_ntp_sources(approved_sources, approved_sources)
    config = render_eph_chrony_config(sources)
    config_lines = "\\n".join(config.rstrip("\\n").splitlines())
    return f"""#!/usr/bin/env bash
set -euo pipefail

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
cat > "$CHRONY_CONF" <<'EOF'
{config_lines}
EOF
systemctl enable "$CHRONY_SERVICE"
systemctl restart "$CHRONY_SERVICE"

for attempt in $(seq 1 30); do
    if chronyc tracking | grep -Eiq '^Leap status[[:space:]]*:[[:space:]]*Normal'; then
        exit 0
    fi
    sleep 2
done

echo 'EPH bootstrap chrony did not synchronize with an approved source' >&2
exit 1
"""
