from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


class BootValidationError(RuntimeError):
    """Raised when boot validation is configured incorrectly."""


@dataclass(frozen=True)
class GuestConsoleEvidence:
    path: str
    exists: bool
    size_bytes: int | None
    sha256: str | None
    bytes_examined: int
    truncated: bool
    kernel_markers: tuple[str, ...]
    userspace_markers: tuple[str, ...]
    failure_markers: tuple[str, ...]
    errors: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        document = asdict(self)
        document["kernel_markers"] = list(
            self.kernel_markers
        )
        document["userspace_markers"] = list(
            self.userspace_markers
        )
        document["failure_markers"] = list(
            self.failure_markers
        )
        document["errors"] = list(self.errors)
        return document


@dataclass(frozen=True)
class BootObservation:
    metric: str
    status: str
    observed_at: str
    methods: tuple[str, ...]
    candidate_boot_claim_present: bool
    authenticated_service_count: int
    lifecycle_outcome: str | None
    console: GuestConsoleEvidence
    reason: str

    def to_dict(self) -> dict[str, Any]:
        document = asdict(self)
        document["methods"] = list(self.methods)
        document["console"] = self.console.to_dict()
        return document


_KERNEL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "linux_version",
        re.compile(r"\bLinux version\s+\S+", re.IGNORECASE),
    ),
    (
        "booting_linux_cpu",
        re.compile(
            r"Booting Linux on physical CPU",
            re.IGNORECASE,
        ),
    ),
    (
        "kernel_command_line",
        re.compile(
            r"\bKernel command line:",
            re.IGNORECASE,
        ),
    ),
    (
        "freeing_kernel_memory",
        re.compile(
            r"Freeing unused kernel (?:image )?memory",
            re.IGNORECASE,
        ),
    ),
)

_USERSPACE_PATTERNS: tuple[
    tuple[str, re.Pattern[str]], ...
] = (
    (
        "run_init",
        re.compile(
            r"Run /(?:sbin/)?init as init process",
            re.IGNORECASE,
        ),
    ),
    (
        "starting_init",
        re.compile(
            r"\bStarting init:",
            re.IGNORECASE,
        ),
    ),
    (
        "busybox_init",
        re.compile(
            r"init started:\s*BusyBox",
            re.IGNORECASE,
        ),
    ),
    (
        "login_prompt",
        re.compile(
            r"(?:^|\s)[A-Za-z0-9._-]+\s+login:\s*$",
            re.IGNORECASE,
        ),
    ),
    (
        "system_welcome",
        re.compile(
            r"^\s*Welcome to\s+",
            re.IGNORECASE,
        ),
    ),
)

_FAILURE_PATTERNS: tuple[
    tuple[str, re.Pattern[str]], ...
] = (
    (
        "kernel_panic",
        re.compile(
            r"Kernel panic\s*-\s*not syncing",
            re.IGNORECASE,
        ),
    ),
    (
        "rootfs_mount_failure",
        re.compile(
            r"VFS:\s*Unable to mount root fs",
            re.IGNORECASE,
        ),
    ),
    (
        "no_working_init",
        re.compile(
            r"No working init found",
            re.IGNORECASE,
        ),
    ),
    (
        "init_execution_failure",
        re.compile(
            r"Failed to execute\s+/(?:sbin/)?init",
            re.IGNORECASE,
        ),
    ),
    (
        "init_killed",
        re.compile(
            r"Attempted to kill init",
            re.IGNORECASE,
        ),
    ),
)


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _match_patterns(
    text: str,
    patterns: tuple[tuple[str, re.Pattern[str]], ...],
) -> tuple[str, ...]:
    matched = {
        name
        for name, pattern in patterns
        if pattern.search(text)
    }

    return tuple(sorted(matched))


