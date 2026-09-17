#!/usr/bin/env bash
set -euo pipefail

FIRMWARE="${1:-}"
ARCH="${2:-}"

if [ -z "$FIRMWARE" ] || [ -z "$ARCH" ]; then
    echo "[FIRMARBITER][emba] ERROR: usage: run_adapter.sh <firmware_path> <architecture>" >&2
    exit 3
fi

if [ ! -f "$FIRMWARE" ]; then
    echo "[FIRMARBITER][emba] ERROR: firmware not found: $FIRMWARE" >&2
    exit 3
fi

FIRMWARE_BASENAME=$(basename "$FIRMWARE" | sed 's/\.[^.]*$//')
LOG_DIR="/opt/emba/emba_logs/${FIRMWARE_BASENAME}_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$LOG_DIR"

echo "[FIRMARBITER][emba] Starting EMBA on: $(basename "$FIRMWARE") ($ARCH)"
echo "[FIRMARBITER][emba] Log dir: $LOG_DIR"

cd /opt/emba

# -D = developer mode (run on host without container protection)
# -F = ignore dependency errors  
# -Q = enable system emulation
sudo ./emba -f "$FIRMWARE" -l "$LOG_DIR" -D -F 2>&1

EXIT_CODE=$?
echo "[FIRMARBITER][emba] Exited with code: $EXIT_CODE"
exit $EXIT_CODE
