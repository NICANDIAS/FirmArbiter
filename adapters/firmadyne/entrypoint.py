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

SUPPORTED_STAGES = {"unpack", "emulate", "endpoint-discovery"}

import re as _re
RUN_ID_PATTERN = _re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}")


def require_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError(f"{name} must be a non-empty string")
    return value

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
    contract_root = os.environ.get("FIRMARBITER_CONTRACT_ROOT")
    if not contract_root:
        return Path(contract_path)
    path = PurePosixPath(contract_path)
    try:
        relative = path.relative_to("/firmarbiter")
    except ValueError as exc:
        raise ContractError(
            f"Contract path is outside /firmarbiter: {contract_path}"
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
    # Required by firmadyne.config — all helper scripts derive paths from this.
    env["FIRMWARE_DIR"] = str(FIRMADYNE_HOME)
    return env


def _wait_for_postgres_ready(
    timeout_seconds: float = 30.0,
) -> None:
    """
    Block until PostgreSQL is actually accepting connections.

    `service postgresql start` returns success as soon as the
    daemon process is forked, before it has finished initialising
    and opening its listening socket. Under system load this race
    can be lost intermittently, causing the extractor's
    psycopg2.connect() call to fail with an uncaught
    ConnectionRefusedError deep inside a subprocess whose output
    is captured but never surfaced as a clear top-level error —
    observed as a silent stage failure with no adapter-level
    diagnostic. Polling pg_isready with a bounded timeout replaces
    the implicit assumption of readiness with an explicit,
    verified check.
    """
    deadline = time.monotonic() + timeout_seconds
    last_output = ""
    while time.monotonic() < deadline:
        check = subprocess.run(
            ["pg_isready", "-h", "127.0.0.1"],
            capture_output=True, text=True, check=False,
        )
        if check.returncode == 0:
            return
        last_output = check.stdout + check.stderr
        time.sleep(0.5)
    raise AdapterOperationalError(
        "FIRMADYNE_POSTGRES_START_FAILED",
        "infrastructure",
        f"PostgreSQL did not become ready within "
        f"{timeout_seconds}s: {last_output}",
    )


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
    _wait_for_postgres_ready()
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
        phase="adapter_setup",
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

    print("[FIRMARBITER][firmadyne] Stage 1: extracting filesystem", flush=True)

    extractor = FIRMADYNE_HOME / "sources/extractor/extractor.py"
    images_dir = FIRMADYNE_IMAGES

    images_dir.mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        [
            "python3", str(extractor),
            "-b", "FIRMARBITER",
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
            message="Extractor produced no image ID in database.",
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
            message="Root filesystem archive missing.",
        )
        return False

    # Copy rootfs to artifacts and extract for FIRMARBITER independent validation
    import shutil, tarfile
    rootfs_copy = artifacts_path / "rootfs.tar.gz"
    shutil.copy2(str(rootfs), str(rootfs_copy))

    # FIRMARBITER expects extracted rootfs at artifacts/unpack/rootfs/
    unpack_dir = artifacts_path / "unpack" / "rootfs"
    unpack_dir.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(str(rootfs_copy), "r:gz") as tf:
            tf.extractall(str(unpack_dir))
        # Normalize permissions on the extracted tree so FIRMARBITER's
        # independent (non-root) verification process can read every
        # file and traverse every directory. The original tarball may
        # faithfully preserve root-only permission bits (e.g. /root,
        # /etc/shadow) from the source firmware; this step does not
        # alter file contents or add/remove any files, it only ensures
        # the export is independently inspectable. This mirrors the
        # equivalent fix already applied to EMBA's exported artifacts.
        import stat
        for walk_root, dirs, files in os.walk(str(unpack_dir)):
            for name in dirs:
                p = os.path.join(walk_root, name)
                try:
                    st = os.stat(p)
                    os.chmod(
                        p,
                        st.st_mode
                        | stat.S_IRUSR | stat.S_IXUSR
                        | stat.S_IROTH | stat.S_IXOTH,
                    )
                except OSError as e:
                    print(f"[FIRMARBITER][firmadyne] chmod failed on dir {p}: {e}", flush=True)
            for name in files:
                p = os.path.join(walk_root, name)
                try:
                    st = os.stat(p)
                    os.chmod(p, st.st_mode | stat.S_IRUSR | stat.S_IROTH)
                except OSError as e:
                    print(f"[FIRMARBITER][firmadyne] chmod failed on file {p}: {e}", flush=True)
    except Exception as exc:
        print(f"[FIRMARBITER][firmadyne] WARNING: rootfs extraction for validation failed: {exc}", flush=True)
    event_writer.emit(
        "extraction_complete",
    )

    # Stage 2: architecture detection
    print("[FIRMARBITER][firmadyne] Stage 2: detecting architecture", flush=True)

    run_checked(
        ["bash", str(FIRMADYNE_HOME / "scripts/getArch.sh"), str(rootfs)],
        phase="candidate_execution",
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
            message="Architecture detection returned nothing.",
        )
        return False

    duration = time.monotonic() - stage_start
    event_writer.emit(
        "stage_completed",
        stage="unpack",
        stage_outcome="succeeded",
        message="Filesystem extracted and architecture detected.",
    )

    print(f"[FIRMARBITER][firmadyne] Architecture: {arch}", flush=True)
    return True


def stage_emulate(
    *,
    iid: str,
    artifacts_path: Path,
    event_writer: EventWriter,
    env: dict[str, str],
    boot_wait_timeout: float,
    stage_start: float,
    shutdown_event: "threading.Event | None" = None,
) -> tuple[bool, str, subprocess.Popen | None]:
    """Stages 3-6: tar2db, makeImage, inferNetwork, launch QEMU."""

    print("[FIRMARBITER][firmadyne] Stage 3: loading filesystem database", flush=True)

    rootfs = FIRMADYNE_IMAGES / f"{iid}.tar.gz"

    arch_result = subprocess.run(
        ["psql", "-Atqc", f"SELECT arch FROM image WHERE id={iid};"],
        capture_output=True, text=True, check=False, env=env,
    )
    arch = arch_result.stdout.strip()

    run_checked(
        ["python3", str(FIRMADYNE_HOME / "scripts/tar2db.py"), "-i", iid, "-f", str(rootfs)],
        phase="candidate_execution",
        error_code="FIRMADYNE_TAR2DB_FAILED",
        cwd=str(FIRMADYNE_HOME),
        env=env,
    )

    print("[FIRMARBITER][firmadyne] Stage 4: creating QEMU image", flush=True)

    run_checked(
        ["bash", str(FIRMADYNE_HOME / "scripts/makeImage.sh"), iid, arch],
        phase="candidate_execution",
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

    print("[FIRMARBITER][firmadyne] Stage 5: inferring network", flush=True)

    run_checked(
        ["bash", str(FIRMADYNE_HOME / "scripts/inferNetwork.sh"), iid, arch],
        phase="candidate_execution",
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

    # Copy run.sh to artifacts before any parsing attempt
    import shutil, re
    shutil.copy2(str(run_sh), str(artifacts_path / "generated_run.sh"))

    # Extract candidate-reported IP — try multiple patterns
    run_sh_text = run_sh.read_text()

    # Pattern 1: sudo ip route add <IP>
    match = re.search(r"sudo\s+ip\s+route\s+add\s+(\d+\.\d+\.\d+\.\d+)", run_sh_text)
    # Pattern 2: -net <IP>
    if not match:
        match = re.search(r"-net\s+(\d+\.\d+\.\d+\.\d+)", run_sh_text)
    # Pattern 3: any bare IP-like string after NET=
    if not match:
        match = re.search(r"NET=(\d+\.\d+\.\d+\.\d+)", run_sh_text)

    target_ip = match.group(1) if match else ""

    # Detect network mode: TAP (IP known) vs socket (IP unknown until runtime)
    socket_mode = "netdev socket" in run_sh_text or "listen=:" in run_sh_text
    tap_mode = "tap" in run_sh_text and target_ip

    if not target_ip and not socket_mode:
        # Log the run.sh content for diagnosis
        (artifacts_path / "run_sh_debug.txt").write_text(run_sh_text)
        raise AdapterOperationalError(
            "FIRMADYNE_NO_TARGET_IP",
            "emulate",
            f"Could not parse firmware IP from run.sh and no socket mode detected. "
            f"Content saved to run_sh_debug.txt. "
            f"First 500 chars: {run_sh_text[:500]}",
        )

    if socket_mode and not target_ip:
        # Socket-mode: IP not known until firmware boots.
        # We launch QEMU and detect boot from process survival only.
        target_ip = "unknown"
        (artifacts_path / "network_mode.txt").write_text("socket\n")
    else:
        (artifacts_path / "network_mode.txt").write_text(f"tap:{target_ip}\n")

    print("[FIRMARBITER][firmadyne] Stage 6: launching final emulation", flush=True)

    qemu_proc = subprocess.Popen(
        ["bash", str(run_sh)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=str(FIRMADYNE_HOME),
        env=env,
    )
    # CRITICAL: FirmArbiter's independent boot verification
    # (firmarbiter_core/probes/boot_validation.py) requires a real
    # artifacts/boot/guest-console.log file containing genuine QEMU
    # serial console output (kernel + userspace boot markers) — without
    # it, boot is always reported "inconclusive" regardless of whether
    # the firmware actually booted. Confirmed via a real run: qemu_proc's
    # stdout was captured via PIPE but never read anywhere, so this file
    # was never created. Start a background thread to tee the output in
    # real time, matching the same pattern FirmAE already uses correctly.
    console_log_path = artifacts_path / "boot" / "guest-console.log"
    console_log_path.parent.mkdir(parents=True, exist_ok=True)

    def _tee_console_output():
        try:
            with console_log_path.open("a", encoding="utf-8") as f:
                for line in qemu_proc.stdout:
                    f.write(line)
                    f.flush()
        except Exception:
            pass

    console_thread = threading.Thread(target=_tee_console_output, daemon=True)
    console_thread.start()

    # Detect network mode from run.sh content
    socket_mode = "netdev socket" in run_sh_text or "listen=:" in run_sh_text

    if socket_mode:
        # Socket-mode networking: no TAP interface created on host.
        # Confirm QEMU stays alive for at least 10 seconds.
        print("[FIRMARBITER][firmadyne] Socket-mode networking detected — waiting for QEMU stability", flush=True)
        for _i in range(10):
            if shutdown_event is not None and shutdown_event.is_set():
                print("[FIRMARBITER][firmadyne] shutdown requested during socket-mode stability wait", flush=True)
                break
            if qemu_proc.poll() is not None:
                raise AdapterOperationalError(
                    "FIRMADYNE_QEMU_EARLY_EXIT",
                    "emulate",
                    "QEMU exited within 10s of launch (socket-mode).",
                )
            time.sleep(1)
        print("[FIRMARBITER][firmadyne] QEMU stable in socket mode", flush=True)
    else:
        tap_name = f"tap{iid}_0"
        tap_ready = False
        deadline = time.monotonic() + 20

        while time.monotonic() < deadline:
            if shutdown_event is not None and shutdown_event.is_set():
                print("[FIRMARBITER][firmadyne] shutdown requested during TAP interface wait", flush=True)
                break
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

    # Write deterministic boot marker for FIRMARBITER probe
    boot_ip_file = artifacts_path / "firmarbiter_boot_ip.txt"
    boot_ip_file.write_text(target_ip + "\n")

    duration = time.monotonic() - stage_start
    event_writer.emit(
        "stage_completed",
        stage="emulate",
        stage_outcome="succeeded",
        message="QEMU launched and firmware runtime is active.",
    )

    print(
        f"[FIRMARBITER][firmadyne] BOOT SUCCESS interface up {target_ip}",
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

    print("[FIRMARBITER][firmadyne] Stage 7: endpoint discovery", flush=True)

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
            message=f"HTTP endpoint found at {target_ip}:80.",
        )
    else:
        event_writer.emit(
            "stage_completed",
            stage="endpoint-discovery",
            stage_outcome="failed",
            message=f"No TCP response within {endpoint_wait_timeout:.0f}s.",
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

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

    run = require_object(request.get("run"), "run")
    firmware = require_object(request.get("firmware"), "firmware")
    lifecycle = require_object(request.get("lifecycle"), "lifecycle")
    paths = require_object(request.get("paths"), "paths")

    run_id = require_string(run.get("run_id"), "run.run_id")

    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise ContractError(f"Invalid run.run_id: {run_id}")

    adapter_id = require_string(run.get("adapter_id"), "run.adapter_id")

    if adapter_id != ADAPTER_ID:
        raise ContractError(
            f"Request adapter_id must be {ADAPTER_ID!r}, "
            f"not {adapter_id!r}"
        )

    firmware_contract_path = firmware.get("path")

    if firmware_contract_path != "/firmarbiter/input/firmware":
        raise ContractError(
            "firmware.path must be '/firmarbiter/input/firmware'"
        )

    firmware_path = map_contract_path(firmware_contract_path)

    if not firmware_path.is_file():
        raise ContractError(
            f"Firmware input does not exist: {firmware_path}"
        )

    request["_firmware_path"] = firmware_path
    request["_run_id"] = run_id

    return request


def main() -> int:
    if len(sys.argv) != 2:
        print(
            "Usage: entrypoint.py REQUEST_PATH",
            file=sys.stderr,
        )
        return 2

    request_path = Path(sys.argv[1])
    try:
        request = load_and_validate_request(request_path)
    except ContractError as exc:
        print(
            f"[FIRMARBITER][firmadyne] contract error: {exc}",
            file=sys.stderr,
        )
        return 2

    paths = request["paths"]
    run_id = request["_run_id"]
    adapter_id = ADAPTER_ID
    firmware_path = request["_firmware_path"]

    lifecycle = require_object(request.get("lifecycle"), "lifecycle")

    events_path = map_contract_path(paths["events"])
    artifacts_path = map_contract_path(paths["artifacts"])
    control_directory = map_contract_path(paths["control"])
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
            f"[FIRMARBITER][firmadyne] received signal {signum}",
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

            # CRITICAL: requested_stages was never checked here — this
            # adapter always attempted emulate+endpoint-discovery after any
            # successful unpack, regardless of what was actually requested.
            # Confirmed via a real run: a "--stages unpack" run's
            # request.json showed run.requested_stages == ["unpack"], but
            # stage_emulate() ran anyway. Every prior "unpack-only"
            # FIRMADYNE run may have silently also attempted full
            # emulation, consuming unrequested time/resources and
            # contributing to the shutdown_failed pattern (a shutdown
            # request arriving mid-emulate had no way to interrupt it).
            requested_stages = set(
                request.get("run", {}).get("requested_stages", [])
            )

            if unpack_ok and "emulate" in requested_stages:
                iid = iid_holder[0]
                emulate_start = time.monotonic()
                emulate_ok, target_ip, qemu_proc = stage_emulate(
                    iid=iid,
                    artifacts_path=artifacts_path,
                    event_writer=event_writer,
                    env=env,
                    boot_wait_timeout=boot_wait_timeout,
                    stage_start=emulate_start,
                    shutdown_event=signal_received,
                )

                if emulate_ok and "endpoint-discovery" in requested_stages:
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
                            f"FIRMADYNE QEMU exited before FIRMARBITER "
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