def inspect_guest_console(
    *,
    contract_root: Path,
    max_examined_bytes: int = 32 * 1024 * 1024,
) -> GuestConsoleEvidence:
    if max_examined_bytes < 1:
        raise BootValidationError(
            "max_examined_bytes must be positive"
        )

    console_path = (
        contract_root.resolve()
        / "artifacts"
        / "boot"
        / "guest-console.log"
    )

    if not console_path.exists():
        return GuestConsoleEvidence(
            path=str(console_path),
            exists=False,
            size_bytes=None,
            sha256=None,
            bytes_examined=0,
            truncated=False,
            kernel_markers=(),
            userspace_markers=(),
            failure_markers=(),
            errors=(),
        )

    if not console_path.is_file():
        return GuestConsoleEvidence(
            path=str(console_path),
            exists=True,
            size_bytes=None,
            sha256=None,
            bytes_examined=0,
            truncated=False,
            kernel_markers=(),
            userspace_markers=(),
            failure_markers=(),
            errors=(
                "Guest console export is not a regular file",
            ),
        )

    digest = hashlib.sha256()
    examined = bytearray()
    errors: list[str] = []

    try:
        size_bytes = console_path.stat().st_size

        with console_path.open("rb") as handle:
            while True:
                block = handle.read(1024 * 1024)

                if not block:
                    break

                digest.update(block)

                remaining = (
                    max_examined_bytes
                    - len(examined)
                )

                if remaining > 0:
                    examined.extend(block[:remaining])

    except OSError as exc:
        errors.append(
            f"{type(exc).__name__}: {exc}"
        )

        return GuestConsoleEvidence(
            path=str(console_path),
            exists=True,
            size_bytes=None,
            sha256=None,
            bytes_examined=len(examined),
            truncated=False,
            kernel_markers=(),
            userspace_markers=(),
            failure_markers=(),
            errors=tuple(errors),
        )

    text = bytes(examined).decode(
        "utf-8",
        errors="replace",
    )

    return GuestConsoleEvidence(
        path=str(console_path),
        exists=True,
        size_bytes=size_bytes,
        sha256=digest.hexdigest(),
        bytes_examined=len(examined),
        truncated=size_bytes > max_examined_bytes,
        kernel_markers=_match_patterns(
            text,
            _KERNEL_PATTERNS,
        ),
        userspace_markers=_match_patterns(
            text,
            _USERSPACE_PATTERNS,
        ),
        failure_markers=_match_patterns(
            text,
            _FAILURE_PATTERNS,
        ),
        errors=tuple(errors),
    )


def _lifecycle_outcome(
    lifecycle: Any | None,
) -> str | None:
    if lifecycle is None:
        return None

    if isinstance(lifecycle, dict):
        value = lifecycle.get("run_outcome")
    else:
        value = getattr(
            lifecycle,
            "run_outcome",
            None,
        )

    return value if isinstance(value, str) else None


def _authenticity_status(
    record: Any,
) -> str | None:
    if isinstance(record, dict):
        measurement = record.get(
            "independent_measurement",
            {},
        )

        if isinstance(measurement, dict):
            status = measurement.get("status")
            return (
                status
                if isinstance(status, str)
                else None
            )

        return None

    measurement = getattr(
        record,
        "independent_measurement",
        None,
    )

    status = getattr(
        measurement,
        "status",
        None,
    )

    return status if isinstance(status, str) else None


