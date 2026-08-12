#!/usr/bin/env bash

set -euo pipefail

LOCK_FILE="${1:?python dependency lock path required}"
PROVENANCE="/opt/firmarbiter-adapter/provenance"
DOWNLOAD_DIR="$(mktemp -d)"

cleanup() {
    rm -rf "$DOWNLOAD_DIR"
}
trap cleanup EXIT

test -s "$LOCK_FILE"
mkdir -p "$PROVENANCE" /opt/firmarbiter-adapter/locks

install \
    -o root \
    -g root \
    -m 0644 \
    "$LOCK_FILE" \
    /opt/firmarbiter-adapter/locks/python-source-lock.tsv

{
    IFS=$'\t' read -r header_name header_version header_url header_sha256

    if [ "$header_name" != "name" ] || \
       [ "$header_version" != "version" ] || \
       [ "$header_url" != "url" ] || \
       [ "$header_sha256" != "sha256" ]
    then
        echo "Invalid Python source lock header." >&2
        exit 1
    fi

    while IFS=$'\t' read -r name version url expected_sha256
    do
        [ -n "$name" ] || continue
        [ -n "$version" ] || {
            echo "Missing version for $name" >&2
            exit 1
        }
        [ -n "$url" ] || {
            echo "Missing URL for $name" >&2
            exit 1
        }
        case "$expected_sha256" in
            [0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]* ) ;;
            * )
                echo "Invalid SHA-256 for $name" >&2
                exit 1
                ;;
        esac

        archive="$DOWNLOAD_DIR/${name}-${version}.tar.gz"

        curl \
            --fail \
            --location \
            --retry 5 \
            --retry-all-errors \
            --output "$archive" \
            "$url"

        printf '%s  %s\n' \
            "$expected_sha256" \
            "$archive" | sha256sum --check --strict -

        python3 -m pip install \
            --disable-pip-version-check \
            --no-cache-dir \
            --no-deps \
            "$archive"
    done
} < "$LOCK_FILE"

python3 - <<'PY'
from importlib import metadata
import json
from pathlib import Path

import lzo
import ubireader

version = metadata.version("ubi-reader")
if version != "0.8.5":
    raise SystemExit(
        f"Unexpected ubi-reader version: {version}"
    )

Path("/opt/firmarbiter-adapter/provenance/python-dependencies.json").write_text(
    json.dumps(
        {
            "ubi-reader": version,
            "lzo_module": getattr(lzo, "__file__", None),
            "ubireader_module": getattr(ubireader, "__file__", None),
        },
        indent=2,
        sort_keys=True,
    ) + "\n",
    encoding="utf-8",
)
PY

command -v ubireader_extract_files \
    > "$PROVENANCE/ubireader-extract-files.path"

sha256sum "$(command -v ubireader_extract_files)" \
    > "$PROVENANCE/ubireader-extract-files.sha256"

sha256sum \
    /opt/firmarbiter-adapter/locks/python-source-lock.tsv \
    "$PROVENANCE/python-dependencies.json" \
    "$PROVENANCE/ubireader-extract-files.path" \
    "$PROVENANCE/ubireader-extract-files.sha256" \
    > "$PROVENANCE/python-dependency-files.sha256"

echo "FirmAE pinned Python dependency installation completed."
