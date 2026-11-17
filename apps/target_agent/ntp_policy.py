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
