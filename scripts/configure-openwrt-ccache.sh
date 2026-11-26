#!/bin/sh
set -eu

config_file="${1:-.config}"
[ -f "$config_file" ] || {
    echo "OpenWrt config not found: $config_file" >&2
    exit 1
}

# OpenWrt v25.12.6 has scripts/config/ as a Kconfig source directory, not the
# scripts/config command found in Linux trees. Set these symbols directly,
# removing both enabled and disabled forms emitted by Kconfig first.
sed -i \
    -e '/^CONFIG_DEVEL=/d' \
    -e '/^# CONFIG_DEVEL is not set$/d' \
    -e '/^CONFIG_CCACHE=/d' \
    -e '/^# CONFIG_CCACHE is not set$/d' \
    -e '/^CONFIG_CCACHE_DIR=/d' \
    -e '/^# CONFIG_CCACHE_DIR is not set$/d' \
    "$config_file"

printf '%s\n' \
    'CONFIG_DEVEL=y' \
    'CONFIG_CCACHE=y' \
    'CONFIG_CCACHE_DIR="/repo/ccache"' >> "$config_file"
