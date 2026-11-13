#!/usr/bin/env bash
set -euo pipefail

if mountpoint -q /mnt/eph-bootstrap; then
    exit 0
fi

device=""
for attempt in $(seq 1 5); do
    device="$(blkid -L EPHBOOT 2>/dev/null || true)"
    [ -n "$device" ] && break
    sleep 2
done

[ -n "$device" ] || {
    printf '%s\n' "EPH_BOOTSTRAP_MEDIA_NOT_FOUND" >&2
    exit 1
}
install -d -m 0700 /mnt/eph-bootstrap
mount -o ro,nosuid,nodev,noexec "$device" /mnt/eph-bootstrap
