#!/usr/bin/env bash
set -euo pipefail

command -v systemctl >/dev/null 2>&1 || exit 1
systemctl enable --now systemd-timesyncd.service

for attempt in $(seq 1 180); do
    if [ -s /run/systemd/timesync/synchronized ]; then
        printf '%s\n' "EPH_SYSTEM_CLOCK_SYNCHRONIZED=true"
        exit 0
    fi
    sleep 2
done

systemctl status systemd-timesyncd.service --no-pager >&2 || true
journalctl -b -u systemd-timesyncd.service --no-pager -n 80 >&2 || true
printf '%s\n' "EPH_SYSTEM_CLOCK_SYNCHRONIZATION_TIMEOUT" >&2
exit 1
