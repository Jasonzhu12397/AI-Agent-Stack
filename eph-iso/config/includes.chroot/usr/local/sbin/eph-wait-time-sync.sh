#!/usr/bin/env bash
set -euo pipefail

command -v systemctl >/dev/null 2>&1 || exit 1
command -v timeout >/dev/null 2>&1 || exit 1

systemctl enable --now systemd-timesyncd.service

if ! timeout --foreground 360s systemctl start systemd-time-wait-sync.service; then
    systemctl status systemd-timesyncd.service --no-pager >&2 || true
    systemctl status systemd-time-wait-sync.service --no-pager >&2 || true
    journalctl -b -u systemd-timesyncd.service -u systemd-time-wait-sync.service --no-pager -n 100 >&2 || true
    printf '%s\n' "EPH_SYSTEM_CLOCK_SYNCHRONIZATION_TIMEOUT" >&2
    exit 1
fi

if [ ! -s /run/systemd/timesync/synchronized ]; then
    journalctl -b -u systemd-timesyncd.service -u systemd-time-wait-sync.service --no-pager -n 100 >&2 || true
    printf '%s\n' "EPH_SYSTEM_CLOCK_SYNCHRONIZATION_MARKER_MISSING" >&2
    exit 1
fi

printf '%s\n' "EPH_SYSTEM_CLOCK_SYNCHRONIZED=true"
