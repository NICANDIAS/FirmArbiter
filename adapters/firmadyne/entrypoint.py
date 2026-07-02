#!/usr/bin/env python3
"""FIRMADYNE adapter entrypoint v0.1.0.

Implements Adapter Contract v1.0 for FIRMADYNE.
Pipeline mirrors candidates/firmadyne/run_adapter.sh.
Structure mirrors adapters/firmae/entrypoint.py.
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable

ADAPTER_ID = "firmadyne"
SCHEMA_VERSION = "1.0"
CONTRACT_VERSION = "1.0"

FIRMADYNE_HOME = Path("/opt/firmadyne")
FIRMADYNE_IMAGES = FIRMADYNE_HOME / "images"
FIRMADYNE_SCRATCH = FIRMADYNE_HOME / "scratch"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ContractError(RuntimeError):
    """Malformed or missing run-request field."""


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


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def map_contract_path(contract_path: str) -> Path:
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


def require_object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{name} must be an object")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run_checked(
    cmd: list[str],
    *,
    phase: str,
    error_code: str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AdapterOperationalError(
            error_code,
            phase,
            f"Command timed out after {timeout}s: {cmd}",
        ) from exc
    except OSError as exc:
        raise AdapterOperationalError(
            error_code,
            phase,
            f"Failed to launch command {cmd}: {exc}",
        ) from exc
    if result.returncode != 0:
        raise AdapterOperationalError(
            error_code,
            phase,
            f"Command {cmd} exited {result.returncode}:\n"
            f"stdout: {result.stdout[-2000:]}\n"
            f"stderr: {result.stderr[-2000:]}",
        )
    return result


# ---------------------------------------------------------------------------
# Event writer
# ---------------------------------------------------------------------------

class EventWriter:
    def __init__(
        self,
        events_path: Path,
        run_id: str,
        adapter_id: str,
    ) -> None:
        events_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = events_path.open(
            "a",
            encoding="utf-8",
            buffering=1,
        )
        self._run_id = run_id
        self._adapter_id = adapter_id
        self._sequence = 0
        self._lock = threading.Lock()
        self._closed = False

    def emit(self, event_name: str, **additional_fields: Any) -> None:
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
                json.dumps(document, sort_keys=True, separators=(",", ":"))
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


# ---------------------------------------------------------------------------
# Heartbeat worker
# ---------------------------------------------------------------------------

class HeartbeatWorker:
    def __init__(
        self,
        event_writer: EventWriter,
        interval_seconds: float,
        state_getter: Callable[[], str],
    ) -> None:
        self._event_writer = event_writer
        self._interval_seconds = max(0.1, interval_seconds)
        self._state_getter = state_getter
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="firmadyne-heartbeat",
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
                next_heartbeat = current_time + self._interval_seconds
            self._stop_event.wait(0.05)


# ---------------------------------------------------------------------------
# PostgreSQL control
# ---------------------------------------------------------------------------

def pg_env(request: dict[str, Any]) -> dict[str, str]:
    env = os.environ.copy()
    env["PGHOST"] = "127.0.0.1"
    env["PGPORT"] = "5432"
    env["PGUSER"] = "firmadyne"
    env["PGPASSWORD"] = "firmadyne"
    env["PGDATABASE"] = "firmware"
    env["USER"] = "firmadyne"
    return env


def start_postgres() -> None:
    result = subprocess.run(
        ["service", "postgresql", "start"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise AdapterOperationalError(
            "FIRMADYNE_POSTGRES_START_FAILED",
            "infrastructure",
            f"PostgreSQL failed to start: {result.stderr}",
        )
    env = {
        "PGHOST": "127.0.0.1",
        "PGPORT": "5432",
        "PGUSER": "firmadyne",
        "PGPASSWORD": "firmadyne",
        "PGDATABASE": "firmware",
    }
    for attempt in range(30):
        r = subprocess.run(
            ["pg_isready", "-h", "127.0.0.1", "-p", "5432", "-U", "postgres"],
            capture_output=True, text=True, check=False, env={**os.environ, **env},
        )
        if r.returncode == 0:
            return
        if attempt == 29:
            raise AdapterOperationalError(
                "FIRMADYNE_POSTGRES_READY_TIMEOUT",
                "infrastructure",
                "PostgreSQL did not become ready within 30 seconds.",
            )
        time.sleep(1)


def stop_postgres() -> None:
    subprocess.run(
        ["service", "postgresql", "stop"],
        capture_output=True, text=True, check=False,
    )


def reset_postgres(env: dict[str, str]) -> None:
    run_checked(
        [
            "psql", "-v", "ON_ERROR_STOP=1", "-c",
            "TRUNCATE TABLE object_to_image, object, image, "
            "product, brand RESTART IDENTITY CASCADE;",
        ],
        phase="infrastructure",
        error_code="FIRMADYNE_POSTGRES_RESET_FAILED",
        env=env,
    )


# ---------------------------------------------------------------------------
# Pipeline stages
# ---------------------------------------------------------------------------

def stage_unpack(
    *,
    firmware_path: Path,
    iid_holder: list[str],
    artifacts_path: Path,
    event_writer: EventWriter,
    env: dict[str, str],
    stage_start: float,
) -> bool:
    """Stage 1+2: extract filesystem and detect architecture."""

    print("[VERITAS][firmadyne] Stage 1: extracting filesystem", flush=True)

    extractor = FIRMADYNE_HOME / "sources/extractor/extractor.py"
    images_dir = FIRMADYNE_IMAGES

    images_dir.mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        [
            "python3", str(extractor),
            "-b", "VERITAS",
            "-sql", "127.0.0.1",
            "-np", "-nk",
            str(firmware_path),
            str(images_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(FIRMADYNE_HOME),
        env=env,
    )

    stdout_log = artifacts_path / "extractor_stdout.txt"
    stderr_log = artifacts_path / "extractor_stderr.txt"
    stdout_log.write_text(result.stdout)
    stderr_log.write_text(result.stderr)

    # Query image ID regardless of extractor exit code —
    # some versions exit non-zero even on partial success.
    iid_result = subprocess.run(
        ["psql", "-Atqc", "SELECT MAX(id) FROM image;"],
        capture_output=True, text=True, check=False, env=env,
    )
    iid = iid_result.stdout.strip()

    if not iid:
        duration = time.monotonic() - stage_start
        event_writer.emit(
            "stage_completed",
            stage="unpack",
            stage_outcome="failed",
            duration_seconds=round(duration, 3),
            detail="Extractor produced no image ID in database.",
        )
        return False

    iid_holder.append(iid)
    rootfs = FIRMADYNE_IMAGES / f"{iid}.tar.gz"

    if not rootfs.is_file():
        duration = time.monotonic() - stage_start
        event_writer.emit(
            "stage_completed",
            stage="unpack",
            stage_outcome="failed",
            duration_seconds=round(duration, 3),
            detail=f"Root filesystem archive missing: {rootfs}",
        )
        return False

    # Copy rootfs to artifacts
    import shutil
    rootfs_copy = artifacts_path / "rootfs.tar.gz"
    shutil.copy2(str(rootfs), str(rootfs_copy))

    event_writer.emit(
        "extraction_complete",
        iid=iid,
        rootfs_path=str(rootfs_copy),
        sha256=sha256_file(rootfs),
    )

    # Stage 2: architecture detection
    print("[VERITAS][firmadyne] Stage 2: detecting architecture", flush=True)

    run_checked(
        ["bash", str(FIRMADYNE_HOME / "scripts/getArch.sh"), str(rootfs)],
        phase="unpack",
        error_code="FIRMADYNE_ARCH_DETECTION_FAILED",
        cwd=str(FIRMADYNE_HOME),
        env=env,
    )

    arch_result = subprocess.run(
        ["psql", "-Atqc", f"SELECT arch FROM image WHERE id={iid};"],
        capture_output=True, text=True, check=False, env=env,
    )
    arch = arch_result.stdout.strip()

    if not arch:
        duration = time.monotonic() - stage_start
        event_writer.emit(
            "stage_completed",
            stage="unpack",
            stage_outcome="failed",
            duration_seconds=round(duration, 3),
            detail="Architecture detection returned nothing.",
        )
        return False

    duration = time.monotonic() - stage_start
    event_writer.emit(
        "stage_completed",
        stage="unpack",
        stage_outcome="succeeded",
        duration_seconds=round(duration, 3),
        iid=iid,
        architecture=arch,
    )

    print(f"[VERITAS][firmadyne] Architecture: {arch}", flush=True)
    return True


def stage_emulate(
    *,
    iid: str,
    artifacts_path: Path,
    event_writer: EventWriter,
    env: dict[str, str],
    boot_wait_timeout: float,
    stage_start: float,
) -> tuple[bool, str, subprocess.Popen | None]:
    """Stages 3-6: tar2db, makeImage, inferNetwork, launch QEMU."""

    print("[VERITAS][firmadyne] Stage 3: loading filesystem database", flush=True)

    rootfs = FIRMADYNE_IMAGES / f"{iid}.tar.gz"

    arch_result = subprocess.run(
        ["psql", "-Atqc", f"SELECT arch FROM image WHERE id={iid};"],
        capture_output=True, text=True, check=False, env=env,
    )
    arch = arch_result.stdout.strip()

    run_checked(
        ["python3", str(FIRMADYNE_HOME / "scripts/tar2db.py"), "-i", iid, "-f", str(rootfs)],
        phase="emulate",
        error_code="FIRMADYNE_TAR2DB_FAILED",
        cwd=str(FIRMADYNE_HOME),
        env=env,
    )

    print("[VERITAS][firmadyne] Stage 4: creating QEMU image", flush=True)

    run_checked(
        ["bash", str(FIRMADYNE_HOME / "scripts/makeImage.sh"), iid, arch],
        phase="emulate",
        error_code="FIRMADYNE_MAKEIMAGE_FAILED",
        cwd=str(FIRMADYNE_HOME),
        env=env,
    )

    raw_image = FIRMADYNE_SCRATCH / iid / "image.raw"
    if not raw_image.is_file():
        raise AdapterOperationalError(
            "FIRMADYNE_RAW_IMAGE_MISSING",
            "emulate",
            f"QEMU disk image not found: {raw_image}",
        )

    print("[VERITAS][firmadyne] Stage 5: inferring network", flush=True)

    run_checked(
        ["bash", str(FIRMADYNE_HOME / "scripts/inferNetwork.sh"), iid, arch],
        phase="emulate",
        error_code="FIRMADYNE_INFER_NETWORK_FAILED",
        cwd=str(FIRMADYNE_HOME),
        env=env,
    )

    run_sh = FIRMADYNE_SCRATCH / iid / "run.sh"
    if not run_sh.is_file():
        raise AdapterOperationalError(
            "FIRMADYNE_RUN_SH_MISSING",
            "emulate",
            "Network inference produced no run.sh.",
        )

    # Extract candidate-reported IP
    import re
    run_sh_text = run_sh.read_text()
    match = re.search(
        r"sudo ip route add (\S+)",
        run_sh_text,
    )
    target_ip = match.group(1) if match else ""

    if not target_ip:
        raise AdapterOperationalError(
            "FIRMADYNE_NO_TARGET_IP",
            "emulate",
            "Could not parse firmware IP from run.sh.",
        )

    import shutil
    shutil.copy2(str(run_sh), str(artifacts_path / "generated_run.sh"))

    print("[VERITAS][firmadyne] Stage 6: launching final emulation", flush=True)

    qemu_proc = subprocess.Popen(
        ["bash", str(run_sh)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=str(FIRMADYNE_HOME),
        env=env,
    )

    tap_name = f"tap{iid}_0"
    tap_ready = False
    deadline = time.monotonic() + 20

    while time.monotonic() < deadline:
        if qemu_proc.poll() is not None:
            raise AdapterOperationalError(
                "FIRMADYNE_QEMU_EARLY_EXIT",
                "emulate",
                "Final QEMU process exited before TAP interface appeared.",
            )
        r = subprocess.run(
            ["ip", "link", "show", tap_name],
            capture_output=True, check=False,
        )
        if r.returncode == 0:
            tap_ready = True
            break
        time.sleep(1)

    if not tap_ready:
        qemu_proc.terminate()
        raise AdapterOperationalError(
            "FIRMADYNE_TAP_TIMEOUT",
            "emulate",
            f"TAP interface {tap_name} did not appear within 20s.",
        )

    # Write deterministic boot marker for VERITAS probe
    boot_ip_file = artifacts_path / "veritas_boot_ip.txt"
    boot_ip_file.write_text(target_ip + "\n")

    duration = time.monotonic() - stage_start
    event_writer.emit(
        "stage_completed",
        stage="emulate",
        stage_outcome="succeeded",
        duration_seconds=round(duration, 3),
        candidate_reported_ip=target_ip,
    )

    print(
        f"[VERITAS][firmadyne] BOOT SUCCESS interface up {target_ip}",
        flush=True,
    )

    return True, target_ip, qemu_proc


def stage_endpoint_discovery(
    *,
    target_ip: str,
    event_writer: EventWriter,
    endpoint_wait_timeout: float,
    stage_start: float,
) -> None:
    """Probe for HTTP endpoint on candidate-reported IP."""

    import socket

    print("[VERITAS][firmadyne] Stage 7: endpoint discovery", flush=True)

    deadline = time.monotonic() + endpoint_wait_timeout
    found = False

    while time.monotonic() < deadline:
        try:
            with socket.create_connection((target_ip, 80), timeout=2):
                found = True
                break
        except OSError:
            time.sleep(2)

    duration = time.monotonic() - stage_start

    if found:
        event_writer.emit(
            "endpoint_reported",
            protocol="http",
            host=target_ip,
            port=80,
        )
        event_writer.emit(
            "stage_completed",
            stage="endpoint-discovery",
            stage_outcome="succeeded",
            duration_seconds=round(duration, 3),
            endpoint=f"http://{target_ip}:80",
        )
    else:
        event_writer.emit(
            "stage_completed",
            stage="endpoint-discovery",
            stage_outcome="failed",
            duration_seconds=round(duration, 3),
            detail=f"No TCP response on {target_ip}:80 "
                   f"within {endpoint_wait_timeout}s.",
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    if len(sys.argv) < 2:
        print(
            "[VERITAS][firmadyne] ERROR: usage: entrypoint.py <request.json>",
            file=sys.stderr,
        )
        return 3

    request_path = Path(sys.argv[1])
    if not request_path.is_file():
        print(
            f"[VERITAS][firmadyne] ERROR: request file not found: {request_path}",
            file=sys.stderr,
        )
        return 3

    try:
        request = json.loads(request_path.read_text())
    except Exception as exc:
        print(
            f"[VERITAS][firmadyne] ERROR: could not parse request: {exc}",
            file=sys.stderr,
        )
        return 3

    try:
        lifecycle = require_object(request.get("lifecycle"), "lifecycle")
        paths = require_object(request.get("paths"), "paths")
    except ContractError as exc:
        print(f"[VERITAS][firmadyne] CONTRACT ERROR: {exc}", file=sys.stderr)
        return 3

    run_id = request.get("run_id", "unknown")
    adapter_id = ADAPTER_ID

    events_path = map_contract_path(paths["events"])
    artifacts_path = map_contract_path(paths["artifacts"])
    firmware_path = map_contract_path(paths["firmware"])
    control_directory = map_contract_path(paths.get("control", "/veritas/control"))
    shutdown_path = control_directory / "shutdown.json"

    artifacts_path.mkdir(parents=True, exist_ok=True)
    control_directory.mkdir(parents=True, exist_ok=True)

    heartbeat_interval = float(lifecycle.get("heartbeat_interval_seconds", 30))
    boot_wait_timeout = float(lifecycle.get("boot_wait_timeout_seconds", 900))
    endpoint_wait_timeout = float(lifecycle.get("endpoint_wait_timeout_seconds", 60))

    event_writer = EventWriter(events_path, run_id, adapter_id)

    lifecycle_state = {"value": "starting"}
    signal_received = threading.Event()
    failure_seen = False
    database_started = False
    qemu_proc: subprocess.Popen | None = None

    def handle_signal(signum: int, _frame: Any) -> None:
        print(
            f"[VERITAS][firmadyne] received signal {signum}",
            file=sys.stderr,
        )
        signal_received.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    heartbeat = HeartbeatWorker(
        event_writer=event_writer,
        interval_seconds=heartbeat_interval,
        state_getter=lambda: lifecycle_state["value"],
    )

    try:
        event_writer.emit("adapter_started", state="starting")
        heartbeat.start()

        env = pg_env(request)

        try:
            start_postgres()
            database_started = True
            reset_postgres(env)

            # Clean scratch and images from any prior run
            import shutil
            if FIRMADYNE_IMAGES.exists():
                shutil.rmtree(str(FIRMADYNE_IMAGES))
            FIRMADYNE_IMAGES.mkdir(parents=True)
            if FIRMADYNE_SCRATCH.exists():
                shutil.rmtree(str(FIRMADYNE_SCRATCH))
            FIRMADYNE_SCRATCH.mkdir(parents=True)

            lifecycle_state["value"] = "running"

            iid_holder: list[str] = []
            unpack_start = time.monotonic()

            unpack_ok = stage_unpack(
                firmware_path=firmware_path,
                iid_holder=iid_holder,
                artifacts_path=artifacts_path,
                event_writer=event_writer,
                env=env,
                stage_start=unpack_start,
            )

            if not unpack_ok:
                failure_seen = True

            if unpack_ok:
                iid = iid_holder[0]
                emulate_start = time.monotonic()
                emulate_ok, target_ip, qemu_proc = stage_emulate(
                    iid=iid,
                    artifacts_path=artifacts_path,
                    event_writer=event_writer,
                    env=env,
                    boot_wait_timeout=boot_wait_timeout,
                    stage_start=emulate_start,
                )

                if emulate_ok:
                    endpoint_start = time.monotonic()
                    stage_endpoint_discovery(
                        target_ip=target_ip,
                        event_writer=event_writer,
                        endpoint_wait_timeout=endpoint_wait_timeout,
                        stage_start=endpoint_start,
                    )

        except AdapterOperationalError as exc:
            failure_seen = True
            lifecycle_state["value"] = "waiting_for_shutdown"
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
            lifecycle_state["value"] = "waiting_for_shutdown"
            event_writer.emit(
                "error",
                state="waiting_for_shutdown",
                error={
                    "code": "FIRMADYNE_UNEXPECTED_ERROR",
                    "phase": "candidate_execution",
                    "message": f"{type(exc).__name__}: {exc}"[:4000],
                    "recoverable": False,
                },
            )

        lifecycle_state["value"] = "waiting_for_shutdown"

        while not shutdown_path.exists() and not signal_received.is_set():
            if qemu_proc is not None and qemu_proc.poll() is not None:
                failure_seen = True
                rc = qemu_proc.returncode
                qemu_proc = None
                event_writer.emit(
                    "error",
                    state="waiting_for_shutdown",
                    error={
                        "code": "FIRMADYNE_RUNTIME_EXITED",
                        "phase": "candidate_execution",
                        "message": (
                            f"FIRMADYNE QEMU exited before VERITAS "
                            f"requested shutdown: return code {rc}"
                        ),
                        "recoverable": False,
                    },
                )
            time.sleep(0.05)

        heartbeat.stop()
        lifecycle_state["value"] = "shutting_down"
        event_writer.emit("shutdown_started", state="shutting_down")

        cleanup_succeeded = True

        if qemu_proc is not None:
            try:
                qemu_proc.terminate()
                for _ in range(15):
                    if qemu_proc.poll() is not None:
                        break
                    time.sleep(1)
                if qemu_proc.poll() is None:
                    qemu_proc.kill()
                qemu_proc.wait()
            except Exception as exc:
                cleanup_succeeded = False
                failure_seen = True
                event_writer.emit(
                    "error",
                    state="shutting_down",
                    error={
                        "code": "FIRMADYNE_QEMU_STOP_FAILED",
                        "phase": "cleanup",
                        "message": str(exc)[:4000],
                        "recoverable": False,
                    },
                )

        if database_started:
            try:
                stop_postgres()
            except Exception as exc:
                cleanup_succeeded = False
                failure_seen = True
                event_writer.emit(
                    "error",
                    state="shutting_down",
                    error={
                        "code": "FIRMADYNE_POSTGRES_STOP_FAILED",
                        "phase": "cleanup",
                        "message": str(exc)[:4000],
                        "recoverable": False,
                    },
                )

        if cleanup_succeeded:
            event_writer.emit("cleanup_complete", state="shutting_down")

        if signal_received.is_set():
            outcome = "terminated"
            return_code = 143
        elif failure_seen or not cleanup_succeeded:
            outcome = "failed"
            return_code = 1
        else:
            outcome = "completed"
            return_code = 0

        event_writer.emit("adapter_stopped", outcome=outcome)
        return return_code

    finally:
        heartbeat.stop()
        event_writer.close()


if __name__ == "__main__":
    raise SystemExit(main())
