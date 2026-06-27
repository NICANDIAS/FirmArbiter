#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable


ADAPTER_ID = "firmae"
SCHEMA_VERSION = "1.0"
CONTRACT_VERSION = "1.0"

DATABASE_CONTROLLER = Path(
    "/usr/local/bin/veritas-firmae-database"
)

DEPENDENCY_DOCTOR = Path(
    "/usr/local/bin/veritas-firmae-dependency-doctor"
)

FIRMAE_DATABASE_PORT = os.environ.get(
    "VERITAS_FIRMAE_PGPORT",
    "55432",
)

# FirmAE helper scripts invoke psql directly. Export the private
# database connection so every child process uses the same isolated
# PostgreSQL endpoint instead of the host PostgreSQL on port 5432.
os.environ["VERITAS_FIRMAE_PGPORT"] = (
    FIRMAE_DATABASE_PORT
)
os.environ["PGHOST"] = "127.0.0.1"
os.environ["PGPORT"] = FIRMAE_DATABASE_PORT

PREPARE_IMAGE_HELPER = Path(
    "/usr/local/bin/veritas-firmae-prepare-image"
)
INFER_NETWORK_HELPER = Path(
    "/usr/local/bin/veritas-firmae-infer-network"
)
PERSISTENT_RUNTIME_HELPER = Path(
    "/usr/local/bin/veritas-firmae-run-persistent"
)

FIRMAE_HOME = Path("/opt/firmae")
FIRMAE_IMAGES = FIRMAE_HOME / "images"
FIRMAE_SCRATCH = FIRMAE_HOME / "scratch"
RUNTIME_EVIDENCE_ALIAS = Path("/veritas/evidence")

FULL_EXECUTION_STAGES = {
    "unpack",
    "emulate",
    "endpoint-discovery",
}

COMMON_ENDPOINTS = (
    (22, "ssh"),
    (23, "telnet"),
    (53, "tcp"),
    (80, "http"),
    (443, "https"),
    (8080, "http"),
    (8443, "https"),
)

RUN_ID_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
)

SUPPORTED_STAGES = {
    "unpack",
    "emulate",
    "endpoint-discovery",
}


class ContractError(RuntimeError):
    """The supplied run request violates the adapter contract."""