def validate_boot_evidence(
    *,
    contract_root: Path,
    requested: bool,
    candidate_events: Iterable[dict[str, Any]],
    lifecycle: Any | None,
    authenticity_records: Iterable[Any],
) -> BootObservation:
    """
    Independently evaluate Linux boot/emulation success.

    candidate_boot_reported is retained as a claim only. It cannot set the
    independent result to true.
    """
    console = inspect_guest_console(
        contract_root=contract_root
    )

    candidate_claim = any(
        event.get("event")
        == "candidate_boot_reported"
        for event in candidate_events
    )

    authenticated_service_count = sum(
        _authenticity_status(record) == "true"
        for record in authenticity_records
    )

    lifecycle_outcome = _lifecycle_outcome(
        lifecycle
    )

    if not requested:
        return BootObservation(
            metric="boot_success",
            status="not_attempted",
            observed_at=_utc_now(),
            methods=(),
            candidate_boot_claim_present=(
                candidate_claim
            ),
            authenticated_service_count=(
                authenticated_service_count
            ),
            lifecycle_outcome=lifecycle_outcome,
            console=console,
            reason=(
                "The experiment did not request emulation"
            ),
        )

    if console.errors:
        return BootObservation(
            metric="boot_success",
            status="probe_error",
            observed_at=_utc_now(),
            methods=("guest_console",),
            candidate_boot_claim_present=(
                candidate_claim
            ),
            authenticated_service_count=(
                authenticated_service_count
            ),
            lifecycle_outcome=lifecycle_outcome,
            console=console,
            reason=(
                "VERITAS could not inspect the available guest "
                "console evidence"
            ),
        )

    methods: list[str] = []

    console_boot_verified = bool(
        console.kernel_markers
        and console.userspace_markers
    )

    if console_boot_verified:
        methods.append(
            "linux_kernel_and_userspace_console"
        )

    if authenticated_service_count > 0:
        methods.append(
            "authenticated_firmware_service"
        )

    if methods:
        return BootObservation(
            metric="boot_success",
            status="true",
            observed_at=_utc_now(),
            methods=tuple(methods),
            candidate_boot_claim_present=(
                candidate_claim
            ),
            authenticated_service_count=(
                authenticated_service_count
            ),
            lifecycle_outcome=lifecycle_outcome,
            console=console,
            reason=(
                "Independent guest-level evidence verified that "
                "firmware execution reached a booted userspace state"
            ),
        )

    if console.failure_markers:
        return BootObservation(
            metric="boot_success",
            status="false",
            observed_at=_utc_now(),
            methods=("guest_console_failure",),
            candidate_boot_claim_present=(
                candidate_claim
            ),
            authenticated_service_count=0,
            lifecycle_outcome=lifecycle_outcome,
            console=console,
            reason=(
                "The guest console contains explicit generic "
                "Linux boot-failure evidence and no successful "
                "userspace transition"
            ),
        )

    if lifecycle_outcome in {
        "experiment_timeout",
        "unexpected_early_exit",
    }:
        return BootObservation(
            metric="boot_success",
            status="false",
            observed_at=_utc_now(),
            methods=("lifecycle_observation",),
            candidate_boot_claim_present=(
                candidate_claim
            ),
            authenticated_service_count=0,
            lifecycle_outcome=lifecycle_outcome,
            console=console,
            reason=(
                "The run reached its terminal limit or exited "
                "early without independent guest-level boot evidence"
            ),
        )

    if console.truncated:
        reason = (
            "The guest console exceeded the inspection limit and "
            "contained insufficient boot evidence in the examined data"
        )

    elif candidate_claim:
        reason = (
            "The candidate reported boot, but VERITAS found no "
            "sufficient independent guest-level evidence"
        )

    elif console.exists:
        reason = (
            "A guest console was exported, but it contained "
            "insufficient generic Linux boot evidence"
        )

    else:
        reason = (
            "No independently verifiable guest console or "
            "authenticated firmware service was available"
        )

    return BootObservation(
        metric="boot_success",
        status="inconclusive",
        observed_at=_utc_now(),
        methods=(),
        candidate_boot_claim_present=candidate_claim,
        authenticated_service_count=0,
        lifecycle_outcome=lifecycle_outcome,
        console=console,
        reason=reason,
    )


def write_boot_evidence(
    *,
    contract_root: Path,
    observation: BootObservation,
) -> None:
    output_path = (
        contract_root.resolve()
        / "artifacts"
        / "boot-observation.json"
    )

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary_path = output_path.with_suffix(
        ".tmp"
    )

    temporary_path.write_text(
        json.dumps(
            observation.to_dict(),
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    temporary_path.replace(output_path)
