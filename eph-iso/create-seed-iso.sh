#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$ROOT_DIR/.." && pwd)"

fail() {
    printf '%s\n' "$1" >&2
    exit 1
}

[ "$#" -eq 2 ] || fail "USAGE: create-seed-iso.sh SEED_ARCHIVE.zip OUTPUT.iso"
input_archive="$1"
output_iso="$2"

[ -f "$input_archive" ] && [ ! -L "$input_archive" ] || fail "SEED_ARCHIVE_NOT_FOUND"
[ ! -e "$output_iso" ] && [ ! -L "$output_iso" ] || fail "SEED_ISO_OUTPUT_ALREADY_EXISTS"

for command_name in python3 sha256sum mktemp; do
    command -v "$command_name" >/dev/null 2>&1 || fail "SEED_ISO_DEPENDENCY_MISSING:$command_name"
done

temporary_dir="$(mktemp -d)"
trap 'rm -rf "$temporary_dir"' EXIT

python3 - "$input_archive" "$temporary_dir" <<'PY'
import pathlib
import stat
import sys
import zipfile

archive_path = pathlib.Path(sys.argv[1])
output_dir = pathlib.Path(sys.argv[2])
expected = {"user-data", "meta-data", "network-config", "time-dns-profile.json"}

try:
    with zipfile.ZipFile(str(archive_path), "r") as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        if len(names) != len(expected) or set(names) != expected:
            raise ValueError("SEED_ARCHIVE_CONTENTS_INVALID")
        for entry in entries:
            mode = entry.external_attr >> 16
            if stat.S_ISLNK(mode) or entry.is_dir() or entry.flag_bits & 1:
                raise ValueError("SEED_ARCHIVE_ENTRY_INVALID")
            if entry.file_size > 262144 or entry.compress_size > 262144:
                raise ValueError("SEED_ARCHIVE_ENTRY_TOO_LARGE")
            content = archive.read(entry)
            if len(content) != entry.file_size:
                raise ValueError("SEED_ARCHIVE_ENTRY_SIZE_MISMATCH")
            (output_dir / entry.filename).write_bytes(content)
except (OSError, ValueError, zipfile.BadZipFile, RuntimeError):
    raise SystemExit("SEED_ARCHIVE_INVALID")
PY

python3 "$REPO_ROOT/deploy/cloud-init/render_site_cloud_init.py" \
    --role eph-live-os \
    --profile-json "$temporary_dir/time-dns-profile.json" \
    --output "$temporary_dir/time-dns-user-data" ||
    fail "EPH_TIME_DNS_CLOUD_INIT_RENDER_FAILED"

python3 "$ROOT_DIR/merge-cloud-init-user-data.py" \
    --base "$temporary_dir/user-data" \
    --additional "$temporary_dir/time-dns-user-data" \
    --output "$temporary_dir/merged-user-data" ||
    fail "EPH_CLOUD_INIT_USER_DATA_MERGE_FAILED"

mv "$temporary_dir/merged-user-data" "$temporary_dir/user-data"
rm -f "$temporary_dir/time-dns-profile.json" "$temporary_dir/time-dns-user-data"

command -v xorriso >/dev/null 2>&1 || fail "SEED_ISO_DEPENDENCY_MISSING:xorriso"
install -d -m 0700 "$(dirname -- "$output_iso")"
xorriso -as mkisofs -quiet -V CIDATA -J -R -o "$output_iso" "$temporary_dir"
[ -s "$output_iso" ] || fail "SEED_ISO_BUILD_FAILED"
chmod 0600 "$output_iso"
sha256sum "$output_iso" > "$output_iso.sha256"
chmod 0600 "$output_iso.sha256"
