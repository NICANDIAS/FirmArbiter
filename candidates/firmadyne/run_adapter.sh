#!/usr/bin/env bash
set -euo pipefail

FIRMWARE="${1:-}"
ARCH="${2:-}"

if [ -z "$FIRMWARE" ] || [ -z "$ARCH" ]; then
    echo "[VERITAS][firmadyne] ERROR: usage: run_adapter.sh <firmware_path> <architecture>" >&2
    exit 3
fi

if [ ! -f "$FIRMWARE" ]; then
    echo "[VERITAS][firmadyne] ERROR: firmware not found: $FIRMWARE" >&2
    exit 3
fi

echo "[VERITAS][firmadyne] Starting FIRMADYNE on: $(basename "$FIRMWARE") ($ARCH)"
echo "[VERITAS][firmadyne] Finding F2: scratch.sh removed, extractor.py removed"
echo "[VERITAS][firmadyne] Attempting with available scripts..."

cd /opt/firmadyne

# Try startup.sh if it exists
if [ -f "startup.sh" ]; then
    echo "[VERITAS][firmadyne] Found startup.sh — trying it"
    sudo bash startup.sh "$FIRMWARE" "$ARCH" 2>&1
elif [ -f "scripts/run.sh" ]; then
    echo "[VERITAS][firmadyne] No extractor available — cannot proceed"
    echo "[VERITAS][firmadyne] FIRMADYNE requires sources/extractor/extractor.py"
    echo "[VERITAS][firmadyne] which was removed from the repository"
    exit 1
fi

EXIT_CODE=$?
echo "[VERITAS][firmadyne] Exited with code: $EXIT_CODE"
exit $EXIT_CODE
