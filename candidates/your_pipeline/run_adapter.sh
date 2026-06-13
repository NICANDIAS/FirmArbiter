#!/usr/bin/env bash
# candidates/your_pipeline/run_adapter.sh
#
# Translates VERITAS's standard call into your firmware-security pipeline
# invocation.
#
# VERITAS always calls adapters like this:
#   bash run_adapter.sh <firmware_path> <architecture>
#
# Your pipeline's actual interface:
#   python fw_baseline.py --image <firmware_path> --arch <architecture>
#
# Adjust the command below to match however fw_baseline.py actually
# accepts its arguments as you develop it. You are the author of both
# sides here, so this adapter is the simplest of the four.
#
# Exit codes:
#   0  Pipeline completed and produced output
#   1  Pipeline reported an error
#   2  Pipeline was not found at the configured path
#   3  Wrong arguments

set -euo pipefail

FIRMWARE_PATH="${1:-}"
ARCHITECTURE="${2:-}"

if [ -z "$FIRMWARE_PATH" ] || [ -z "$ARCHITECTURE" ]; then
    echo "[VERITAS][your_pipeline] ERROR: usage: run_adapter.sh <firmware_path> <architecture>" >&2
    exit 3
fi

if [ ! -f "$FIRMWARE_PATH" ]; then
    echo "[VERITAS][your_pipeline] ERROR: firmware file not found: $FIRMWARE_PATH" >&2
    exit 3
fi

# Adjust this to wherever your firmware-security pipeline lives
PIPELINE_DIR="${PIPELINE_DIR:-/home/ubuntu/firmware-security}"

if [ ! -f "$PIPELINE_DIR/pipeline/orchestrator/fw_baseline.py" ]; then
    echo "[VERITAS][your_pipeline] ERROR: fw_baseline.py not found at $PIPELINE_DIR/pipeline/orchestrator/" >&2
    echo "[VERITAS][your_pipeline] Set the PIPELINE_DIR environment variable to the correct path." >&2
    exit 2
fi

echo "[VERITAS][your_pipeline] Starting your pipeline on: $(basename "$FIRMWARE_PATH") ($ARCHITECTURE)"
echo "[VERITAS][your_pipeline] Using pipeline at: $PIPELINE_DIR"

cd "$PIPELINE_DIR/pipeline/orchestrator"

python3 fw_baseline.py --image "$FIRMWARE_PATH" --arch "$ARCHITECTURE"
EXIT_CODE=$?

echo "[VERITAS][your_pipeline] Pipeline exited with code: $EXIT_CODE"
exit $EXIT_CODE
