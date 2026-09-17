#!/usr/bin/env bash
set -euo pipefail

FIRMWARE="${1:-}"
ARCH="${2:-}"

if [ -z "$FIRMWARE" ]; then
    echo "[FIRMARBITER][firmae] ERROR: usage: run_adapter.sh <firmware_path> <architecture>" >&2
    exit 3
fi

if [ ! -f "$FIRMWARE" ]; then
    echo "[FIRMARBITER][firmae] ERROR: firmware not found: $FIRMWARE" >&2
    exit 3
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF_FILE="$SCRIPT_DIR/candidate.conf"
CONF_FIRMAE_DIR=""

if [ -f "$CONF_FILE" ]; then
    CONF_FIRMAE_DIR="$(awk -F= '
        /^[[:space:]]*tool_local_path[[:space:]]*=/ {
            value=$2
            sub(/^[[:space:]]+/, "", value)
            sub(/[[:space:]]+$/, "", value)
            print value
            exit
        }
    ' "$CONF_FILE")"
fi

FIRMAE_DIR="${FIRMAE_DIR:-${CONF_FIRMAE_DIR:-/opt/firmae}}"

if [ ! -d "$FIRMAE_DIR" ]; then
    echo "[FIRMARBITER][firmae] ERROR: FirmAE directory not found: $FIRMAE_DIR" >&2
    echo "[FIRMARBITER][firmae] Check tool_local_path in candidates/firmae/candidate.conf" >&2
    exit 2
fi

if [ ! -f "$FIRMAE_DIR/run.sh" ]; then
    echo "[FIRMARBITER][firmae] ERROR: run.sh not found in: $FIRMAE_DIR" >&2
    exit 2
fi

echo "[FIRMARBITER][firmae] Starting FirmAE on: $(basename "$FIRMWARE") ($ARCH)"
echo "[FIRMARBITER][firmae] FirmAE dir: $FIRMAE_DIR"

service postgresql start 2>&1 || true
sleep 2

sudo -u postgres psql -c \
    "CREATE USER firmadyne WITH PASSWORD 'firmadyne';" 2>/dev/null || true
sudo -u postgres psql -c \
    "CREATE DATABASE firmware OWNER firmadyne;" 2>/dev/null || true
sudo -u postgres psql -d firmware \
    -f "$FIRMAE_DIR/database/schema" 2>/dev/null || true

cd "$FIRMAE_DIR"

run_firmae() {
    local arch="$1"
    echo "[FIRMARBITER][firmae] Trying arch: $arch"

    sudo -u postgres psql -d firmware -c "DELETE FROM image;" 2>/dev/null || true
    sudo rm -rf "$FIRMAE_DIR/scratch"/* 2>/dev/null || true
    sudo rm -rf "$FIRMAE_DIR/images"/* 2>/dev/null || true

    sudo ./run.sh -c "$arch" "$FIRMWARE"
    return $?
}

if [ "$ARCH" != "unknown" ] && [ -n "$ARCH" ]; then
    echo "[FIRMARBITER][firmae] Using detected arch: $ARCH"
    run_firmae "$ARCH"
else
    echo "[FIRMARBITER][firmae] Arch unknown — trying arm, mips, mipsel in order"
    run_firmae "arm" || run_firmae "mips" || run_firmae "mipsel"
fi

EXIT_CODE=$?
echo "[FIRMARBITER][firmae] Exited with code: $EXIT_CODE"
exit $EXIT_CODE
