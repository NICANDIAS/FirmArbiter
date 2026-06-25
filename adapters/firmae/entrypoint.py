#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
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


def dispatch_candidate(
    request: dict[str, Any],
    event_writer: EventWriter,
) -> None:
    requested_stages = request["run"][
        "requested_stages"
    ]

    if set(requested_stages) != {"unpack"}:
        raise AdapterOperationalError(
            "FIRMAE_STAGE_NOT_IMPLEMENTED",
            "candidate_setup",
            (
                "This FirmAE adapter checkpoint currently "
                "supports only an unpack-only request; "
                "requested stages were: "
                + ", ".join(requested_stages)
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
        raise AdapterOperationalError(
            "FIRMAE_EXTRACTION_FAILED",
            "candidate_execution",
            (
                "FirmAE extraction failed with return "
                f"code {completed.returncode}; inspect "
                "/veritas/artifacts/rootfs-extractor.log, "
                "/veritas/artifacts/kernel-extractor.log "
                "and /veritas/artifacts/unpack-error.json"
            ),
        )

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

    event_writer.emit(
        "extraction_complete",
        state="waiting_for_shutdown",
    )


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
            run_database_command("initialize")
            database_started = True

            lifecycle_state["value"] = "running"

            dispatch_candidate(
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
            time.sleep(0.05)

        heartbeat.stop()
        lifecycle_state["value"] = "shutting_down"

        event_writer.emit(
            "shutdown_started",
            state="shutting_down",
        )

        cleanup_succeeded = True

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
