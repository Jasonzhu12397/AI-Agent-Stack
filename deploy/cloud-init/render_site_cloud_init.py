#!/usr/bin/env python3
"""Render role-specific cloud-init user-data for EPH Live OS or Target OS."""

import argparse
import ipaddress
import json
import re
import sys
from pathlib import Path

HOST_LABEL_PATTERN = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
HOST_RE = re.compile(
    r"^(?=.{1,253}$)" + HOST_LABEL_PATTERN + r"(?:\." + HOST_LABEL_PATTERN + r")*$"
)
DOMAIN_RE = HOST_RE


def validate_ntp_server(value):
    value = value.strip()
    if not value or any(ch.isspace() for ch in value):
        raise argparse.ArgumentTypeError("NTP server must be an IP address or DNS hostname")
    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        if not HOST_RE.fullmatch(value):
            raise argparse.ArgumentTypeError("invalid NTP server hostname")
        return value


def validate_dns_server(value):
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        raise argparse.ArgumentTypeError("DNS server must be an IPv4 or IPv6 address")


def validate_search_domain(value):
    value = value.strip()
    if not DOMAIN_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("invalid DNS search domain")
    return value


def render(args):
    chrony_lines = [
        "# Managed by AI-Agent-Stack cloud-init; do not edit by hand.",
        "driftfile /var/lib/chrony/drift",
        "makestep 1.0 3",
        "rtcsync",
        "leapsectz right/UTC",
    ]
    chrony_lines.extend("server {} iburst".format(server) for server in args.ntp_server)
    chrony_content = "\n".join(chrony_lines) + "\n"

    resolved_lines = [
        "# Managed by AI-Agent-Stack cloud-init; do not edit by hand.",
        "[Resolve]",
        "DNS=" + " ".join(args.dns_server),
    ]
    if args.search_domain:
        resolved_lines.append("Domains=" + " ".join(args.search_domain))
    resolved_content = "\n".join(resolved_lines) + "\n"

    apply_script = """#!/bin/sh
set -eu
if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemd is required for chronyd and systemd-resolved" >&2
  exit 1
fi
if ! systemctl cat systemd-resolved.service >/dev/null 2>&1; then
  echo "systemd-resolved is required by this DNS cloud-init profile" >&2
  exit 1
fi
systemctl enable --now systemd-resolved.service
if [ ! -e /run/systemd/resolve/stub-resolv.conf ]; then
  echo "systemd-resolved stub resolv.conf is missing" >&2
  exit 1
fi
ln -sfn /run/systemd/resolve/stub-resolv.conf /etc/resolv.conf
systemctl restart systemd-resolved.service
if systemctl cat chronyd.service >/dev/null 2>&1; then
  chrony_unit=chronyd.service
elif systemctl cat chrony.service >/dev/null 2>&1; then
  chrony_unit=chrony.service
else
  echo "chrony service unit not found; verify the target image package repository" >&2
  exit 1
fi
systemctl enable --now "$chrony_unit"
systemctl restart "$chrony_unit"
chronyc waitsync 10 0.1
chronyc -n tracking
resolvectl query __DNS_TEST_NAME__
resolvectl status
"""

    apply_script = apply_script.replace("__DNS_TEST_NAME__", args.dns_test_name)

    files = [
        ("/etc/chrony/chrony.conf", chrony_content, "0644"),
        ("/etc/chrony.conf", chrony_content, "0644"),
        ("/etc/systemd/resolved.conf.d/90-ai-agent-site-dns.conf", resolved_content, "0644"),
        ("/usr/local/sbin/ai-agent-site-time-dns-apply", apply_script, "0755"),
        ("/etc/ai-agent/site-network-time-role", args.role + "\n", "0644"),
    ]
    output = ["#cloud-config", "package_update: true", "packages:", "  - chrony", "write_files:"]
    for path, content, permissions in files:
        output.extend([
            "  - path: " + path,
            "    owner: root:root",
            "    permissions: '" + permissions + "'",
            "    content: " + json.dumps(content),
        ])
    output.extend([
        "runcmd:",
        "  - [ /usr/local/sbin/ai-agent-site-time-dns-apply ]",
    ])
    return "\n".join(output) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", required=True, choices=("eph-live-os", "target-os"))
    parser.add_argument(
        "--ntp-server",
        action="append",
        type=validate_ntp_server,
        required=True,
    )
    parser.add_argument(
        "--dns-server",
        action="append",
        type=validate_dns_server,
        required=True,
    )
    parser.add_argument(
        "--search-domain",
        action="append",
        type=validate_search_domain,
        default=[],
    )
    parser.add_argument(
        "--dns-test-name",
        type=validate_search_domain,
        required=True,
    )
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    content = render(args)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(destination)
    print("Generated {} cloud-init user-data for {}".format(destination, args.role))
    return 0


if __name__ == "__main__":
    sys.exit(main())
