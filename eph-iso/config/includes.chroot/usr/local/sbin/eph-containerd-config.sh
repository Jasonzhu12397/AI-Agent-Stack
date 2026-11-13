#!/usr/bin/env bash
set -euo pipefail

CONFIG_DIR="/etc/containerd"
CONFIG_FILE="$CONFIG_DIR/config.toml"
TEMP_FILE="$CONFIG_DIR/config.toml.eph-tmp"

install -d -m 0755 "$CONFIG_DIR" "$CONFIG_DIR/certs.d"
containerd config default > "$TEMP_FILE"
sed -i 's|config_path = ""|config_path = "/etc/containerd/certs.d"|' "$TEMP_FILE"
sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' "$TEMP_FILE"
sed -i -E 's#sandbox_image = "registry.k8s.io/pause:[^"]+"#sandbox_image = "registry.k8s.io/pause:3.10"#' "$TEMP_FILE"

grep -Fq 'config_path = "/etc/containerd/certs.d"' "$TEMP_FILE"
grep -Fq 'SystemdCgroup = true' "$TEMP_FILE"
grep -Fq 'sandbox_image = "registry.k8s.io/pause:3.10"' "$TEMP_FILE"

chmod 0644 "$TEMP_FILE"
mv -f "$TEMP_FILE" "$CONFIG_FILE"
