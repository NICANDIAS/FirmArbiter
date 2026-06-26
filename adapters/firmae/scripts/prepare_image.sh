#!/usr/bin/env bash

set -euo pipefail

cd /opt/firmae

unset FIRMAE_DOCKER || true

WORK_DIR="/opt/firmae/scratch/$IID"
IMAGE_PATH="$WORK_DIR/image.raw"
MOUNT_PATH="$WORK_DIR/image"

cleanup() {
    set +e

    if mountpoint -q "$MOUNT_PATH"
    then
        umount -l "$MOUNT_PATH"
    fi

    if [ -e "$IMAGE_PATH" ]
    then
        losetup -j "$IMAGE_PATH" |
        cut -d: -f1 |
        while read -r loop_device
        do
            if [ -n "$loop_device" ]
            then
                losetup --detach "$loop_device"
            fi
        done
    fi

    veritas-firmae-database stop \
        >/dev/null 2>&1 || true

    chown -R \
        "$HOST_UID:$HOST_GID" \
        /opt/firmae/scratch \
        /veritas/evidence \
        2>/dev/null || true
}

trap cleanup EXIT

echo "===== DATABASE INITIALISATION ====="

veritas-firmae-database initialize \
    > /veritas/evidence/database.log \
    2>&1

export PGPASSWORD=firmadyne

echo "===== RESTORE FIRMAE IMAGE RECORD ====="

psql \
    --host 127.0.0.1 \
    --username firmadyne \
    --dbname firmware \
    --set ON_ERROR_STOP=1 \
    --variable iid="$IID" \
    --variable arch="$ARCH" \
    --variable firmware_sha="$FIRMWARE_SHA" \
    --variable firmware_name="$FIRMWARE_NAME" \
    --variable brand_name="$BRAND_NAME" \
    > /veritas/evidence/database-seed.log \
    2>&1 <<'SQL'
INSERT INTO brand (name)
VALUES (:'brand_name')
ON CONFLICT (name) DO NOTHING;

INSERT INTO image (
    id,
    filename,
    description,
    brand_id,
    hash,
    rootfs_extracted,
    kernel_extracted,
    arch
)
VALUES (
    :iid,
    :'firmware_name',
    'VERITAS reconstructed FirmAE image record',
    (
        SELECT id
        FROM brand
        WHERE name = :'brand_name'
    ),
    :'firmware_sha',
    TRUE,
    FALSE,
    :'arch'
)
ON CONFLICT (id)
DO UPDATE SET
    filename = EXCLUDED.filename,
    description = EXCLUDED.description,
    brand_id = EXCLUDED.brand_id,
    hash = EXCLUDED.hash,
    rootfs_extracted = EXCLUDED.rootfs_extracted,
    kernel_extracted = EXCLUDED.kernel_extracted,
    arch = EXCLUDED.arch;

SELECT setval(
    'image_id_seq',
    GREATEST(
        COALESCE(
            (
                SELECT MAX(id)
                FROM image
            ),
            1
        ),
        1
    ),
    TRUE
);
SQL

psql \
    --host 127.0.0.1 \
    --username firmadyne \
    --dbname firmware \
    --tuples-only \
    --no-align \
    --command "
        SELECT id, filename, arch, rootfs_extracted
        FROM image
        WHERE id = $IID;
    " \
    > /veritas/evidence/database-image-record.txt

echo "Database image record:"
cat /veritas/evidence/database-image-record.txt

grep -q "^${IID}|" \
    /veritas/evidence/database-image-record.txt

mkdir -p \
    /opt/firmae/images \
    "$WORK_DIR"

cp \
    /veritas/artifacts/rootfs.tar.gz \
    "/opt/firmae/images/$IID.tar.gz"

printf '%s\n' "$ARCH" \
    > "$WORK_DIR/architecture"

printf '%s\n' "$FIRMWARE_NAME" \
    > "$WORK_DIR/name"

printf '%s\n' "$BRAND_NAME" \
    > "$WORK_DIR/brand"

echo "===== DATABASE INDEXING ====="

START_TAR="$(date -u +%s.%N)"

python3 -u \
    ./scripts/tar2db.py \
    -i "$IID" \
    -f "./images/$IID.tar.gz" \
    -h 127.0.0.1 \
    > /veritas/evidence/tar2db.log \
    2>&1

END_TAR="$(date -u +%s.%N)"

python3 - "$START_TAR" "$END_TAR" \
    > /veritas/evidence/tar2db-duration.json <<'PY'
import json
import sys

start = float(sys.argv[1])
end = float(sys.argv[2])

print(json.dumps({
    "duration_seconds": end - start
}, indent=2))
PY

psql \
    --host 127.0.0.1 \
    --username firmadyne \
    --dbname firmware \
    --tuples-only \
    --no-align \
    --command "
        SELECT COUNT(*)
        FROM object_to_image
        WHERE iid = $IID;
    " \
    > /veritas/evidence/indexed-object-count.txt

INDEXED_COUNT="$(
    tr -d '[:space:]' \
        < /veritas/evidence/indexed-object-count.txt
)"

echo "Indexed rootfs objects: $INDEXED_COUNT"

if [ -z "$INDEXED_COUNT" ] ||
   [ "$INDEXED_COUNT" -le 0 ]
then
    echo "No rootfs objects were indexed." >&2
    exit 1
fi

echo "===== QEMU IMAGE CREATION ====="

START_IMAGE="$(date -u +%s.%N)"

./scripts/makeImage.sh \
    "$IID" \
    "$ARCH" \
    "$FIRMWARE_NAME" \
    > /veritas/evidence/makeImage.log \
    2>&1

END_IMAGE="$(date -u +%s.%N)"

python3 - "$START_IMAGE" "$END_IMAGE" \
    > /veritas/evidence/make-image-duration.json <<'PY'
import json
import sys

start = float(sys.argv[1])
end = float(sys.argv[2])

print(json.dumps({
    "duration_seconds": end - start
}, indent=2))
PY

test -s "$IMAGE_PATH"

qemu-img info \
    --output=json \
    "$IMAGE_PATH" \
    > /veritas/evidence/qemu-image-info.json

fdisk -l "$IMAGE_PATH" \
    > /veritas/evidence/partition-table.txt

stat \
    --printf='size_bytes=%s\nblocks=%b\nblock_size=%B\n' \
    "$IMAGE_PATH" \
    > /veritas/evidence/image-stat.txt

find "$WORK_DIR" \
    -maxdepth 2 \
    -printf '%y\t%s\t%p\n' \
    > /veritas/evidence/work-directory.txt

chown -R \
    "$HOST_UID:$HOST_GID" \
    /opt/firmae/scratch \
    /veritas/evidence

echo
echo "QEMU filesystem image preparation passed"
echo "Image: $IMAGE_PATH"
