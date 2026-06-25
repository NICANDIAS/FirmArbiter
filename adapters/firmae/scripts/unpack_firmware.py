#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


FIRMAE_ROOT = Path("/opt/firmae")
EXTRACTOR = FIRMAE_ROOT / "sources/extractor/extractor.py"
UTILITY = FIRMAE_ROOT / "scripts/util.py"
IMAGE_DIRECTORY = FIRMAE_ROOT / "images"
POSTGRESQL_HOST = "127.0.0.1"
EXTRACTION_TIMEOUT_SECONDS = 300


class UnpackError(RuntimeError):
    pass


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as handle:
        for block in iter(
            lambda: handle.read(1024 * 1024),
            b"",
        ):
            digest.update(block)

    return digest.hexdigest()


def run_extractor(
    *,
    firmware_path: Path,
    brand: str,
    disable_flag: str,
    log_path: Path,
) -> float:
    command = [
        sys.executable,
        str(EXTRACTOR),
        "-b",
        brand,
        "-sql",
        POSTGRESQL_HOST,
        "-np",
        disable_flag,
        str(firmware_path),
        str(IMAGE_DIRECTORY),
    ]

    started = time.monotonic()

    with log_path.open(
        "w",
        encoding="utf-8",
    ) as log_file:
        log_file.write(
            "Command: "
            + " ".join(command)
            + "\n\n"
        )
        log_file.flush()

        try:
            completed = subprocess.run(
                command,
                cwd=FIRMAE_ROOT,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=EXTRACTION_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise UnpackError(
                "FirmAE extraction exceeded "
                f"{EXTRACTION_TIMEOUT_SECONDS} seconds"
            ) from exc

    elapsed = time.monotonic() - started

    if completed.returncode != 0:
        raise UnpackError(
            "FirmAE extractor returned "
            f"{completed.returncode}; inspect {log_path.name}"
        )

    return elapsed


def obtain_image_id(
    firmware_path: Path,
) -> str:
    completed = subprocess.run(
        [
            sys.executable,
            str(UTILITY),
            "get_iid",
            str(firmware_path),
            POSTGRESQL_HOST,
        ],
        cwd=FIRMAE_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    image_id = completed.stdout.strip()

    if completed.returncode != 0 or not image_id:
        raise UnpackError(
            "FirmAE did not record an internal image ID"
        )

    if not image_id.isdigit():
        raise UnpackError(
            f"FirmAE returned invalid image ID: {image_id!r}"
        )

    return image_id


def verify_rootfs_archive(path: Path) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise UnpackError(
            "FirmAE did not produce a root filesystem archive"
        )

    try:
        with tarfile.open(path, "r:gz") as archive:
            first_member = next(iter(archive), None)
    except (tarfile.TarError, OSError) as exc:
        raise UnpackError(
            "FirmAE produced an invalid root filesystem archive"
        ) from exc

    if first_member is None:
        raise UnpackError(
            "FirmAE produced an empty root filesystem archive"
        )


def write_json(
    path: Path,
    document: dict[str, Any],
) -> None:
    path.write_text(
        json.dumps(
            document,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run FirmAE extraction for VERITAS"
    )

    parser.add_argument(
        "--firmware",
        required=True,
        type=Path,
    )
    parser.add_argument(
        "--artifacts",
        required=True,
        type=Path,
    )
    parser.add_argument(
        "--workspace",
        required=True,
        type=Path,
    )
    parser.add_argument(
        "--brand",
        required=True,
    )
    parser.add_argument(
        "--case-id",
        required=True,
    )

    arguments = parser.parse_args()

    firmware_path = arguments.firmware.resolve()
    artifacts_path = arguments.artifacts.resolve()
    workspace_path = arguments.workspace.resolve()
    brand = arguments.brand.strip() or "unknown"

    artifacts_path.mkdir(
        parents=True,
        exist_ok=True,
    )
    workspace_path.mkdir(
        parents=True,
        exist_ok=True,
    )
    IMAGE_DIRECTORY.mkdir(
        parents=True,
        exist_ok=True,
    )

    rootfs_log = (
        artifacts_path / "rootfs-extractor.log"
    )
    kernel_log = (
        artifacts_path / "kernel-extractor.log"
    )
    error_path = (
        artifacts_path / "unpack-error.json"
    )

    try:
        if not firmware_path.is_file():
            raise UnpackError(
                f"Firmware does not exist: {firmware_path}"
            )

        if not EXTRACTOR.is_file():
            raise UnpackError(
                f"FirmAE extractor is missing: {EXTRACTOR}"
            )

        started_at = utc_now()

        rootfs_seconds = run_extractor(
            firmware_path=firmware_path,
            brand=brand,
            disable_flag="-nk",
            log_path=rootfs_log,
        )

        image_id = obtain_image_id(
            firmware_path
        )

        kernel_seconds = run_extractor(
            firmware_path=firmware_path,
            brand=brand,
            disable_flag="-nf",
            log_path=kernel_log,
        )

        source_rootfs = (
            IMAGE_DIRECTORY / f"{image_id}.tar.gz"
        )
        source_kernel = (
            IMAGE_DIRECTORY / f"{image_id}.kernel"
        )

        verify_rootfs_archive(source_rootfs)

        artifact_rootfs = (
            artifacts_path / "rootfs.tar.gz"
        )

        shutil.copyfile(
            source_rootfs,
            artifact_rootfs,
        )

        kernel_document: dict[str, Any] = {
            "present": False,
        }

        if (
            source_kernel.is_file()
            and source_kernel.stat().st_size > 0
        ):
            artifact_kernel = (
                artifacts_path / "kernel.bin"
            )

            shutil.copyfile(
                source_kernel,
                artifact_kernel,
            )

            kernel_document = {
                "present": True,
                "path": "kernel.bin",
                "size_bytes": (
                    artifact_kernel.stat().st_size
                ),
                "sha256": sha256_file(
                    artifact_kernel
                ),
            }

        metadata = {
            "schema_version": "1.0",
            "adapter_id": "firmae",
            "operation": "unpack",
            "case_id": arguments.case_id,
            "brand": brand,
            "firmae_image_id": int(image_id),
            "started_at": started_at,
            "completed_at": utc_now(),
            "source_firmware": {
                "size_bytes": (
                    firmware_path.stat().st_size
                ),
                "sha256": sha256_file(
                    firmware_path
                ),
            },
            "rootfs": {
                "path": "rootfs.tar.gz",
                "size_bytes": (
                    artifact_rootfs.stat().st_size
                ),
                "sha256": sha256_file(
                    artifact_rootfs
                ),
            },
            "kernel": kernel_document,
            "timings_seconds": {
                "rootfs_extraction": rootfs_seconds,
                "kernel_extraction": kernel_seconds,
            },
            "logs": [
                "rootfs-extractor.log",
                "kernel-extractor.log",
            ],
        }

        write_json(
            artifacts_path / "unpack-metadata.json",
            metadata,
        )

        error_path.unlink(missing_ok=True)

        print(
            json.dumps(
                {
                    "status": "completed",
                    "firmae_image_id": int(
                        image_id
                    ),
                    "rootfs": str(
                        artifact_rootfs
                    ),
                    "kernel_present": (
                        kernel_document["present"]
                    ),
                },
                sort_keys=True,
            )
        )

        return 0

    except Exception as exc:
        write_json(
            error_path,
            {
                "schema_version": "1.0",
                "adapter_id": "firmae",
                "operation": "unpack",
                "case_id": arguments.case_id,
                "timestamp": utc_now(),
                "error_type": type(exc).__name__,
                "message": str(exc),
            },
        )

        print(
            f"FirmAE unpack failed: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )

        return 1


if __name__ == "__main__":
    raise SystemExit(main())