class AdapterOperationalError(RuntimeError):
    """A structured candidate or adapter execution failure."""

    def __init__(
        self,
        code: str,
        phase: str,
        message: str,
        *,
        recoverable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.phase = phase
        self.recoverable = recoverable


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def map_contract_path(contract_path: str) -> Path:
    """
    Use /veritas paths directly in containers.

    VERITAS_CONTRACT_ROOT supports host-side contract tests.
    """
    contract_root = os.environ.get("VERITAS_CONTRACT_ROOT")

    if not contract_root:
        return Path(contract_path)

    path = PurePosixPath(contract_path)

    try:
        relative = path.relative_to("/veritas")
    except ValueError as exc:
        raise ContractError(
            f"Contract path is outside /veritas: {contract_path}"
        ) from exc

    return Path(contract_root).joinpath(*relative.parts)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as file_handle:
        for block in iter(
            lambda: file_handle.read(1024 * 1024),
            b"",
        ):
            digest.update(block)

    return digest.hexdigest()


def require_object(
    value: Any,
    name: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{name} must be an object")

    return value


def require_string(
    value: Any,
    name: str,
) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError(
            f"{name} must be a non-empty string"
        )

    return value


def load_and_validate_request(
    request_path: Path,
) -> dict[str, Any]:
    try:
        request = json.loads(
            request_path.read_text(encoding="utf-8")
        )
    except FileNotFoundError as exc:
        raise ContractError(
            f"Request file does not exist: {request_path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ContractError(
            f"Request is not valid JSON: {exc}"
        ) from exc

    request = require_object(request, "request")

    if request.get("schema_version") != SCHEMA_VERSION:
        raise ContractError(
            "Unsupported request schema_version"
        )

    if request.get("contract_version") != CONTRACT_VERSION:
        raise ContractError(
            "Unsupported request contract_version"
        )

    run = require_object(request.get("run"), "run")
    firmware = require_object(
        request.get("firmware"),
        "firmware",
    )
    lifecycle = require_object(
        request.get("lifecycle"),
        "lifecycle",
    )
    paths = require_object(request.get("paths"), "paths")

    run_id = require_string(
        run.get("run_id"),
        "run.run_id",
    )

    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise ContractError(
            f"Invalid run.run_id: {run_id}"
        )

    adapter_id = require_string(
        run.get("adapter_id"),
        "run.adapter_id",
    )

    if adapter_id != ADAPTER_ID:
        raise ContractError(
            f"Request adapter_id must be {ADAPTER_ID!r}, "
            f"not {adapter_id!r}"
        )

    requested_stages = run.get("requested_stages")

    if (
        not isinstance(requested_stages, list)
        or not requested_stages
        or not all(
            isinstance(stage, str)
            for stage in requested_stages
        )
    ):
        raise ContractError(
            "run.requested_stages must be a non-empty "
            "string array"
        )

    unsupported_stages = (
        set(requested_stages) - SUPPORTED_STAGES
    )

    if unsupported_stages:
        unsupported = ", ".join(
            sorted(unsupported_stages)
        )
        raise ContractError(
            f"FirmAE does not support requested stages: "
            f"{unsupported}"
        )

    expected_paths = {
        "workspace": "/veritas/work",
        "artifacts": "/veritas/artifacts",
        "events": "/veritas/events/events.jsonl",
        "control": "/veritas/control",
    }

    for name, expected in expected_paths.items():
        actual = paths.get(name)

        if actual != expected:
            raise ContractError(
                f"paths.{name} must be {expected!r}, "
                f"not {actual!r}"
            )

    firmware_contract_path = firmware.get("path")

    if firmware_contract_path != "/veritas/input/firmware":
        raise ContractError(
            "firmware.path must be "
            "'/veritas/input/firmware'"
        )

    firmware_path = map_contract_path(
        firmware_contract_path
    )

    if not firmware_path.is_file():
        raise ContractError(
            f"Firmware input does not exist: "
            f"{firmware_path}"
        )

    expected_size = firmware.get("size_bytes")

    if (
        not isinstance(expected_size, int)
        or expected_size < 0
    ):
        raise ContractError(
            "firmware.size_bytes must be a "
            "non-negative integer"
        )

    actual_size = firmware_path.stat().st_size

    if actual_size != expected_size:
        raise ContractError(
            "Firmware size mismatch: "
            f"expected {expected_size}, got {actual_size}"
        )

    expected_hash = require_string(
        firmware.get("sha256"),
        "firmware.sha256",
    )

    actual_hash = sha256_file(firmware_path)

    if actual_hash != expected_hash:
        raise ContractError(
            "Firmware SHA-256 mismatch: "
            f"expected {expected_hash}, got {actual_hash}"
        )

    heartbeat_interval = lifecycle.get(
        "heartbeat_interval_seconds"
    )

    if (
        not isinstance(heartbeat_interval, int)
        or heartbeat_interval < 1
    ):
        raise ContractError(
            "lifecycle.heartbeat_interval_seconds "
            "must be a positive integer"
        )

    boot_wait_timeout = lifecycle.get(
        "boot_wait_timeout_seconds"
    )

    if (
        boot_wait_timeout is not None
        and (
            not isinstance(
                boot_wait_timeout,
                (int, float),
            )
            or isinstance(boot_wait_timeout, bool)
            or boot_wait_timeout <= 0
        )
    ):
        raise ContractError(
            "lifecycle.boot_wait_timeout_seconds "
            "must be a positive number"
        )

    endpoint_wait_timeout = lifecycle.get(
        "endpoint_wait_timeout_seconds"
    )

    if (
        endpoint_wait_timeout is not None
        and (
            not isinstance(
                endpoint_wait_timeout,
                (int, float),
            )
            or isinstance(endpoint_wait_timeout, bool)
            or endpoint_wait_timeout <= 0
        )
    ):
        raise ContractError(
            "lifecycle.endpoint_wait_timeout_seconds "
            "must be a positive number"
        )

    return request


class EventWriter:
    def __init__(
        self,
        event_path: Path,
        run_id: str,
        adapter_id: str,
    ) -> None:
        event_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        self._file = event_path.open(
            "a",
            encoding="utf-8",
            buffering=1,
        )
        self._run_id = run_id
        self._adapter_id = adapter_id
        self._sequence = 0
        self._lock = threading.Lock()
        self._closed = False

    def emit(
        self,
        event_name: str,
        **additional_fields: Any,
    ) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError(
                    "Cannot emit after event writer closed"
                )

            self._sequence += 1

            document = {
                "schema_version": SCHEMA_VERSION,
                "contract_version": CONTRACT_VERSION,
                "event": event_name,
                "sequence": self._sequence,
                "timestamp": utc_now(),
                "run_id": self._run_id,
                "adapter_id": self._adapter_id,
                **additional_fields,
            }

            self._file.write(
                json.dumps(
                    document,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )
            self._file.flush()
            os.fsync(self._file.fileno())

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return

            self._file.flush()
            os.fsync(self._file.fileno())
            self._file.close()
            self._closed = True


class HeartbeatWorker:
    def __init__(
        self,
        event_writer: EventWriter,
        interval_seconds: float,
        state_getter: Callable[[], str],
    ) -> None:
        self._event_writer = event_writer
        self._interval_seconds = max(
            0.1,
            interval_seconds,
        )
        self._state_getter = state_getter
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="firmae-heartbeat",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        next_heartbeat = time.monotonic()

        while not self._stop_event.is_set():
            current_time = time.monotonic()

            if current_time >= next_heartbeat:
                self._event_writer.emit(
                    "heartbeat",
                    state=self._state_getter(),
                )
                next_heartbeat = (
                    current_time + self._interval_seconds
                )

            self._stop_event.wait(0.05)


def run_dependency_doctor(
    *,
    artifacts_path: Path,
) -> None:
    report_path = artifacts_path / "dependency-check.json"

    artifacts_path.mkdir(
        parents=True,
        exist_ok=True,
    )

    completed = subprocess.run(
        [
            str(DEPENDENCY_DOCTOR),
            "--output",
            str(report_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    if completed.returncode == 0:
        return

    detail = completed.stderr.strip()

    if report_path.is_file():
        try:
            report = read_json_object(report_path)
        except Exception:
            report = {}

        missing = []

        for key in (
            "missing_commands",
            "missing_files",
            "missing_python_imports",
        ):
            values = report.get(key, [])

            if isinstance(values, list):
                missing.extend(
                    str(value)
                    for value in values
                )

        if missing:
            detail = "missing: " + ", ".join(
                sorted(set(missing))
            )

    raise AdapterOperationalError(
        "FIRMAE_DEPENDENCY_CHECK_FAILED",
        "candidate_setup",
        (
            "FirmAE runtime dependency validation failed"
            + (f" ({detail})" if detail else "")
            + "; inspect /veritas/artifacts/dependency-check.json"
        ),
    )


def run_database_command(action: str) -> None:
    if not DATABASE_CONTROLLER.is_file():
        raise AdapterOperationalError(
            "FIRMAE_DATABASE_CONTROLLER_MISSING",
            "adapter_setup",
            (
                "FirmAE database controller is missing: "
                f"{DATABASE_CONTROLLER}"
            ),
        )

    completed = subprocess.run(
        [str(DATABASE_CONTROLLER), action],
        check=False,
        text=True,
        stdout=sys.stderr,
        stderr=sys.stderr,
    )

    if completed.returncode != 0:
        raise AdapterOperationalError(
            "FIRMAE_DATABASE_OPERATION_FAILED",
            (
                "cleanup"
                if action == "stop"
                else "adapter_setup"
            ),
            (
                f"FirmAE database action {action!r} "
                f"returned {completed.returncode}"
            ),
        )




class ConsoleMirror:
    """Continuously export the persistent guest serial console."""

    def __init__(
        self,
        *,
        source_path: Path,
        destination_path: Path,
    ) -> None:
        self.source_path = source_path
        self.destination_path = destination_path
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="firmae-console-mirror",
            daemon=True,
        )

    def start(self) -> None:
        self.destination_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        self.destination_path.write_bytes(b"")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=10)
        self._copy_available()

    def _copy_available(self) -> None:
        if not self.source_path.is_file():
            return

        destination_size = (
            self.destination_path.stat().st_size
            if self.destination_path.is_file()
            else 0
        )
        source_size = self.source_path.stat().st_size

        if source_size < destination_size:
            self.destination_path.write_bytes(b"")
            destination_size = 0

        if source_size == destination_size:
            return

        with self.source_path.open("rb") as source:
            source.seek(destination_size)

            with self.destination_path.open("ab") as destination:
                shutil.copyfileobj(
                    source,
                    destination,
                    length=1024 * 1024,
                )
                destination.flush()
                os.fsync(destination.fileno())

    def _run(self) -> None:
        while not self._stop_event.wait(0.25):
            try:
                self._copy_available()
            except OSError:
                continue


class RuntimeController:
    """Own the persistent FirmAE/QEMU subprocess."""

    def __init__(
        self,
        process: subprocess.Popen[str],
        log_handle: Any,
        log_path: Path,
        console_mirror: ConsoleMirror,
    ) -> None:
        self._process = process
        self._log_handle = log_handle
        self.log_path = log_path
        self._console_mirror = console_mirror
        self._closed = False

    @classmethod
    def start(
        cls,
        *,
        environment: dict[str, str],
        log_path: Path,
        console_source_path: Path,
        console_export_path: Path,
    ) -> "RuntimeController":
        if not PERSISTENT_RUNTIME_HELPER.is_file():
            raise AdapterOperationalError(
                "FIRMAE_RUNTIME_HELPER_MISSING",
                "candidate_setup",
                (
                    "FirmAE persistent runtime helper is missing: "
                    f"{PERSISTENT_RUNTIME_HELPER}"
                ),
            )

        log_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        log_handle = log_path.open(
            "w",
            encoding="utf-8",
            buffering=1,
        )

        console_mirror = ConsoleMirror(
            source_path=console_source_path,
            destination_path=console_export_path,
        )
        console_mirror.start()

        try:
            process = subprocess.Popen(
                [str(PERSISTENT_RUNTIME_HELPER)],
                text=True,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                env=environment,
            )
        except Exception:
            console_mirror.stop()
            log_handle.close()
            raise

        return cls(
            process=process,
            log_handle=log_handle,
            log_path=log_path,
            console_mirror=console_mirror,
        )

    @property
    def pid(self) -> int:
        return self._process.pid

    def poll(self) -> int | None:
        return self._process.poll()

    def _close_log(self) -> None:
        if self._closed:
            return

        self._log_handle.flush()
        self._log_handle.close()
        self._closed = True

    def record_unexpected_exit(self) -> int:
        return_code = self._process.wait()
        self._console_mirror.stop()
        self._close_log()
        return return_code

    def stop(
        self,
        grace_seconds: float,
    ) -> None:
        return_code = self._process.poll()

        if return_code is None:
            self._process.terminate()

            try:
                return_code = self._process.wait(
                    timeout=max(1.0, grace_seconds),
                )
            except subprocess.TimeoutExpired:
                self._process.kill()
                return_code = self._process.wait(
                    timeout=10,
                )

        self._console_mirror.stop()
        self._close_log()

        if return_code != 0:
            raise AdapterOperationalError(
                "FIRMAE_RUNTIME_STOP_FAILED",
                "cleanup",
                (
                    "FirmAE persistent runtime exited with "
                    f"code {return_code}; inspect {self.log_path}"
                ),
            )


def read_json_object(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(
            path.read_text(encoding="utf-8")
        )
    except FileNotFoundError as exc:
        raise AdapterOperationalError(
            "FIRMAE_METADATA_MISSING",
            "candidate_execution",
            f"Required FirmAE metadata is missing: {path}",
        ) from exc
    except json.JSONDecodeError as exc:
        raise AdapterOperationalError(
            "FIRMAE_METADATA_INVALID",
            "candidate_execution",
            f"FirmAE metadata is invalid JSON: {path}: {exc}",
        ) from exc

    if not isinstance(document, dict):
        raise AdapterOperationalError(
            "FIRMAE_METADATA_INVALID",
            "candidate_execution",
            f"FirmAE metadata must be an object: {path}",
        )

    return document


def unpack_failure_message(
    artifacts_path: Path,
    return_code: int,
) -> str:
    """Return a bounded candidate-stage failure description."""
    error_path = artifacts_path / "unpack-error.json"

    if error_path.is_file():
        try:
            document = read_json_object(error_path)
            message = document.get("message")

            if isinstance(message, str) and message.strip():
                return message.strip()[:4000]
        except (
            OSError,
            ValueError,
            json.JSONDecodeError,
            AdapterOperationalError,
        ):
            pass

    return (
        "FirmAE extraction completed without a usable root "
        f"filesystem (helper return code {return_code})"
    )


def replace_directory_with_symlink(
    alias: Path,
    target: Path,
) -> None:
    target.mkdir(
        parents=True,
        exist_ok=True,
    )

    if alias.is_symlink():
        try:
            if alias.resolve() == target.resolve():
                return
        except FileNotFoundError:
            pass

        alias.unlink()

    elif alias.exists():
        if alias.is_dir():
            shutil.rmtree(alias)
        else:
            alias.unlink()

    alias.parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    alias.symlink_to(
        target,
        target_is_directory=True,
    )


def prepare_runtime_layout(
    workspace_path: Path,
    artifacts_path: Path,
) -> tuple[Path, Path]:
    if os.environ.get("VERITAS_CONTRACT_ROOT"):
        raise AdapterOperationalError(
            "FIRMAE_EMULATION_REQUIRES_CONTAINER",
            "candidate_setup",
            (
                "FirmAE emulation cannot run through the "
                "host-side VERITAS_CONTRACT_ROOT test mapping"
            ),
        )

    scratch_path = workspace_path / "firmae-scratch"
    runtime_evidence_path = (
        artifacts_path / "firmae-runtime-evidence"
    )

    replace_directory_with_symlink(
        FIRMAE_SCRATCH,
        scratch_path,
    )
    replace_directory_with_symlink(
        RUNTIME_EVIDENCE_ALIAS,
        runtime_evidence_path,
    )

    return scratch_path, runtime_evidence_path


def build_runtime_environment(
    *,
    iid: str,
    architecture: str,
    firmware_sha256: str,
    firmware_name: str,
    brand: str,
    workspace_path: Path,
) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "IID": iid,
            "ARCH": architecture,
            "FIRMWARE_SHA": firmware_sha256,
            "FIRMWARE_NAME": firmware_name,
            "BRAND_NAME": brand,
            "HOST_UID": str(workspace_path.stat().st_uid),
            "HOST_GID": str(workspace_path.stat().st_gid),
            "USER": "root",
        }
    )
    return environment


def run_checked_helper(
    helper: Path,
    *,
    environment: dict[str, str],
    log_path: Path,
    error_code: str,
    phase: str,
) -> None:
    if not helper.is_file():
        raise AdapterOperationalError(
            "FIRMAE_RUNTIME_HELPER_MISSING",
            "candidate_setup",
            f"FirmAE runtime helper is missing: {helper}",
        )

    log_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with log_path.open(
        "w",
        encoding="utf-8",
    ) as log_handle:
        completed = subprocess.run(
            [str(helper)],
            check=False,
            text=True,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            env=environment,
        )

    if completed.returncode != 0:
        raise AdapterOperationalError(
            error_code,
            phase,
            (
                f"FirmAE helper {helper.name!r} returned "
                f"{completed.returncode}; inspect {log_path}"
            ),
        )



def _safe_archive_member_path(
    name: str,
) -> PurePosixPath | None:
    path = PurePosixPath(name)

    if path.is_absolute() or ".." in path.parts:
        raise AdapterOperationalError(
            "FIRMAE_ROOTFS_EXPORT_UNSAFE",
            "candidate_execution",
            f"Unsafe rootfs archive member path: {name!r}",
        )

    cleaned_parts = tuple(
        part
        for part in path.parts
        if part not in {"", "."}
    )

    if not cleaned_parts:
        return None

    return PurePosixPath(*cleaned_parts)


def _ensure_real_directory(
    root: Path,
    relative_parts: tuple[str, ...],
) -> Path:
    current = root

    for part in relative_parts:
        current = current / part

        if current.exists() or current.is_symlink():
            if current.is_symlink() or not current.is_dir():
                raise AdapterOperationalError(
                    "FIRMAE_ROOTFS_EXPORT_UNSAFE",
                    "candidate_execution",
                    (
                        "Rootfs archive attempts to traverse "
                        f"a non-directory path: {current}"
                    ),
                )
        else:
            current.mkdir(mode=0o755)

    return current


def _normalise_validation_copy_mode(
    *,
    original_mode: int,
    directory: bool,
) -> tuple[int, bool]:
    """
    Preserve candidate mode bits while ensuring the independent host-side
    validator can traverse directories and read regular files.

    This policy is applied only to the exported validation copy. The
    canonical rootfs archive and FirmAE's emulation filesystem are not
    modified.
    """
    original = original_mode & 0o7777
    required = 0o555 if directory else 0o444
    normalised = original | required
    return normalised, normalised != original


def export_rootfs_tree(
    *,
    rootfs_archive: Path,
    artifacts_path: Path,
    max_members: int = 500000,
    max_regular_bytes: int = 8 * 1024 * 1024 * 1024,
) -> None:
    export_parent = artifacts_path / "unpack"
    export_path = export_parent / "rootfs"
    temporary_path = export_parent / ".rootfs.tmp"
    report_path = export_parent / "export-metadata.json"

    if temporary_path.exists():
        shutil.rmtree(temporary_path)

    export_parent.mkdir(parents=True, exist_ok=True)
    temporary_path.mkdir(mode=0o755)

    source_archive_sha256_before = sha256_file(
        rootfs_archive
    )

    member_count = 0
    total_regular_bytes = 0
    regular_files = 0
    directories = 0
    symlinks = 0
    hardlinks = 0
    skipped_special = 0
    permission_normalised_directories = 0
    permission_normalised_files = 0
    deferred_hardlinks: list[
        tuple[Path, PurePosixPath]
    ] = []

    try:
        with tarfile.open(
            rootfs_archive,
            mode="r:*",
        ) as archive:
            for member in archive:
                member_count += 1

                if member_count > max_members:
                    raise AdapterOperationalError(
                        "FIRMAE_ROOTFS_EXPORT_LIMIT_EXCEEDED",
                        "candidate_execution",
                        (
                            "Rootfs archive exceeds the member "
                            f"limit of {max_members}"
                        ),
                    )

                if member.isreg():
                    total_regular_bytes += int(
                        member.size
                    )

                    if (
                        total_regular_bytes
                        > max_regular_bytes
                    ):
                        raise AdapterOperationalError(
                            "FIRMAE_ROOTFS_EXPORT_LIMIT_EXCEEDED",
                            "candidate_execution",
                            (
                                "Rootfs archive exceeds the "
                                "regular-file byte limit of "
                                f"{max_regular_bytes}"
                            ),
                        )

                relative = _safe_archive_member_path(
                    member.name
                )

                if relative is None:
                    continue

                destination = temporary_path.joinpath(
                    *relative.parts
                )
                _ensure_real_directory(
                    temporary_path,
                    tuple(relative.parts[:-1]),
                )

                if member.isdir():
                    if (
                        destination.exists()
                        or destination.is_symlink()
                    ):
                        if (
                            destination.is_symlink()
                            or not destination.is_dir()
                        ):
                            raise AdapterOperationalError(
                                "FIRMAE_ROOTFS_EXPORT_UNSAFE",
                                "candidate_execution",
                                (
                                    "Directory member collides with "
                                    f"another entry: {relative}"
                                ),
                            )
                    else:
                        destination.mkdir(mode=0o755)

                    directory_mode, changed = (
                        _normalise_validation_copy_mode(
                            original_mode=member.mode,
                            directory=True,
                        )
                    )
                    os.chmod(destination, directory_mode)
                    permission_normalised_directories += int(
                        changed
                    )

                    directories += 1
                    continue

                if member.isreg():
                    if (
                        destination.exists()
                        or destination.is_symlink()
                    ):
                        if destination.is_dir():
                            raise AdapterOperationalError(
                                "FIRMAE_ROOTFS_EXPORT_UNSAFE",
                                "candidate_execution",
                                (
                                    "Regular file collides with a "
                                    f"directory: {relative}"
                                ),
                            )
                        destination.unlink()

                    source = archive.extractfile(member)

                    if source is None:
                        raise AdapterOperationalError(
                            "FIRMAE_ROOTFS_EXPORT_FAILED",
                            "candidate_execution",
                            (
                                "Could not read rootfs archive "
                                f"member: {relative}"
                            ),
                        )

                    flags = (
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_TRUNC
                    )

                    if hasattr(os, "O_NOFOLLOW"):
                        flags |= os.O_NOFOLLOW

                    file_mode, changed = (
                        _normalise_validation_copy_mode(
                            original_mode=member.mode,
                            directory=False,
                        )
                    )

                    descriptor = os.open(
                        destination,
                        flags,
                        file_mode or 0o444,
                    )

                    with source, os.fdopen(
                        descriptor,
                        "wb",
                    ) as output:
                        shutil.copyfileobj(
                            source,
                            output,
                            length=1024 * 1024,
                        )

                    os.chmod(destination, file_mode or 0o444)
                    permission_normalised_files += int(changed)

                    regular_files += 1
                    continue

                if member.issym():
                    if (
                        destination.exists()
                        or destination.is_symlink()
                    ):
                        if destination.is_dir():
                            raise AdapterOperationalError(
                                "FIRMAE_ROOTFS_EXPORT_UNSAFE",
                                "candidate_execution",
                                (
                                    "Symlink collides with a "
                                    f"directory: {relative}"
                                ),
                            )
                        destination.unlink()

                    destination.symlink_to(member.linkname)
                    symlinks += 1
                    continue

                if member.islnk():
                    link_target = _safe_archive_member_path(
                        member.linkname
                    )

                    if link_target is None:
                        raise AdapterOperationalError(
                            "FIRMAE_ROOTFS_EXPORT_UNSAFE",
                            "candidate_execution",
                            (
                                "Hard-link target is empty: "
                                f"{member.linkname!r}"
                            ),
                        )

                    deferred_hardlinks.append(
                        (destination, link_target)
                    )
                    continue

                skipped_special += 1

        for destination, link_target in deferred_hardlinks:
            source = temporary_path.joinpath(
                *link_target.parts
            )

            try:
                resolved_source = source.resolve(
                    strict=True
                )
            except FileNotFoundError as exc:
                raise AdapterOperationalError(
                    "FIRMAE_ROOTFS_EXPORT_FAILED",
                    "candidate_execution",
                    (
                        "Hard-link target is unavailable: "
                        f"{link_target}"
                    ),
                ) from exc

            if (
                not resolved_source.is_relative_to(
                    temporary_path.resolve()
                )
                or source.is_symlink()
                or not resolved_source.is_file()
            ):
                raise AdapterOperationalError(
                    "FIRMAE_ROOTFS_EXPORT_FAILED",
                    "candidate_execution",
                    (
                        "Hard-link target is outside the "
                        f"export root or unsafe: {link_target}"
                    ),
                )

            if (
                destination.exists()
                or destination.is_symlink()
            ):
                if destination.is_dir():
                    raise AdapterOperationalError(
                        "FIRMAE_ROOTFS_EXPORT_UNSAFE",
                        "candidate_execution",
                        (
                            "Hard link collides with a "
                            f"directory: {destination}"
                        ),
                    )
                destination.unlink()

            os.link(source, destination)
            hardlinks += 1

        if export_path.exists() or export_path.is_symlink():
            if export_path.is_dir() and not export_path.is_symlink():
                shutil.rmtree(export_path)
            else:
                export_path.unlink()

        temporary_path.replace(export_path)

        source_archive_sha256_after = sha256_file(
            rootfs_archive
        )

        if (
            source_archive_sha256_after
            != source_archive_sha256_before
        ):
            raise AdapterOperationalError(
                "FIRMAE_ROOTFS_ARCHIVE_CHANGED",
                "candidate_execution",
                (
                    "The canonical rootfs archive changed while "
                    "creating the validation export"
                ),
            )

        report_path.write_text(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "adapter_id": ADAPTER_ID,
                    "exported_at": utc_now(),
                    "source_archive": str(
                        rootfs_archive
                    ),
                    "export_path": str(export_path),
                    "member_count": member_count,
                    "total_regular_bytes": (
                        total_regular_bytes
                    ),
                    "regular_files": regular_files,
                    "directories": directories,
                    "symlinks": symlinks,
                    "hardlinks": hardlinks,
                    "skipped_special_entries": (
                        skipped_special
                    ),
                    "permission_policy": (
                        "validation-copy-world-readable"
                    ),
                    "permission_normalised": bool(
                        permission_normalised_directories
                        or permission_normalised_files
                    ),
                    "permission_normalised_directories": (
                        permission_normalised_directories
                    ),
                    "permission_normalised_files": (
                        permission_normalised_files
                    ),
                    "source_archive_sha256_before": (
                        source_archive_sha256_before
                    ),
                    "source_archive_sha256_after": (
                        source_archive_sha256_after
                    ),
                    "source_archive_unchanged": True,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    except Exception:
        if temporary_path.exists():
            shutil.rmtree(
                temporary_path,
                ignore_errors=True,
            )
        raise


def detect_firmae_architecture(
    *,
    iid: str,
    artifacts_path: Path,
    runtime_evidence_path: Path,
) -> str:
    rootfs_path = artifacts_path / "rootfs.tar.gz"
    firmae_rootfs_path = FIRMAE_IMAGES / f"{iid}.tar.gz"
    scratch_directory = FIRMAE_SCRATCH / iid

    FIRMAE_IMAGES.mkdir(
        parents=True,
        exist_ok=True,
    )
    scratch_directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    if not firmae_rootfs_path.is_file():
        shutil.copy2(
            rootfs_path,
            firmae_rootfs_path,
        )

    output_path = (
        runtime_evidence_path
        / "native-architecture.raw"
    )
    log_path = (
        runtime_evidence_path
        / "native-architecture.log"
    )

    with output_path.open(
        "w",
        encoding="utf-8",
    ) as output_handle, log_path.open(
        "w",
        encoding="utf-8",
    ) as log_handle:
        completed = subprocess.run(
            [
                "/usr/bin/python3",
                str(FIRMAE_HOME / "scripts/getArch.py"),
                str(firmae_rootfs_path),
                "127.0.0.1",
            ],
            cwd=FIRMAE_HOME,
            check=False,
            text=True,
            stdout=output_handle,
            stderr=log_handle,
        )

    if completed.returncode != 0:
        raise AdapterOperationalError(
            "FIRMAE_ARCHITECTURE_DETECTION_FAILED",
            "candidate_execution",
            (
                "FirmAE architecture detection returned "
                f"{completed.returncode}; inspect {log_path}"
            ),
        )

    architecture_lines = [
        line.strip()
        for line in output_path.read_text(
            encoding="utf-8",
            errors="replace",
        ).splitlines()
        if line.strip()
    ]

    supported_architectures = {
        "armel",
        "mipseb",
        "mipsel",
    }

    architecture = next(
        (
            line
            for line in reversed(architecture_lines)
            if line in supported_architectures
        ),
        None,
    )

    if architecture is None:
        raise AdapterOperationalError(
            "FIRMAE_ARCHITECTURE_UNDETECTED",
            "candidate_execution",
            (
                "FirmAE did not produce a supported "
                "architecture; inspect "
                f"{output_path} and {log_path}"
            ),
        )

    architecture_document = {
        "schema_version": SCHEMA_VERSION,
        "adapter_id": ADAPTER_ID,
        "operation": "architecture-identification",
        "detected_at": utc_now(),
        "firmae_architecture": architecture,
        "supported_by_pinned_firmae": True,
        "method": "native-firmae-getArch",
    }

    (
        artifacts_path / "architecture.json"
    ).write_text(
        json.dumps(
            architecture_document,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    return architecture


def load_inferred_addresses(
    scratch_directory: Path,
) -> list[str]:
    count_path = scratch_directory / "ip_num"

    try:
        count = int(
            count_path.read_text(
                encoding="utf-8",
            ).strip()
        )
    except (FileNotFoundError, ValueError) as exc:
        raise AdapterOperationalError(
            "FIRMAE_NETWORK_INFERENCE_FAILED",
            "candidate_execution",
            (
                "FirmAE did not produce a valid ip_num "
                f"file in {scratch_directory}"
            ),
        ) from exc

    addresses: list[str] = []

    for index in range(count):
        address_path = scratch_directory / f"ip.{index}"

        if not address_path.is_file():
            continue

        address = address_path.read_text(
            encoding="utf-8",
        ).strip()

        if address:
            addresses.append(address)

    if not addresses:
        raise AdapterOperationalError(
            "FIRMAE_NETWORK_ADDRESS_MISSING",
            "candidate_execution",
            (
                "FirmAE network inference completed but "
                "reported no candidate IP addresses"
            ),
        )

    return addresses


def _scan_candidate_endpoints(
    *,
    address: str,
    timeout_seconds: float,
) -> list[dict[str, Any]]:
    endpoints: list[dict[str, Any]] = []

    for port, protocol in COMMON_ENDPOINTS:
        try:
            with socket.create_connection(
                (address, port),
                timeout=timeout_seconds,
            ):
                endpoints.append(
                    {
                        "host": address,
                        "port": port,
                        "protocol": protocol,
                    }
                )
        except OSError:
            continue

    return endpoints


def wait_for_candidate_network_readiness(
    *,
    runtime: RuntimeController,
    address: str,
    timeout_seconds: float,
    evidence_path: Path,
    poll_interval_seconds: float = 5.0,
) -> tuple[list[dict[str, Any]], bool]:
    """
    Observe candidate-reported network readiness without deciding boot.

    ICMP or a supported TCP endpoint is a FirmAE network-readiness
    signal. A timeout is a valid candidate outcome: the persistent
    runtime remained alive, but no network-readiness claim appeared.
    Independent VERITAS boot validation uses guest-level evidence and
    therefore remains separate from this observation.
    """
    if timeout_seconds <= 0:
        raise ValueError(
            "timeout_seconds must be positive"
        )

    if poll_interval_seconds <= 0:
        raise ValueError(
            "poll_interval_seconds must be positive"
        )

    started_at = utc_now()
    started_monotonic = time.monotonic()
    deadline = started_monotonic + timeout_seconds
    latest_ping_output = ""
    latest_endpoints: list[dict[str, Any]] = []
    attempt_count = 0

    while time.monotonic() < deadline:
        return_code = runtime.poll()

        if return_code is not None:
            runtime.record_unexpected_exit()
            raise AdapterOperationalError(
                "FIRMAE_RUNTIME_EXITED_EARLY",
                "candidate_execution",
                (
                    "FirmAE persistent runtime exited with "
                    f"code {return_code} before network readiness "
                    "was observed"
                ),
            )

        completed = subprocess.run(
            [
                "ping",
                "-c",
                "1",
                "-W",
                "2",
                address,
            ],
            check=False,
            text=True,
            capture_output=True,
        )
        attempt_count += 1
        latest_ping_output = (
            completed.stdout + completed.stderr
        )

        latest_endpoints = _scan_candidate_endpoints(
            address=address,
            timeout_seconds=0.5,
        )
        network_ready = (
            completed.returncode == 0
            or bool(latest_endpoints)
        )

        if network_ready:
            elapsed_seconds = max(
                0.0,
                time.monotonic() - started_monotonic,
            )
            evidence_path.write_text(
                json.dumps(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "adapter_id": ADAPTER_ID,
                        "started_at": started_at,
                        "observed_at": utc_now(),
                        "address": address,
                        "timeout_seconds": timeout_seconds,
                        "poll_interval_seconds": (
                            poll_interval_seconds
                        ),
                        "attempt_count": attempt_count,
                        "elapsed_seconds": elapsed_seconds,
                        "outcome": "network_ready",
                        "ping_succeeded": (
                            completed.returncode == 0
                        ),
                        "ping_output": latest_ping_output,
                        "candidate_endpoint_claims": (
                            latest_endpoints
                        ),
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            return latest_endpoints, True

        remaining_seconds = deadline - time.monotonic()

        if remaining_seconds <= 0:
            break

        time.sleep(
            min(
                poll_interval_seconds,
                remaining_seconds,
            )
        )

    elapsed_seconds = max(
        0.0,
        time.monotonic() - started_monotonic,
    )
    evidence_path.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "adapter_id": ADAPTER_ID,
                "started_at": started_at,
                "observed_at": utc_now(),
                "address": address,
                "timeout_seconds": timeout_seconds,
                "poll_interval_seconds": (
                    poll_interval_seconds
                ),
                "attempt_count": attempt_count,
                "elapsed_seconds": elapsed_seconds,
                "outcome": "timeout",
                "ping_succeeded": False,
                "ping_output": latest_ping_output,
                "candidate_endpoint_claims": (
                    latest_endpoints
                ),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    return latest_endpoints, False


def _write_endpoint_discovery_evidence(
    *,
    artifacts_path: Path,
    addresses: list[str],
    timeout_seconds: float,
    scan_interval_seconds: float,
    scan_timeout_seconds: float,
    attempts: list[dict[str, Any]],
    endpoints: list[dict[str, Any]],
    started_at: str,
    elapsed_seconds: float,
    outcome: str,
    runtime_exit_code: int | None = None,
) -> None:
    completed_at = utc_now()

    discovery_record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "adapter_id": ADAPTER_ID,
        "started_at": started_at,
        "completed_at": completed_at,
        "addresses": addresses,
        "ports_tested": [
            {
                "port": port,
                "protocol": protocol,
            }
            for port, protocol in COMMON_ENDPOINTS
        ],
        "timeout_seconds": timeout_seconds,
        "scan_interval_seconds": scan_interval_seconds,
        "scan_timeout_seconds": scan_timeout_seconds,
        "attempt_count": len(attempts),
        "elapsed_seconds": elapsed_seconds,
        "outcome": outcome,
        "attempts": attempts,
        "endpoints": endpoints,
    }

    if runtime_exit_code is not None:
        discovery_record["runtime_exit_code"] = (
            runtime_exit_code
        )

    (
        artifacts_path
        / "candidate-endpoint-discovery.json"
    ).write_text(
        json.dumps(
            discovery_record,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    (
        artifacts_path / "candidate-endpoint-claims.json"
    ).write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "adapter_id": ADAPTER_ID,
                "recorded_at": completed_at,
                "endpoints": endpoints,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def discover_candidate_endpoints(
    *,
    runtime: RuntimeController,
    addresses: list[str],
    artifacts_path: Path,
    timeout_seconds: float,
    initial_endpoints: list[dict[str, Any]] | None = None,
    scan_interval_seconds: float = 5.0,
    scan_timeout_seconds: float = 1.0,
) -> tuple[list[dict[str, Any]], int | None]:
    if timeout_seconds <= 0:
        raise ValueError(
            "timeout_seconds must be positive"
        )

    if scan_interval_seconds <= 0:
        raise ValueError(
            "scan_interval_seconds must be positive"
        )

    if scan_timeout_seconds <= 0:
        raise ValueError(
            "scan_timeout_seconds must be positive"
        )

    started_at = utc_now()
    started_monotonic = time.monotonic()
    deadline = started_monotonic + timeout_seconds
    attempts: list[dict[str, Any]] = []
    endpoints = list(initial_endpoints or [])

    if endpoints:
        attempts.append(
            {
                "attempt": 1,
                "observed_at": utc_now(),
                "elapsed_seconds": 0.0,
                "source": "boot-readiness",
                "endpoint_count": len(endpoints),
                "endpoints": endpoints,
            }
        )
    else:
        while True:
            return_code = runtime.poll()

            if return_code is not None:
                recorded_return_code = (
                    runtime.record_unexpected_exit()
                )
                elapsed_seconds = max(
                    0.0,
                    time.monotonic() - started_monotonic,
                )
                _write_endpoint_discovery_evidence(
                    artifacts_path=artifacts_path,
                    addresses=addresses,
                    timeout_seconds=timeout_seconds,
                    scan_interval_seconds=(
                        scan_interval_seconds
                    ),
                    scan_timeout_seconds=(
                        scan_timeout_seconds
                    ),
                    attempts=attempts,
                    endpoints=[],
                    started_at=started_at,
                    elapsed_seconds=elapsed_seconds,
                    outcome="runtime_exited",
                    runtime_exit_code=(
                        recorded_return_code
                    ),
                )
                return [], recorded_return_code

            now = time.monotonic()
            remaining_seconds = deadline - now

            if attempts and remaining_seconds <= 0:
                break

            per_connection_timeout = min(
                scan_timeout_seconds,
                max(
                    0.05,
                    max(remaining_seconds, 0.05)
                    / max(1, len(COMMON_ENDPOINTS)),
                ),
            )

            discovered: list[dict[str, Any]] = []

            for address in addresses:
                discovered.extend(
                    _scan_candidate_endpoints(
                        address=address,
                        timeout_seconds=(
                            per_connection_timeout
                        ),
                    )
                )

            observed_monotonic = time.monotonic()
            attempts.append(
                {
                    "attempt": len(attempts) + 1,
                    "observed_at": utc_now(),
                    "elapsed_seconds": max(
                        0.0,
                        observed_monotonic
                        - started_monotonic,
                    ),
                    "source": "active-port-scan",
                    "connection_timeout_seconds": (
                        per_connection_timeout
                    ),
                    "endpoint_count": len(discovered),
                    "endpoints": discovered,
                }
            )

            if discovered:
                endpoints = discovered
                break

            remaining_seconds = (
                deadline - observed_monotonic
            )

            if remaining_seconds <= 0:
                break

            time.sleep(
                min(
                    scan_interval_seconds,
                    remaining_seconds,
                )
            )

    elapsed_seconds = max(
        0.0,
        time.monotonic() - started_monotonic,
    )
    outcome = (
        "endpoint_found"
        if endpoints
        else "timeout"
    )

    _write_endpoint_discovery_evidence(
        artifacts_path=artifacts_path,
        addresses=addresses,
        timeout_seconds=timeout_seconds,
        scan_interval_seconds=scan_interval_seconds,
        scan_timeout_seconds=scan_timeout_seconds,
        attempts=attempts,
        endpoints=endpoints,
        started_at=started_at,
        elapsed_seconds=elapsed_seconds,
        outcome=outcome,
    )

    return endpoints, None


def dispatch_candidate(
    request: dict[str, Any],
    event_writer: EventWriter,
) -> RuntimeController | None:
    requested_stages = set(
        request["run"]["requested_stages"]
    )

    if requested_stages not in (
        {"unpack"},
        FULL_EXECUTION_STAGES,
    ):
        raise AdapterOperationalError(
            "FIRMAE_STAGE_COMBINATION_NOT_IMPLEMENTED",
            "candidate_setup",
            (
                "This FirmAE adapter supports either "
                "an unpack-only request or the complete "
                "unpack, emulate and endpoint-discovery "
                "pipeline; requested stages were: "
                + ", ".join(
                    sorted(requested_stages)
                )
            ),
        )

    hints = request.get("hints", {})
    vendor_hint = hints.get("vendor", {})

    brand = vendor_hint.get(
        "value",
        "unknown",
    )

    firmware_path = map_contract_path(
        request["firmware"]["path"]
    )
    workspace_path = map_contract_path(
        request["paths"]["workspace"]
    )
    artifacts_path = map_contract_path(
        request["paths"]["artifacts"]
    )

    workspace_path.mkdir(
        parents=True,
        exist_ok=True,
    )
    artifacts_path.mkdir(
        parents=True,
        exist_ok=True,
    )

    event_writer.emit(
        "candidate_started",
        state="running",
    )

    command = [
        "/usr/local/bin/veritas-firmae-unpack",
        "--firmware",
        str(firmware_path),
        "--artifacts",
        str(artifacts_path),
        "--workspace",
        str(workspace_path),
        "--brand",
        brand,
        "--case-id",
        request["firmware"]["case_id"],
    ]

    completed = subprocess.run(
        command,
        check=False,
        text=True,
        stdout=sys.stderr,
        stderr=sys.stderr,
    )

    if completed.returncode != 0:
        event_writer.emit(
            "stage_completed",
            state="waiting_for_shutdown",
            stage="unpack",
            stage_outcome="failed",
            message=unpack_failure_message(
                artifacts_path,
                completed.returncode,
            ),
        )
        return None

    required_artifacts = [
        artifacts_path / "rootfs.tar.gz",
        artifacts_path / "unpack-metadata.json",
        artifacts_path / "rootfs-extractor.log",
        artifacts_path / "kernel-extractor.log",
    ]

    missing = [
        path.name
        for path in required_artifacts
        if not path.is_file()
    ]

    if missing:
        raise AdapterOperationalError(
            "FIRMAE_EXTRACTION_ARTIFACT_MISSING",
            "candidate_execution",
            (
                "FirmAE extraction returned successfully "
                "but required artifacts are missing: "
                + ", ".join(missing)
            ),
        )

    export_rootfs_tree(
        rootfs_archive=(
            artifacts_path / "rootfs.tar.gz"
        ),
        artifacts_path=artifacts_path,
    )

    unpack_state = (
        "waiting_for_shutdown"
        if requested_stages == {"unpack"}
        else "running"
    )

    event_writer.emit(
        "extraction_complete",
        state=unpack_state,
    )
    event_writer.emit(
        "stage_completed",
        state=unpack_state,
        stage="unpack",
        stage_outcome="succeeded",
        message=(
            "FirmAE produced a root filesystem export for "
            "independent VERITAS validation"
        ),
    )

    if requested_stages == {"unpack"}:
        return None

    scratch_path, runtime_evidence_path = (
        prepare_runtime_layout(
            workspace_path,
            artifacts_path,
        )
    )

    unpack_metadata = read_json_object(
        artifacts_path / "unpack-metadata.json"
    )
    iid_value = unpack_metadata.get(
        "firmae_image_id"
    )

    if (
        not isinstance(iid_value, (str, int))
        or not str(iid_value).strip()
    ):
        raise AdapterOperationalError(
            "FIRMAE_IMAGE_ID_MISSING",
            "candidate_execution",
            (
                "unpack-metadata.json does not contain "
                "a valid firmae_image_id"
            ),
        )

    iid = str(iid_value).strip()

    architecture = detect_firmae_architecture(
        iid=iid,
        artifacts_path=artifacts_path,
        runtime_evidence_path=runtime_evidence_path,
    )

    environment = build_runtime_environment(
        iid=iid,
        architecture=architecture,
        firmware_sha256=request["firmware"]["sha256"],
        firmware_name=request["firmware"]["case_id"],
        brand=brand,
        workspace_path=workspace_path,
    )

    run_checked_helper(
        PREPARE_IMAGE_HELPER,
        environment=environment,
        log_path=(
            runtime_evidence_path
            / "prepare-image-wrapper.log"
        ),
        error_code="FIRMAE_IMAGE_PREPARATION_FAILED",
        phase="candidate_execution",
    )

    run_checked_helper(
        INFER_NETWORK_HELPER,
        environment=environment,
        log_path=(
            runtime_evidence_path
            / "infer-network-wrapper.log"
        ),
        error_code="FIRMAE_NETWORK_INFERENCE_FAILED",
        phase="candidate_execution",
    )

    candidate_scratch = scratch_path / iid
    generated_runner = candidate_scratch / "run.sh"
    serial_log = (
        candidate_scratch
        / "qemu.initial.serial.log"
    )

    if not generated_runner.is_file():
        raise AdapterOperationalError(
            "FIRMAE_GENERATED_RUNNER_MISSING",
            "candidate_execution",
            (
                "FirmAE network inference did not produce "
                f"{generated_runner}"
            ),
        )

    if not serial_log.is_file():
        raise AdapterOperationalError(
            "FIRMAE_INITIAL_SERIAL_LOG_MISSING",
            "candidate_execution",
            (
                "FirmAE network inference did not produce "
                f"{serial_log}"
            ),
        )

    addresses = load_inferred_addresses(
        candidate_scratch
    )

    runtime: RuntimeController | None = None

    try:
        runtime = RuntimeController.start(
            environment=environment,
            log_path=(
                runtime_evidence_path
                / "persistent-runtime-wrapper.log"
            ),
            console_source_path=(
                candidate_scratch
                / "qemu.final.serial.log"
            ),
            console_export_path=(
                artifacts_path
                / "boot"
                / "guest-console.log"
            ),
        )

        (
            runtime_evidence_path
            / "persistent-runtime.pid"
        ).write_text(
            f"{runtime.pid}\n",
            encoding="utf-8",
        )

        boot_timeout = float(
            request["lifecycle"].get(
                "boot_wait_timeout_seconds",
                min(
                    600.0,
                    max(
                        60.0,
                        float(
                            request["lifecycle"][
                                "timeout_seconds"
                            ]
                        ),
                    ),
                ),
            )
        )

        (
            readiness_endpoints,
            network_readiness_observed,
        ) = wait_for_candidate_network_readiness(
            runtime=runtime,
            address=addresses[0],
            timeout_seconds=boot_timeout,
            evidence_path=(
                runtime_evidence_path
                / "persistent-readiness.json"
            ),
        )

        if network_readiness_observed:
            event_writer.emit(
                "candidate_boot_reported",
                state="running",
            )
            emulation_message = (
                "FirmAE reported a persistent firmware runtime "
                "with an ICMP or supported TCP network-readiness "
                "signal"
            )
        else:
            emulation_message = (
                "FirmAE persistent runtime remained active for the "
                f"full {boot_timeout:.0f}-second network-readiness "
                "observation window, but produced no ICMP or "
                "supported TCP readiness signal; independent "
                "VERITAS boot validation will use guest-level "
                "evidence"
            )

        event_writer.emit(
            "stage_completed",
            state="running",
            stage="emulate",
            stage_outcome=(
                "succeeded"
                if network_readiness_observed
                else "inconclusive"
            ),
            message=emulation_message,
        )

        endpoint_timeout = float(
            request["lifecycle"].get(
                "endpoint_wait_timeout_seconds",
                300.0,
            )
        )

        (
            endpoint_claims,
            endpoint_runtime_exit_code,
        ) = discover_candidate_endpoints(
            runtime=runtime,
            addresses=addresses,
            artifacts_path=artifacts_path,
            timeout_seconds=endpoint_timeout,
            initial_endpoints=readiness_endpoints,
        )

        if endpoint_runtime_exit_code is not None:
            event_writer.emit(
                "stage_completed",
                state="waiting_for_shutdown",
                stage="endpoint-discovery",
                stage_outcome="failed",
                message=(
                    "FirmAE persistent runtime exited during "
                    "endpoint discovery with return code "
                    f"{endpoint_runtime_exit_code}"
                ),
            )
            return None

        for endpoint in endpoint_claims:
            event_writer.emit(
                "endpoint_reported",
                state="running",
                endpoint=endpoint,
            )

        endpoint_count = len(endpoint_claims)
        event_writer.emit(
            "stage_completed",
            state="waiting_for_shutdown",
            stage="endpoint-discovery",
            stage_outcome="succeeded",
            message=(
                "FirmAE endpoint discovery completed after a "
                f"bounded {endpoint_timeout:.0f}-second window "
                f"with {endpoint_count} candidate endpoint claim"
                + ("" if endpoint_count == 1 else "s")
            ),
        )

        return runtime

    except Exception:
        if runtime is not None:
            try:
                runtime.stop(
                    float(
                        request["lifecycle"][
                            "shutdown_grace_seconds"
                        ]
                    )
                )
            except AdapterOperationalError as cleanup_error:
                print(
                    (
                        "FirmAE runtime cleanup after "
                        f"candidate failure also failed: "
                        f"{cleanup_error}"
                    ),
                    file=sys.stderr,
                )

        raise

def main() -> int:
    if len(sys.argv) != 2:
        print(
            "Usage: entrypoint.py REQUEST_PATH",
            file=sys.stderr,
        )
        return 2

    request_path = Path(sys.argv[1])

    try:
        request = load_and_validate_request(
            request_path
        )
    except ContractError as exc:
        print(
            f"FirmAE contract error: {exc}",
            file=sys.stderr,
        )
        return 2

    run_id = request["run"]["run_id"]
    adapter_id = request["run"]["adapter_id"]

    events_path = map_contract_path(
        request["paths"]["events"]
    )
    control_directory = map_contract_path(
        request["paths"]["control"]
    )

    shutdown_path = control_directory / "shutdown.json"

    control_directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    event_writer = EventWriter(
        events_path,
        run_id,
        adapter_id,
    )

    lifecycle_state = {"value": "starting"}
    signal_received = threading.Event()
    database_started = False
    failure_seen = False
    runtime_controller: RuntimeController | None = None
    runtime_failure_reported = False

    def handle_signal(
        signum: int,
        _frame: Any,
    ) -> None:
        print(
            f"FirmAE adapter received signal {signum}",
            file=sys.stderr,
        )
        signal_received.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    heartbeat = HeartbeatWorker(
        event_writer=event_writer,
        interval_seconds=float(
            request["lifecycle"][
                "heartbeat_interval_seconds"
            ]
        ),
        state_getter=lambda: lifecycle_state["value"],
    )

    try:
        event_writer.emit(
            "adapter_started",
            state="starting",
        )

        heartbeat.start()

        try:
            run_dependency_doctor(
                artifacts_path=map_contract_path(
                    request["paths"]["artifacts"]
                ),
            )

            run_database_command("initialize")
            database_started = True

            lifecycle_state["value"] = "running"

            runtime_controller = dispatch_candidate(
                request,
                event_writer,
            )

        except AdapterOperationalError as exc:
            failure_seen = True
            lifecycle_state["value"] = (
                "waiting_for_shutdown"
            )

            event_writer.emit(
                "error",
                state="waiting_for_shutdown",
                error={
                    "code": exc.code,
                    "phase": exc.phase,
                    "message": str(exc)[:4000],
                    "recoverable": exc.recoverable,
                },
            )

        except Exception as exc:
            failure_seen = True
            lifecycle_state["value"] = (
                "waiting_for_shutdown"
            )

            event_writer.emit(
                "error",
                state="waiting_for_shutdown",
                error={
                    "code": "FIRMAE_UNEXPECTED_ERROR",
                    "phase": "candidate_execution",
                    "message": (
                        f"{type(exc).__name__}: {exc}"
                    )[:4000],
                    "recoverable": False,
                },
            )

        lifecycle_state["value"] = "waiting_for_shutdown"

        while (
            not shutdown_path.exists()
            and not signal_received.is_set()
        ):
            if (
                runtime_controller is not None
                and runtime_controller.poll() is not None
                and not runtime_failure_reported
            ):
                runtime_failure_reported = True
                failure_seen = True
                return_code = (
                    runtime_controller
                    .record_unexpected_exit()
                )
                runtime_controller = None

                event_writer.emit(
                    "error",
                    state="waiting_for_shutdown",
                    error={
                        "code": "FIRMAE_RUNTIME_EXITED",
                        "phase": "candidate_execution",
                        "message": (
                            "FirmAE persistent runtime "
                            "exited before VERITAS "
                            f"requested shutdown: "
                            f"return code {return_code}"
                        ),
                        "recoverable": False,
                    },
                )

            time.sleep(0.05)

        heartbeat.stop()
        lifecycle_state["value"] = "shutting_down"

        event_writer.emit(
            "shutdown_started",
            state="shutting_down",
        )

        cleanup_succeeded = True

        if runtime_controller is not None:
            try:
                runtime_controller.stop(
                    float(
                        request["lifecycle"][
                            "shutdown_grace_seconds"
                        ]
                    )
                )
                runtime_controller = None
            except AdapterOperationalError as exc:
                cleanup_succeeded = False
                failure_seen = True

                event_writer.emit(
                    "error",
                    state="shutting_down",
                    error={
                        "code": exc.code,
                        "phase": "cleanup",
                        "message": str(exc)[:4000],
                        "recoverable": False,
                    },
                )

        if database_started:
            try:
                run_database_command("stop")
            except AdapterOperationalError as exc:
                cleanup_succeeded = False
                failure_seen = True

                event_writer.emit(
                    "error",
                    state="shutting_down",
                    error={
                        "code": exc.code,
                        "phase": "cleanup",
                        "message": str(exc)[:4000],
                        "recoverable": False,
                    },
                )

        if cleanup_succeeded:
            event_writer.emit(
                "cleanup_complete",
                state="shutting_down",
            )

        if signal_received.is_set():
            outcome = "terminated"
            return_code = 143
        elif failure_seen or not cleanup_succeeded:
            outcome = "failed"
            return_code = 1
        else:
            outcome = "completed"
            return_code = 0

        event_writer.emit(
            "adapter_stopped",
            outcome=outcome,
        )

        return return_code

    finally:
        heartbeat.stop()
        event_writer.close()


if __name__ == "__main__":
    raise SystemExit(main())
