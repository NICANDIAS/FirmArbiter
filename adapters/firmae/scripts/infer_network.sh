#!/usr/bin/env bash

set -euo pipefail

cd /opt/firmae

WORK_DIR="/opt/firmae/scratch/$IID"
IMAGE_PATH="$WORK_DIR/image.raw"
MOUNT_PATH="$WORK_DIR/image"

cleanup() {
    set +e

    pkill -INT -f qemu-system-arm \
        >/dev/null 2>&1 || true

    sleep 2

    pkill -KILL -f qemu-system-arm \
        >/dev/null 2>&1 || true

    if mountpoint -q "$MOUNT_PATH"
    then
        umount -l "$MOUNT_PATH"
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

    chown -R \
        "$HOST_UID:$HOST_GID" \
        /opt/firmae/scratch \
        /veritas/evidence \
        >/dev/null 2>&1 || true
}

trap cleanup EXIT

test -s "$IMAGE_PATH"
test -s "$WORK_DIR/init"
test "$(cat "$WORK_DIR/architecture")" = "$ARCH"

echo "===== NETWORK INFERENCE ====="
echo "IID:          $IID"
echo "Architecture: $ARCH"
echo "Init candidates:"
cat "$WORK_DIR/init"

START_TIME="$(date -u +%s.%N)"

TIMEOUT=360 \
FIRMAE_NET=true \
FIRMAE_NVRAM=true \
FIRMAE_KERNEL=true \
FIRMAE_ETC=true \
USER=root \
python3 -u \
    ./scripts/makeNetwork.py \
    -i "$IID" \
    -q \
    -o \
    -a "$ARCH" \
    > /veritas/evidence/makeNetwork.log \
    2>&1

END_TIME="$(date -u +%s.%N)"

python3 - \
    "$START_TIME" \
    "$END_TIME" \
    > /veritas/evidence/network-duration.json <<'PY'
import json
import sys

start = float(sys.argv[1])
end = float(sys.argv[2])

print(json.dumps({
    "duration_seconds": end - start,
}, indent=2))
PY

test -s "$WORK_DIR/qemu.initial.serial.log"

if [ -s "$WORK_DIR/run.sh" ]
then
    cp \
        "$WORK_DIR/run.sh" \
        /veritas/evidence/generated-run.sh
fi

for filename in \
    architecture \
    init \
    current_init \
    ip_num \
    isDhcp \
    ip \
    ping \
    web \
    time_ping \
    time_web \
    qemu.initial.serial.log \
    emulation.log
do
    if [ -e "$WORK_DIR/$filename" ]
    then
        cp \
            "$WORK_DIR/$filename" \
            "/veritas/evidence/$filename"
    fi
done

for candidate in "$WORK_DIR"/ip.*
do
    if [ -f "$candidate" ]
    then
        cp \
            "$candidate" \
            "/veritas/evidence/$(basename "$candidate")"
    fi
done

find "$WORK_DIR" \
    -maxdepth 1 \
    -printf '%y\t%s\t%f\n' |
sort \
    > /veritas/evidence/work-files.txt

qemu-img info \
    --output=json \
    "$IMAGE_PATH" \
    > /veritas/evidence/image-after-info.json

sha256sum "$IMAGE_PATH" |
sed 's#  .*/image.raw#  image.raw#' \
    > /veritas/evidence/image-after.sha256

echo
echo "Network inference command completed"
