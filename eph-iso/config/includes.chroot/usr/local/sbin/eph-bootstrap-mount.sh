#!/usr/bin/env bash
set -euo pipefail

device=""
for attempt in $(seq 1 60); do
    device="$(blkid -L EPHBOOT 2>/dev/null || true)"
    [ -n "$device" ] && break
    sleep 2
done

[ -n "$device" ] || {
    printf '%s\n' "EPH_BOOTSTRAP_MEDIA_NOT_FOUND" >&2
    exit 1
}
install -d -m 0700 /run/eph-bootstrap-media
mount -o ro,nosuid,nodev,noexec "$device" /run/eph-bootstrap-media
