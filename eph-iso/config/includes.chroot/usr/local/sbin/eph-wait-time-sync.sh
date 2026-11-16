#!/usr/bin/env bash
set -euo pipefail

command -v timedatectl >/dev/null 2>&1 || exit 1
systemctl enable --now systemd-timesyncd.service
timedatectl set-ntp true

for attempt in $(seq 1 180); do
    synchronized="$(timedatectl show -p NTPSynchronized --value 2>/dev/null || true)"
    if [ "$synchronized" = yes ]; then
        printf '%s\n' "EPH_SYSTEM_CLOCK_SYNCHRONIZED=true"
        exit 0
    fi
    sleep 2
done

timedatectl status >&2 || true
journalctl -b -u systemd-timesyncd.service --no-pager -n 80 >&2 || true
printf '%s\n' "EPH_SYSTEM_CLOCK_SYNCHRONIZATION_TIMEOUT" >&2
exit 1
