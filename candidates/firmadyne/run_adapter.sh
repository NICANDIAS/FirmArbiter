#!/usr/bin/env bash
set -euo pipefail

FIRMWARE="${1:-}"
ARCH="${2:-}"

if [ -z "$FIRMWARE" ] || [ -z "$ARCH" ]; then
    echo "[FIRMARBITER][firmadyne] ERROR: usage: run_adapter.sh <firmware_path> <architecture>" >&2
    exit 3
fi

if [ ! -f "$FIRMWARE" ]; then
    echo "[FIRMARBITER][firmadyne] ERROR: firmware not found: $FIRMWARE" >&2
    exit 3
fi

echo "[FIRMARBITER][firmadyne] Starting FIRMADYNE on: $(basename "$FIRMWARE") ($ARCH)"
echo "[FIRMARBITER][firmadyne] Finding F2: scratch.sh removed, extractor.py removed"
echo "[FIRMARBITER][firmadyne] Attempting with available scripts..."

cd /opt/firmadyne

# Try startup.sh if it exists
if [ -f "startup.sh" ]; then
    echo "[FIRMARBITER][firmadyne] Found startup.sh — trying it"
    sudo bash startup.sh "$FIRMWARE" "$ARCH" 2>&1
elif [ -f "scripts/run.sh" ]; then
    echo "[FIRMARBITER][firmadyne] No extractor available — cannot proceed"
    echo "[FIRMARBITER][firmadyne] FIRMADYNE requires sources/extractor/extractor.py"
    echo "[FIRMARBITER][firmadyne] which was removed from the repository"
    exit 1
fi

EXIT_CODE=$?
echo "[FIRMARBITER][firmadyne] Exited with code: $EXIT_CODE"
exit $EXIT_CODE
