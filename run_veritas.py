#!/usr/bin/env python3
"""
VERITAS command-line runner.

This is the user-facing entry point for Adapter Contract v1. It discovers
validated adapter packages from adapters/, scans a firmware file or corpus,
and delegates every execution to the candidate-neutral run coordinator.

Examples:
    ./python run_veritas.py --firmware /path/to/corpus
    ./python run_veritas.py --firmware image.zip --candidates firmae
    ./python run_veritas.py --firmware corpus/ --candidates firmae,firmadyne
    ./python run_veritas.py --firmware corpus/ --experiment-id pilot-01 --dry-run
    ./python run_veritas.py --list-candidates
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import platform
import re
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from veritas_core.adapter_registry import (
    AdapterRecord,
    AdapterRegistryError,
    discover_adapters,
)
from veritas_core.docker_backend import DockerBackend, DockerBackendError
from veritas_core.run_coordinator import (
    CandidateRunCoordinator,
    RunCoordinatorError,
    RunPolicy,
    derive_candidate_stage_results,
)


PROJECT_ROOT = Path(__file__).resolve().parent
ADAPTERS_ROOT = PROJECT_ROOT / "adapters"
SCHEMAS_ROOT = PROJECT_ROOT / "schemas"
MANIFEST_SCHEMA = SCHEMAS_ROOT / "adapter-manifest-v1.schema.json"
REQUEST_SCHEMA = SCHEMAS_ROOT / "run-request-v1.schema.json"
EVENT_SCHEMA = SCHEMAS_ROOT / "adapter-event-v1.schema.json"
DEFAULT_RESULTS_ROOT = PROJECT_ROOT / "results" / "runs"

FIRMWARE_EXTENSIONS = {
    ".bin",
    ".zip",
    ".img",
    ".tar",
    ".gz",
    ".bz2",
    ".7z",
    ".trx",
    ".chk",
    ".dlf",
    ".w",
    ".blob",
}

IDENTIFIER_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
)


class CliError(RuntimeError):
    """Raised for a user-facing VERITAS CLI error."""


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def default_experiment_id() -> str:
    return datetime.now(timezone.utc).strftime(
        "veritas-%Y%m%dT%H%M%SZ"
    )


def safe_token(value: str, *, maximum: int = 128) -> str:
    token = re.sub(
        r"[^A-Za-z0-9._-]+",
        "-",
        value,
    ).strip("._-")

    if not token:
        raise CliError(
            f"Value cannot form a safe identifier: {value!r}"
        )

    return token[:maximum]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as handle:
        for block in iter(
            lambda: handle.read(1024 * 1024),
            b"",
        ):
            digest.update(block)

    return digest.hexdigest()


def write_json_atomic(
    path: Path,
    document: dict[str, Any],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def build_case(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    digest = sha256_file(resolved)
    stem = safe_token(resolved.stem.lower(), maximum=96)
    case_id = safe_token(
        f"{stem}-{digest[:12]}",
        maximum=128,
    )

    return {
        "case_id": case_id,
        "firmware_path": resolved,
        "filename": resolved.name,
        "sha256": digest,
        "size_bytes": resolved.stat().st_size,
    }


def scan_firmware(path_value: str) -> list[dict[str, Any]]:
    source = Path(path_value).expanduser().resolve()

    if source.is_file():
        # An explicitly supplied file is treated as an opaque firmware
        # object even when it has no extension. Directory scans remain
        # extension-filtered to avoid collecting unrelated files.
        if source.stat().st_size < 512:
            raise CliError(
                f"Explicit firmware file is too small: {source}"
            )
        return [build_case(source)]

    if not source.exists():
        raise CliError(f"Firmware path does not exist: {source}")

    if not source.is_dir():
        raise CliError(
            f"Firmware path is neither a file nor directory: {source}"
        )

    cases: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()

    for candidate in sorted(source.rglob("*")):
        if not candidate.is_file():
            continue
        if candidate.suffix.lower() not in FIRMWARE_EXTENSIONS:
            continue
        if candidate.stat().st_size < 512:
            continue

        case = build_case(candidate)

        if case["sha256"] in seen_hashes:
            print(
                f"[VERITAS] Skipping duplicate bytes: {candidate}"
            )
            continue

        seen_hashes.add(case["sha256"])
        cases.append(case)

    if not cases:
        raise CliError(
            f"No firmware files were found under: {source}"
        )

    return cases


def discover_validated_adapters() -> dict[str, AdapterRecord]:
    try:
        return discover_adapters(
            ADAPTERS_ROOT,
            MANIFEST_SCHEMA,
        )
    except AdapterRegistryError as exc:
        raise CliError(str(exc)) from exc


def select_adapters(
    all_adapters: dict[str, AdapterRecord],
    selection: str,
) -> dict[str, AdapterRecord]:
    if selection.strip().lower() == "all":
        return dict(all_adapters)

    requested = [
        item.strip()
        for item in selection.split(",")
        if item.strip()
    ]

    missing = [
        adapter_id
        for adapter_id in requested
        if adapter_id not in all_adapters
    ]

    if missing:
        raise CliError(
            "Unknown candidate adapter(s): "
            + ", ".join(missing)
            + ". Available: "
            + ", ".join(sorted(all_adapters))
        )

    return {
        adapter_id: all_adapters[adapter_id]
        for adapter_id in requested
    }


def parse_stages(value: str) -> tuple[str, ...]:
    stages = tuple(
        item.strip()
        for item in value.split(",")
        if item.strip()
    )

    if not stages:
        raise argparse.ArgumentTypeError(
            "At least one requested stage is required"
        )

    if len(set(stages)) != len(stages):
        raise argparse.ArgumentTypeError(
            "Requested stages must not contain duplicates"
        )

    return stages


def expected_run_directory(
    results_root: Path,
    *,
    experiment_id: str,
    case_id: str,
    adapter_id: str,
    attempt: int,
) -> Path:
    run_id = (
        f"{safe_token(experiment_id)}."
        f"{safe_token(case_id)}."
        f"{adapter_id}.attempt-{attempt}"
    )
    return results_root.resolve() / run_id


def status_value(
    document: dict[str, Any],
    *path: str,
) -> Any:
    current: Any = document

    for component in path:
        if not isinstance(current, dict):
            return None
        current = current.get(component)

    return current


def read_event_stream(event_path: Path) -> list[dict[str, Any]]:
    """Read complete JSONL events while tolerating a partial final line."""
    if not event_path.is_file():
        return []

    events: list[dict[str, Any]] = []

    try:
        lines = event_path.read_text(
            encoding="utf-8",
            errors="replace",
        ).splitlines()
    except OSError:
        return []

    for line in lines:
        if not line.strip():
            continue

        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            # The adapter may be in the middle of appending the last line.
            continue

        if isinstance(event, dict):
            events.append(event)

    return events


def format_duration(total_seconds: float) -> str:
    seconds = max(0, int(total_seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


def infer_live_phase(
    events: list[dict[str, Any]],
) -> str:
    """Infer only contract-level progress; never inspect candidate internals."""
    names = [
        event.get("event")
        for event in events
    ]

    if "error" in names:
        return "adapter error / controlled shutdown"

    latest_stage = next(
        (
            event
            for event in reversed(events)
            if event.get("event") == "stage_completed"
        ),
        None,
    )

    if isinstance(latest_stage, dict):
        stage = latest_stage.get("stage")
        outcome = latest_stage.get("stage_outcome")

        if outcome != "succeeded":
            return f"candidate {stage} outcome recorded"
        if stage == "endpoint-discovery":
            return "candidate stages completed / neutral shutdown"
        if stage == "emulate":
            return "endpoint discovery"
        if stage == "unpack":
            return "candidate emulation preparation"

    latest_state = None

    for event in reversed(events):
        state = event.get("state")

        if isinstance(state, str) and state:
            latest_state = state
            break

    if "candidate_boot_reported" in names:
        if "endpoint_reported" in names:
            return "independent endpoint measurement"
        if latest_state == "waiting_for_shutdown":
            return "candidate ready / awaiting neutral shutdown"
        return "boot reported / endpoint discovery"

    if "extraction_complete" in names:
        return "candidate emulation preparation"

    if "candidate_started" in names:
        return "candidate execution"

    if "adapter_started" in names:
        return "adapter setup"

    return "building adapter image"


def milestone_lines(
    events: list[dict[str, Any]],
    printed_sequences: set[int],
) -> list[str]:
    """Return permanent, human-readable lines for new lifecycle milestones."""
    stage_by_sequence = {
        record.get("event_sequence"): record
        for record in derive_candidate_stage_results(events)
    }
    lines: list[str] = []

    for event in events:
        sequence = event.get("sequence")

        if not isinstance(sequence, int):
            continue
        if sequence in printed_sequences:
            continue

        printed_sequences.add(sequence)
        event_name = event.get("event")

        if event_name == "adapter_started":
            lines.append("[VERITAS] ✓ Adapter started")

        elif event_name == "candidate_started":
            lines.append(
                "[VERITAS] → Candidate execution started"
            )

        elif event_name == "stage_completed":
            outcome = str(event.get("stage_outcome", "unknown"))
            symbol = {
                "succeeded": "✓",
                "failed": "✗",
                "inconclusive": "?",
                "not_applicable": "○",
            }.get(outcome, "•")
            stage = str(event.get("stage", "unknown"))
            message = str(event.get("message", "")).strip()
            stage_record = stage_by_sequence.get(sequence, {})
            elapsed = stage_record.get("elapsed_seconds")
            duration = (
                f" in {format_duration(float(elapsed))}"
                if isinstance(elapsed, (int, float))
                else ""
            )
            detail = f" — {message}" if message else ""
            lines.append(
                f"[VERITAS] {symbol} Stage {stage} {outcome}"
                f"{duration}{detail}"
            )

        elif event_name == "endpoint_reported":
            endpoint = event.get("endpoint", {})
            if isinstance(endpoint, dict):
                protocol = endpoint.get("protocol", "tcp")
                host = endpoint.get("host", "unknown")
                port = endpoint.get("port", "unknown")
                lines.append(
                    "[VERITAS] → Candidate endpoint claim: "
                    f"{protocol}://{host}:{port}"
                )

        elif event_name == "shutdown_started":
            lines.append("[VERITAS] → Controlled shutdown started")

        elif event_name == "cleanup_complete":
            lines.append("[VERITAS] ✓ Adapter cleanup completed")

        elif event_name == "adapter_stopped":
            outcome = event.get("outcome", "unknown")
            symbol = "✓" if outcome == "completed" else "✗"
            lines.append(
                f"[VERITAS] {symbol} Adapter stopped: {outcome}"
            )

    return lines


def print_milestones(
    events: list[dict[str, Any]],
    printed_sequences: set[int],
) -> None:
    lines = milestone_lines(events, printed_sequences)

    if not lines:
        return

    print("\r" + (" " * 160) + "\r", end="")
    for line in lines:
        print(line)


def unrecoverable_adapter_error(
    events: list[dict[str, Any]],
) -> dict[str, Any] | None:
    for event in reversed(events):
        if event.get("event") != "error":
            continue

        error = event.get("error")

        if not isinstance(error, dict):
            return event

        if error.get("recoverable") is not True:
            return event

    return None


def request_error_shutdown(
    run_directory: Path,
    error_event: dict[str, Any],
) -> None:
    control_path = (
        run_directory
        / "contract"
        / "control"
        / "shutdown.json"
    )

    if control_path.exists():
        return

    error = error_event.get("error")
    error_code = (
        error.get("code")
        if isinstance(error, dict)
        else None
    )

    write_json_atomic(
        control_path,
        {
            "command": "shutdown",
            "reason": "unrecoverable-adapter-error",
            "requested_at": utc_now(),
            "error_code": error_code,
        },
    )


def execute_with_live_progress(
    *,
    coordinator: CandidateRunCoordinator,
    adapter: AdapterRecord,
    firmware_path: Path,
    case_id: str,
    policy: RunPolicy,
    trusted_content_sha256: set[str],
    run_directory: Path,
) -> dict[str, Any]:
    """Run one coordinator execution while displaying neutral progress."""
    event_path = (
        run_directory
        / "contract"
        / "events"
        / "events.jsonl"
    )

    submitted_at = time.monotonic()
    adapter_started_at: datetime | None = None
    shutdown_requested_for_error = False
    printed_milestone_sequences: set[int] = set()

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix="veritas-run",
    ) as executor:
        future = executor.submit(
            coordinator.execute,
            adapter=adapter,
            firmware_path=firmware_path,
            case_id=case_id,
            policy=policy,
            trusted_content_sha256=trusted_content_sha256,
        )

        while not future.done():
            events = read_event_stream(event_path)
            print_milestones(
                events,
                printed_milestone_sequences,
            )

            if events and adapter_started_at is None:
                started = next(
                    (
                        event
                        for event in events
                        if event.get("event") == "adapter_started"
                    ),
                    None,
                )

                if isinstance(started, dict):
                    timestamp = started.get("timestamp")

                    if isinstance(timestamp, str):
                        try:
                            adapter_started_at = datetime.fromisoformat(
                                timestamp.replace("Z", "+00:00")
                            )
                        except ValueError:
                            adapter_started_at = None

            fatal_event = unrecoverable_adapter_error(events)

            if (
                fatal_event is not None
                and not shutdown_requested_for_error
            ):
                request_error_shutdown(
                    run_directory,
                    fatal_event,
                )
                shutdown_requested_for_error = True

                error = fatal_event.get("error", {})
                code = (
                    error.get("code")
                    if isinstance(error, dict)
                    else "unknown"
                )
                message = (
                    error.get("message")
                    if isinstance(error, dict)
                    else "Adapter reported an unrecoverable error"
                )

                print()
                print(
                    f"[VERITAS] Adapter error: {code}: {message}",
                    file=sys.stderr,
                )
                print(
                    "[VERITAS] Controlled shutdown requested immediately.",
                    file=sys.stderr,
                )

            now_utc = datetime.now(timezone.utc)

            if adapter_started_at is not None:
                elapsed = max(
                    0.0,
                    (now_utc - adapter_started_at).total_seconds(),
                )
                remaining = max(
                    0.0,
                    float(policy.timeout_seconds) - elapsed,
                )
                timing = (
                    f"{format_duration(elapsed)} elapsed | "
                    f"{format_duration(remaining)} remaining"
                )
            else:
                build_elapsed = time.monotonic() - submitted_at
                timing = (
                    f"{format_duration(build_elapsed)} build/setup elapsed | "
                    f"{format_duration(policy.timeout_seconds)} execution budget"
                )

            phase = infer_live_phase(events)

            latest_name = (
                str(events[-1].get("event"))
                if events
                else "none"
            )
            line = (
                f"[VERITAS] {timing} | "
                f"phase={phase} | latest={latest_name}"
            )
            print(
                "\r" + line.ljust(150),
                end="",
                flush=True,
            )

            time.sleep(2.0)

        final_events = read_event_stream(event_path)
        print_milestones(
            final_events,
            printed_milestone_sequences,
        )
        print()
        return future.result()


def summarise_result(result: dict[str, Any]) -> str:
    measurements = result.get(
        "independent_measurements",
        {},
    )

    reachability = measurements.get("reachability", [])
    reachability_statuses = [
        status_value(
            record,
            "independent_measurement",
            "status",
        )
        for record in reachability
        if isinstance(record, dict)
    ]

    if not reachability_statuses:
        reachable = "not_attempted"
    elif "true" in reachability_statuses:
        reachable = "true"
    elif "false" in reachability_statuses:
        reachable = "false"
    elif "inconclusive" in reachability_statuses:
        reachable = "inconclusive"
    else:
        reachable = reachability_statuses[0]

    return (
        f"status={result.get('overall_status')} "
        f"unpack={status_value(measurements, 'unpack', 'status')} "
        f"boot={status_value(measurements, 'boot', 'status')} "
        f"reachable={reachable}"
    )


def warn_about_emulated_host() -> None:
    machine = platform.machine().lower()

    if machine in {"aarch64", "arm64"}:
        print(
            "[VERITAS] NOTE: this ARM64 host can be used for functional "
            "adapter testing, but linux/amd64 candidate images run through "
            "CPU emulation. Do not use these compute-cost figures for the "
            "final cross-tool paper comparison."
        )


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "VERITAS — candidate-neutral firmware benchmark runner"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  ./python run_veritas.py --firmware /path/to/corpus
  ./python run_veritas.py --firmware image.zip --candidates firmae
  ./python run_veritas.py --firmware corpus --candidate firmae
  ./python run_veritas.py --firmware corpus --experiment-id pilot-01 --dry-run
  ./python run_veritas.py --list-candidates
        """,
    )

    parser.add_argument(
        "--firmware",
        help="Firmware file or directory containing a corpus",
    )
    parser.add_argument(
        "--candidates",
        "--candidate",
        dest="candidates",
        default="all",
        help=(
            "Comma-separated adapter IDs, or 'all'. The singular "
            "--candidate alias is retained for convenience."
        ),
    )
    parser.add_argument(
        "--experiment-id",
        default=None,
        help=(
            "Stable experiment identifier. Defaults to a UTC timestamp."
        ),
    )
    parser.add_argument(
        "--attempt",
        type=int,
        default=1,
        help="Attempt number used in the immutable run identity",
    )
    parser.add_argument(
        "--stages",
        type=parse_stages,
        default=(
            "unpack",
            "emulate",
            "endpoint-discovery",
        ),
        help=(
            "Comma-separated stages (default: "
            "unpack,emulate,endpoint-discovery)"
        ),
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=10800,
        help="Candidate execution timeout in seconds (default: 10800)",
    )
    parser.add_argument(
        "--boot-wait",
        type=float,
        default=900.0,
        help=(
            "Maximum candidate-reported boot/readiness wait in seconds "
            "(default: 900)"
        ),
    )
    parser.add_argument(
        "--endpoint-wait",
        type=float,
        default=300.0,
        help=(
            "Bounded candidate endpoint-discovery window after boot "
            "readiness (default: 300 seconds)"
        ),
    )
    parser.add_argument(
        "--heartbeat-interval",
        type=int,
        default=30,
    )
    parser.add_argument(
        "--heartbeat-timeout",
        type=int,
        default=90,
    )
    parser.add_argument(
        "--shutdown-grace",
        type=int,
        default=60,
    )
    parser.add_argument(
        "--cpu-cores",
        type=float,
        default=4.0,
    )
    parser.add_argument(
        "--memory-gb",
        type=float,
        default=8.0,
    )
    parser.add_argument(
        "--pids-limit",
        type=int,
        default=4096,
    )
    parser.add_argument(
        "--stability-samples",
        type=int,
        default=5,
    )
    parser.add_argument(
        "--stability-interval",
        type=float,
        default=10.0,
    )
    parser.add_argument(
        "--stability-probe-timeout",
        type=float,
        default=3.0,
    )
    parser.add_argument(
        "--compute-sample-interval",
        type=float,
        default=1.0,
    )
    parser.add_argument(
        "--results-root",
        default=str(DEFAULT_RESULTS_ROOT),
        help="Neutral run-result root (default: results/runs)",
    )
    parser.add_argument(
        "--trusted-content-sha256",
        action="append",
        default=[],
        help=(
            "Trusted HTTP content SHA-256; may be provided repeatedly"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show immutable run identities without building or running",
    )
    parser.add_argument(
        "--list-candidates",
        "--list-adapters",
        dest="list_candidates",
        action="store_true",
        help="List validated Adapter Contract v1 packages",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop the batch after the first coordinator exception",
    )

    return parser


def list_adapters(adapters: dict[str, AdapterRecord]) -> None:
    if not adapters:
        print("No validated adapters were discovered under adapters/.")
        return

    print(f"Discovered {len(adapters)} validated adapter(s):\n")

    for adapter_id, record in sorted(adapters.items()):
        manifest = record.manifest
        adapter = manifest["adapter"]
        candidate = manifest["candidate"]
        build = manifest["build"]
        capabilities = manifest["capabilities"]

        print(f"  {adapter_id}")
        print(f"    Display name: {adapter['display_name']}")
        print(f"    Version:      {adapter['version']}")
        print(f"    Candidate:    {candidate['name']}")
        print(f"    Platform:     {build['platform']}")
        print(
            "    Stages:       "
            + ", ".join(capabilities["stages"])
        )
        print(f"    Manifest:     {record.manifest_path}")
        print()


def validate_arguments(args: argparse.Namespace) -> str:
    experiment_id = (
        args.experiment_id or default_experiment_id()
    )

    if not IDENTIFIER_PATTERN.fullmatch(experiment_id):
        raise CliError(
            "experiment-id must match "
            "^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
        )

    if args.attempt < 1:
        raise CliError("attempt must be at least one")

    if args.memory_gb <= 0:
        raise CliError("memory-gb must be positive")

    for digest in args.trusted_content_sha256:
        if not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            raise CliError(
                f"Invalid trusted SHA-256 value: {digest}"
            )

    return experiment_id


def main() -> int:
    parser = create_parser()
    args = parser.parse_args()

    try:
        all_adapters = discover_validated_adapters()

        if args.list_candidates:
            list_adapters(all_adapters)
            return 0

        if not args.firmware:
            parser.print_help()
            return 2

        experiment_id = validate_arguments(args)
        selected_adapters = select_adapters(
            all_adapters,
            args.candidates,
        )
        cases = scan_firmware(args.firmware)
        results_root = Path(args.results_root).expanduser().resolve()

        policy = RunPolicy(
            experiment_id=experiment_id,
            attempt=args.attempt,
            requested_stages=tuple(args.stages),
            timeout_seconds=args.timeout,
            heartbeat_interval_seconds=(
                args.heartbeat_interval
            ),
            heartbeat_timeout_seconds=(
                args.heartbeat_timeout
            ),
            shutdown_grace_seconds=args.shutdown_grace,
            boot_wait_timeout_seconds=args.boot_wait,
            cpu_cores=args.cpu_cores,
            memory_bytes=int(
                args.memory_gb * 1024 * 1024 * 1024
            ),
            pids_limit=args.pids_limit,
            endpoint_wait_timeout_seconds=(
                args.endpoint_wait
            ),
            stability_sample_count=(
                args.stability_samples
            ),
            stability_interval_seconds=(
                args.stability_interval
            ),
            stability_probe_timeout_seconds=(
                args.stability_probe_timeout
            ),
            compute_sample_interval_seconds=(
                args.compute_sample_interval
            ),
        )

    except (CliError, ValueError) as exc:
        print(f"[VERITAS] ERROR: {exc}", file=sys.stderr)
        return 2

    total_runs = len(cases) * len(selected_adapters)

    print()
    print(f"[VERITAS] Experiment      : {experiment_id}")
    print(f"[VERITAS] Firmware images : {len(cases)}")
    print(
        "[VERITAS] Candidates      : "
        f"{list(selected_adapters)}"
    )
    print(f"[VERITAS] Total runs      : {total_runs}")
    print(f"[VERITAS] Attempt         : {args.attempt}")
    print(
        "[VERITAS] Stages          : "
        + ", ".join(policy.requested_stages)
    )
    print(f"[VERITAS] Timeout/run     : {args.timeout}s")
    print(f"[VERITAS] Boot wait       : {args.boot_wait:.0f}s")
    print(f"[VERITAS] Results root    : {results_root}")

    warn_about_emulated_host()

    invocation_timestamp = datetime.now(
        timezone.utc
    ).strftime("%Y%m%dT%H%M%SZ")

    invocation_path = (
        results_root
        / "_experiments"
        / safe_token(experiment_id)
        / "invocations"
        / (
            f"{invocation_timestamp}."
            f"attempt-{args.attempt}.json"
        )
    )

    invocation: dict[str, Any] = {
        "schema_version": "1.0",
        "created_at": utc_now(),
        "experiment_id": experiment_id,
        "policy": policy.to_dict(),
        "host": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": sys.version,
        },
        "adapters": [
            {
                "adapter_id": record.adapter_id,
                "manifest_path": str(record.manifest_path),
                "manifest_sha256": record.manifest_sha256,
            }
            for record in selected_adapters.values()
        ],
        "cases": [
            {
                **{
                    key: value
                    for key, value in case.items()
                    if key != "firmware_path"
                },
                "firmware_path": str(case["firmware_path"]),
            }
            for case in cases
        ],
        "planned_runs": total_runs,
        "records": [],
    }
    write_json_atomic(invocation_path, invocation)

    if args.dry_run:
        print("\n[VERITAS] Mode: DRY RUN\n")

        for case in cases:
            for adapter_id in selected_adapters:
                run_directory = expected_run_directory(
                    results_root,
                    experiment_id=experiment_id,
                    case_id=case["case_id"],
                    adapter_id=adapter_id,
                    attempt=args.attempt,
                )
                print(
                    f"  {adapter_id} <- {case['filename']}\n"
                    f"    case_id: {case['case_id']}\n"
                    f"    result:  {run_directory}"
                )

        return 0

    try:
        backend = DockerBackend(REQUEST_SCHEMA)

        if not backend.docker_available():
            raise CliError(
                "Docker daemon is unavailable. Run 'docker info' "
                "and correct access before benchmarking."
            )

        coordinator = CandidateRunCoordinator(
            backend=backend,
            event_schema_path=EVENT_SCHEMA,
            results_root=results_root,
        )

    except (DockerBackendError, CliError) as exc:
        print(f"[VERITAS] ERROR: {exc}", file=sys.stderr)
        return 2

    saved = 0
    skipped = 0
    coordinator_errors = 0
    non_completed = 0

    for case in cases:
        for adapter_id, adapter in selected_adapters.items():
            run_directory = expected_run_directory(
                results_root,
                experiment_id=experiment_id,
                case_id=case["case_id"],
                adapter_id=adapter_id,
                attempt=args.attempt,
            )
            final_result_path = run_directory / "final-result.json"

            if final_result_path.is_file():
                existing = json.loads(
                    final_result_path.read_text(
                        encoding="utf-8"
                    )
                )
                print(
                    f"[VERITAS] SKIP {adapter_id} / "
                    f"{case['case_id']}: immutable result already "
                    f"exists ({existing.get('overall_status')})"
                )
                skipped += 1
                continue

            if run_directory.exists():
                print(
                    f"[VERITAS] ERROR {adapter_id} / "
                    f"{case['case_id']}: incomplete run directory "
                    f"already exists: {run_directory}. Inspect it or "
                    f"use --attempt {args.attempt + 1}.",
                    file=sys.stderr,
                )
                coordinator_errors += 1

                if args.fail_fast:
                    break
                continue

            print("\n" + "=" * 72)
            print(
                f"[VERITAS] {adapter_id} <- {case['filename']}"
            )
            print(
                f"[VERITAS] case_id={case['case_id']} "
                f"attempt={args.attempt}"
            )
            print("=" * 72)

            try:
                result = execute_with_live_progress(
                    coordinator=coordinator,
                    adapter=adapter,
                    firmware_path=case["firmware_path"],
                    case_id=case["case_id"],
                    policy=policy,
                    trusted_content_sha256={
                        value.lower()
                        for value in (
                            args.trusted_content_sha256
                        )
                    },
                    run_directory=run_directory,
                )

                saved += 1

                if result.get("overall_status") != "completed":
                    non_completed += 1

                print(
                    "[VERITAS] Result: "
                    + summarise_result(result)
                )
                print(
                    "[VERITAS] Saved : "
                    + str(
                        Path(
                            result["run"]["result_directory"]
                        )
                        / "final-result.json"
                    )
                )

                invocation["records"].append(
                    {
                        "case_id": case["case_id"],
                        "adapter_id": adapter_id,
                        "attempt": args.attempt,
                        "overall_status": result.get(
                            "overall_status"
                        ),
                        "result_directory": result["run"][
                            "result_directory"
                        ],
                        "recorded_at": utc_now(),
                    }
                )
                write_json_atomic(
                    invocation_path,
                    invocation,
                )

            except (
                RunCoordinatorError,
                DockerBackendError,
                OSError,
            ) as exc:
                coordinator_errors += 1
                print(
                    f"[VERITAS] COORDINATOR ERROR — {adapter_id} / "
                    f"{case['case_id']}: {exc}",
                    file=sys.stderr,
                )

                invocation["records"].append(
                    {
                        "case_id": case["case_id"],
                        "adapter_id": adapter_id,
                        "attempt": args.attempt,
                        "overall_status": "coordinator_exception",
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                        "recorded_at": utc_now(),
                    }
                )
                write_json_atomic(
                    invocation_path,
                    invocation,
                )

                if args.fail_fast:
                    break

        if args.fail_fast and coordinator_errors:
            break

    invocation["completed_at"] = utc_now()
    invocation["summary"] = {
        "saved": saved,
        "skipped": skipped,
        "non_completed_results": non_completed,
        "coordinator_errors": coordinator_errors,
    }
    write_json_atomic(invocation_path, invocation)

    print("\n[VERITAS] Batch complete")
    print(f"[VERITAS] Saved results       : {saved}")
    print(f"[VERITAS] Existing skipped    : {skipped}")
    print(f"[VERITAS] Non-completed runs  : {non_completed}")
    print(f"[VERITAS] Coordinator errors  : {coordinator_errors}")
    print(f"[VERITAS] Invocation manifest : {invocation_path}")
    print()
    print(
        "[VERITAS] Aggregate this experiment with:\n"
        f"  ./python score_aggregator.py --experiment-id "
        f"{experiment_id}"
    )

    return 1 if coordinator_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
