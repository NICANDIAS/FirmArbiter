#!/usr/bin/env bash

set -Eeuo pipefail

cd /opt/firmae

WORK_DIR="/opt/firmae/scratch/$IID"
RUNNER="$WORK_DIR/run.sh"
IMAGE_PATH="$WORK_DIR/image.raw"
RUN_LOG="/veritas/evidence/generated-run.log"

STOP_REQUESTED=0
RUNNER_PID=""

handle_stop() {
    STOP_REQUESTED=1

    echo "Persistent FirmAE shutdown requested"

    pkill -INT -f qemu-system-arm \
        >/dev/null 2>&1 || true

    for _ in $(seq 1 20)
    do
        if ! pgrep -f qemu-system-arm \
            >/dev/null 2>&1
        then
            return 0
        fi

        sleep 1
    done

    pkill -TERM -f qemu-system-arm \
        >/dev/null 2>&1 || true
}

final_cleanup() {
    set +e

    pkill -KILL -f qemu-system-arm \
        >/dev/null 2>&1 || true

    if mountpoint -q "$WORK_DIR/image"
    then
        umount -l "$WORK_DIR/image"
    fi

    losetup -j "$IMAGE_PATH" |
    cut -d: -f1 |
    while read -r loop_device
    do
        if [ -n "$loop_device" ]
        then
            losetup --detach "$loop_device" \
                >/dev/null 2>&1 || true
        fi
    done

    ip -o link show |
    awk -F': ' '{print $2}' |
    sed 's/@.*//' |
    grep -E "^tap${IID}_" |
    while read -r interface
    do
        ip link delete "$interface" \
            >/dev/null 2>&1 ||
        tunctl -d "$interface" \
            >/dev/null 2>&1 ||
        true
    done

    chown -R \
        "$HOST_UID:$HOST_GID" \
        "$WORK_DIR" \
        /veritas/evidence \
        >/dev/null 2>&1 || true
}

trap handle_stop TERM INT
trap final_cleanup EXIT

test -x "$RUNNER"
test -s "$IMAGE_PATH"

rm -f \
    "$WORK_DIR/qemu.final.serial.log" \
    "$RUN_LOG"

echo "Starting generated FirmAE runner"

"$RUNNER" > "$RUN_LOG" 2>&1 &
RUNNER_PID=$!

printf '%s\n' "$RUNNER_PID" \
    > /veritas/evidence/generated-runner.pid

set +e
wait "$RUNNER_PID"
RUNNER_EXIT_CODE=$?
set -e

if [ "$STOP_REQUESTED" -eq 1 ]
then
    RUNNER_EXIT_CODE=0
fi

printf '%s\n' "$RUNNER_EXIT_CODE" \
    > /veritas/evidence/generated-runner-exit-code.txt

echo "Generated FirmAE runner exited: $RUNNER_EXIT_CODE"

exit "$RUNNER_EXIT_CODE"
