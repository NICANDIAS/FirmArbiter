#!/usr/bin/env bash

set -euo pipefail

FIRMAE_HOME="/opt/firmae"
BINARY_DIR="$FIRMAE_HOME/binaries"
PROVENANCE_DIR="/opt/veritas-adapter/provenance"

cd "$FIRMAE_HOME"

test -f "./download.sh"

mkdir -p \
    "$BINARY_DIR" \
    "$PROVENANCE_DIR"

echo "Downloading FirmAE v1.0 runtime assets..."

sh ./download.sh

required_assets=(
    "busybox.armel"
    "busybox.mipseb"
    "busybox.mipsel"

    "console.armel"
    "console.mipseb"
    "console.mipsel"

    "gdb.armel"
    "gdb.mipseb"
    "gdb.mipsel"

    "gdbserver.armel"
    "gdbserver.mipseb"
    "gdbserver.mipsel"

    "strace.armel"
    "strace.mipseb"
    "strace.mipsel"

    "libnvram.so.armel"
    "libnvram.so.mipseb"
    "libnvram.so.mipsel"

    "libnvram_ioctl.so.armel"
    "libnvram_ioctl.so.mipseb"
    "libnvram_ioctl.so.mipsel"

    "zImage.armel"
    "vmlinux.armel"

    "vmlinux.mipseb.2"
    "vmlinux.mipsel.2"
    "vmlinux.mipseb.4"
    "vmlinux.mipsel.4"
)

missing=0

for asset in "${required_assets[@]}"
do
    asset_path="$BINARY_DIR/$asset"

    if [ ! -s "$asset_path" ]
    then
        echo "Missing FirmAE asset: $asset" >&2
        missing=1
    fi
done

if [ "$missing" -ne 0 ]
then
    echo "One or more FirmAE assets are missing." >&2
    exit 1
fi

chmod 0755 \
    "$BINARY_DIR"/busybox.* \
    "$BINARY_DIR"/console.* \
    "$BINARY_DIR"/gdb.* \
    "$BINARY_DIR"/gdbserver.* \
    "$BINARY_DIR"/strace.*

(
    cd "$FIRMAE_HOME"

    for asset in "${required_assets[@]}"
    do
        sha256sum "binaries/$asset"
    done
) |
sort -k2 \
> "$PROVENANCE_DIR/firmae-release-assets.sha256"

echo "FirmAE runtime assets installed successfully."
echo "Asset count: ${#required_assets[@]}"
