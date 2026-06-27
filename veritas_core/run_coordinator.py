from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from veritas_core.adapter_registry import AdapterRecord
from veritas_core.compute_cost import (
    ComputeCostObservation,
    DockerComputeCostSampler,
)
from veritas_core.docker_backend import (
    BuiltAdapterImage,
    BuiltProbeImage,
    DockerBackend,
)
from veritas_core.docker_supervisor import (
    DockerAdapterSupervisor,
)
from veritas_core.docker_namespace_probe import (
    DockerNamespaceProbeSidecar,
)
from veritas_core.environment_janitor import (
    EnvironmentJanitor,
    RemediationObservation,
    build_remediation_plan,
)
from veritas_core.environment_residue import (
    HostEnvironmentCollector,
    ResidueObservation,
    evaluate_environment_residue,
    write_environment_evidence,
)
from veritas_core.lifecycle_watchdog import (
    LifecycleObservation,
    LifecycleWatchdog,
    LifecycleWatchdogError,
)
from veritas_core.probe_orchestrator import (
    IndependentProbeOrchestrator,
)
from veritas_core.probes.unpack_validation import (
    UnpackObservation,
    validate_unpack_export,
    write_unpack_evidence,
)
from veritas_core.probes.boot_validation import (
    BootObservation,
    validate_boot_evidence,
    write_boot_evidence,
)


class RunCoordinatorError(RuntimeError):
    """Raised when a neutral VERITAS run cannot be prepared."""


@dataclass(frozen=True)
class RunPolicy:
    experiment_id: str
    attempt: int
    requested_stages: tuple[str, ...]

    timeout_seconds: int = 10800
    heartbeat_interval_seconds: int = 30
    heartbeat_timeout_seconds: int = 90
    shutdown_grace_seconds: int = 60
    boot_wait_timeout_seconds: float = 900.0

    cpu_cores: float = 4.0
    memory_bytes: int = 8589934592
    pids_limit: int = 4096

    endpoint_wait_timeout_seconds: float = 300.0

    stability_sample_count: int = 5
    stability_interval_seconds: float = 10.0
    stability_probe_timeout_seconds: float = 3.0

    compute_sample_interval_seconds: float = 1.0

    def __post_init__(self) -> None:
        if not self.experiment_id:
            raise ValueError(
                "experiment_id must not be empty"
            )

        if self.attempt < 1:
            raise ValueError(
                "attempt must be at least one"
            )

        if not self.requested_stages:
            raise ValueError(
                "requested_stages must not be empty"
            )

        if self.timeout_seconds < 1:
            raise ValueError(
                "timeout_seconds must be positive"
            )

        if self.heartbeat_interval_seconds < 1:
            raise ValueError(
                "heartbeat_interval_seconds must be positive"
            )

        if (
            self.heartbeat_timeout_seconds
            <= self.heartbeat_interval_seconds
        ):
            raise ValueError(
                "heartbeat_timeout_seconds must be greater "
                "than heartbeat_interval_seconds"
            )

        if self.shutdown_grace_seconds < 1:
            raise ValueError(
                "shutdown_grace_seconds must be positive"
            )

        if self.boot_wait_timeout_seconds <= 0:
            raise ValueError(
                "boot_wait_timeout_seconds must be positive"
            )

        if self.cpu_cores <= 0:
            raise ValueError(
                "cpu_cores must be positive"
            )

        if self.memory_bytes < 1048576:
            raise ValueError(
                "memory_bytes must be at least 1 MiB"
            )

        if self.pids_limit < 1:
            raise ValueError(
                "pids_limit must be positive"
            )

        if self.endpoint_wait_timeout_seconds <= 0:
            raise ValueError(
                "endpoint_wait_timeout_seconds must be positive"
            )

        if self.stability_sample_count < 1:
            raise ValueError(
                "stability_sample_count must be positive"
            )

        if self.stability_interval_seconds < 0:
            raise ValueError(
                "stability_interval_seconds cannot be negative"
            )

        if self.stability_probe_timeout_seconds <= 0:
            raise ValueError(
                "stability_probe_timeout_seconds must be positive"
            )

        if self.compute_sample_interval_seconds <= 0:
            raise ValueError(
                "compute_sample_interval_seconds must be positive"
            )

    def to_dict(self) -> dict[str, Any]:
        document = asdict(self)
        document["requested_stages"] = list(
            self.requested_stages
        )
        return document


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _safe_token(value: str) -> str:
    token = re.sub(
        r"[^A-Za-z0-9._-]+",
        "-",
        value,
    ).strip("._-")

    if not token:
        raise RunCoordinatorError(
            f"Value cannot form a safe identifier: {value!r}"
        )

    return token[:128]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as handle:
        for block in iter(
            lambda: handle.read(1024 * 1024),
            b"",
        ):
            digest.update(block)

    return digest.hexdigest()


def _write_json_atomic(
    path: Path,
    document: dict[str, Any],
) -> str:
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary_path = path.with_suffix(
        path.suffix + ".tmp"
    )

    temporary_path.write_text(
        json.dumps(
            document,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    temporary_path.replace(path)

    return _sha256_file(path)


def _not_attempted(reason: str) -> dict[str, Any]:
    return {
        "status": "not_attempted",
        "reason": reason,
    }


def _observation_to_dict(
    observation: Any | None,
    *,
    missing_reason: str,
) -> dict[str, Any]:
    if observation is None:
        return _not_attempted(missing_reason)

    return observation.to_dict()


def _parse_event_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None

    try:
        return datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )
    except ValueError:
        return None




def candidate_stage_blocks_following_work(
    event: dict[str, Any],
    stages: set[str],
) -> bool:
    """Return whether a stage outcome prevents later candidate work.

    An inconclusive emulation outcome does not block endpoint discovery
    when the candidate runtime is still alive. It means the adapter did
    not obtain its own network-readiness signal; independent boot
    validation and bounded endpoint discovery may still produce useful
    evidence.
    """
    if (
        event.get("event") != "stage_completed"
        or event.get("stage") not in stages
    ):
        return False

    outcome = event.get("stage_outcome")
    stage = event.get("stage")

    if stage == "emulate" and outcome == "inconclusive":
        return False

    return outcome != "succeeded"


def derive_candidate_stage_results(
    events: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Derive candidate-reported stage outcomes and elapsed times."""
    ordered = list(events)
    starts: dict[str, str] = {}
    results: list[dict[str, Any]] = []

    for event in ordered:
        event_name = event.get("event")
        timestamp = event.get("timestamp")

        if (
            event_name == "candidate_started"
            and isinstance(timestamp, str)
        ):
            starts.setdefault("unpack", timestamp)
            continue

        if event_name != "stage_completed":
            continue

        stage = event.get("stage")
        outcome = event.get("stage_outcome")
        message = event.get("message")

        if not isinstance(stage, str):
            continue

        started_at = starts.get(stage)
        completed_at = (
            timestamp if isinstance(timestamp, str) else None
        )
        elapsed_seconds: float | None = None

        started_dt = _parse_event_timestamp(started_at)
        completed_dt = _parse_event_timestamp(completed_at)

        if started_dt is not None and completed_dt is not None:
            elapsed_seconds = round(
                max(
                    0.0,
                    (completed_dt - started_dt).total_seconds(),
                ),
                6,
            )

        result = {
            "stage": stage,
            "outcome": outcome,
            "message": message,
            "started_at": started_at,
            "completed_at": completed_at,
            "elapsed_seconds": elapsed_seconds,
            "event_sequence": event.get("sequence"),
        }
        results.append(result)

        if completed_at is not None:
            if stage == "unpack" and outcome == "succeeded":
                starts.setdefault("emulate", completed_at)
            elif (
                stage == "emulate"
                and outcome in {"succeeded", "inconclusive"}
            ):
                starts.setdefault(
                    "endpoint-discovery",
                    completed_at,
                )

    return results


def _stage_result(
    results: Iterable[dict[str, Any]],
    stage: str,
) -> dict[str, Any] | None:
    for result in results:
        if result.get("stage") == stage:
            return result
    return None


class CandidateRunCoordinator:
    """
    Coordinate one candidate-neutral firmware execution.

    Candidate identifiers are treated only as manifest and request data.
    """

    def __init__(
        self,
        *,
        backend: DockerBackend,
        event_schema_path: Path,
        results_root: Path,
        environment_collector: (
            HostEnvironmentCollector | None
        ) = None,
    ) -> None:
        self.backend = backend
        self.event_schema_path = (
            event_schema_path.resolve()
        )
        self.results_root = results_root.resolve()
        self.environment_collector = (
            environment_collector
            or HostEnvironmentCollector()
        )

    def execute(
        self,
        *,
        adapter: AdapterRecord,
        firmware_path: Path,
        case_id: str,
        policy: RunPolicy,
        trusted_content_sha256: Iterable[str] = (),
    ) -> dict[str, Any]:
        firmware_path = firmware_path.resolve()

        if not firmware_path.is_file():
            raise RunCoordinatorError(
                f"Firmware input does not exist: "
                f"{firmware_path}"
            )

        safe_experiment = _safe_token(
            policy.experiment_id
        )
        safe_case = _safe_token(case_id)

        run_id = (
            f"{safe_experiment}."
            f"{safe_case}."
            f"{adapter.adapter_id}."
            f"attempt-{policy.attempt}"
        )

        run_directory = (
            self.results_root / run_id
        )

        if run_directory.exists():
            raise RunCoordinatorError(
                f"Run directory already exists: "
                f"{run_directory}. "
                "VERITAS will not mix historical and new results."
            )

        contract_root = run_directory / "contract"
        input_directory = contract_root / "input"
        artifact_directory = contract_root / "artifacts"

        for directory in (
            input_directory,
            contract_root / "work",
            artifact_directory,
            contract_root / "events",
            contract_root / "control",
        ):
            directory.mkdir(
                parents=True,
                exist_ok=False,
            )

        canonical_firmware = (
            input_directory / "firmware"
        )

        shutil.copyfile(
            firmware_path,
            canonical_firmware,
        )

        firmware_sha256 = _sha256_file(
            canonical_firmware
        )
        firmware_size = (
            canonical_firmware.stat().st_size
        )

        experiment_manifest = {
            "schema_version": "1.0",
            "experiment_id": policy.experiment_id,
            "created_at": _utc_now(),
            "input_policy": {
                "delivery_semantics": (
                    "opaque-original-bytes"
                ),
                "canonical_filename": "firmware",
                "candidate_input_read_only": True,
            },
            "execution_policy": policy.to_dict(),
            "case_ids": [case_id],
            "adapter_ids": [
                adapter.adapter_id
            ],
        }

        experiment_manifest_path = (
            run_directory
            / "experiment-manifest.json"
        )

        experiment_manifest_sha256 = (
            _write_json_atomic(
                experiment_manifest_path,
                experiment_manifest,
            )
        )

        runtime_manifest = adapter.manifest["runtime"]

        request = {
            "schema_version": "1.0",
            "contract_version": "1.0",
            "run": {
                "experiment_id": policy.experiment_id,
                "run_id": run_id,
                "adapter_id": adapter.adapter_id,
                "attempt": policy.attempt,
                "created_at": _utc_now(),
                "requested_stages": list(
                    policy.requested_stages
                ),
            },
            "firmware": {
                "case_id": case_id,
                "path": "/veritas/input/firmware",
                "sha256": firmware_sha256,
                "size_bytes": firmware_size,
                "delivery_semantics": (
                    "opaque-original-bytes"
                ),
                "read_only": True,
            },
            "lifecycle": {
                "timeout_seconds": (
                    policy.timeout_seconds
                ),
                "heartbeat_interval_seconds": (
                    policy.heartbeat_interval_seconds
                ),
                "heartbeat_timeout_seconds": (
                    policy.heartbeat_timeout_seconds
                ),
                "shutdown_grace_seconds": (
                    policy.shutdown_grace_seconds
                ),
                "boot_wait_timeout_seconds": (
                    policy.boot_wait_timeout_seconds
                ),
                "endpoint_wait_timeout_seconds": (
                    policy.endpoint_wait_timeout_seconds
                ),
            },
            "resources": {
                "cpu_cores": policy.cpu_cores,
                "memory_bytes": policy.memory_bytes,
                "pids_limit": policy.pids_limit,
            },
            "runtime_grants": {
                "run_as_root": runtime_manifest[
                    "run_as_root"
                ],
                "network": runtime_manifest[
                    "network"
                ],
                "requirements": list(
                    runtime_manifest["requirements"]
                ),
            },
            "paths": {
                "workspace": "/veritas/work",
                "artifacts": "/veritas/artifacts",
                "events": (
                    "/veritas/events/events.jsonl"
                ),
                "control": "/veritas/control",
            },
            "integrity": {
                "adapter_manifest_sha256": (
                    adapter.manifest_sha256
                ),
                "experiment_manifest_sha256": (
                    experiment_manifest_sha256
                ),
            },
        }

        request_path = (
            input_directory / "request.json"
        )

        request_sha256 = _write_json_atomic(
            request_path,
            request,
        )

        # Docker also mounts the complete input directory read-only.
        canonical_firmware.chmod(0o444)
        request_path.chmod(0o444)

        base_result: dict[str, Any] = {
            "schema_version": "1.0",
            "benchmark": "VERITAS",
            "run": {
                "run_id": run_id,
                "experiment_id": policy.experiment_id,
                "case_id": case_id,
                "adapter_id": adapter.adapter_id,
                "attempt": policy.attempt,
                "result_directory": str(
                    run_directory
                ),
            },
            "created_at": _utc_now(),
        }

        image: BuiltAdapterImage | None = None
        probe_image: BuiltProbeImage | None = None
        build_duration_seconds: float | None = None
        probe_build_duration_seconds: (
            float | None
        ) = None
        setup_error: dict[str, Any] | None = None

        build_started = time.monotonic()

        try:
            image = self.backend.build_adapter(
                adapter
            )
            build_duration_seconds = round(
                time.monotonic() - build_started,
                6,
            )

            if runtime_manifest["network"] == "none":
                probe_build_started = (
                    time.monotonic()
                )
                probe_image = (
                    self.backend
                    .build_neutral_probe_image()
                )
                probe_build_duration_seconds = round(
                    (
                        time.monotonic()
                        - probe_build_started
                    ),
                    6,
                )

        except Exception as exc:
            if build_duration_seconds is None:
                build_duration_seconds = round(
                    time.monotonic() - build_started,
                    6,
                )

            setup_error = {
                "phase": (
                    "neutral_probe_image_build"
                    if image is not None
                    and runtime_manifest[
                        "network"
                    ] == "none"
                    and probe_image is None
                    else "adapter_image_build"
                ),
                "error_type": type(exc).__name__,
                "message": str(exc),
            }

        if (
            image is None
            or (
                runtime_manifest["network"] == "none"
                and probe_image is None
            )
        ):
            result = {
                **base_result,
                "overall_status": "setup_failed",
                "setup": {
                    "status": "failed",
                    "build_duration_seconds": (
                        build_duration_seconds
                    ),
                    "probe_build_duration_seconds": (
                        probe_build_duration_seconds
                    ),
                    "error": setup_error,
                },
                "candidate_claims": [],
                "independent_measurements": {
                    "unpack": _not_attempted(
                        "Candidate execution did not start"
                    ),
                    "boot": _not_attempted(
                        "Candidate execution did not start"
                    ),
                    "reachability": [],
                    "stability": [],
                    "authenticity": [],
                    "compute_cost": _not_attempted(
                        "Candidate execution did not start"
                    ),
                    "environmental_residue": (
                        _not_attempted(
                            "Candidate execution did not start"
                        )
                    ),
                    "remediation": _not_attempted(
                        "No runtime residue was measured"
                    ),
                },
                "provenance": {
                    "firmware_sha256": firmware_sha256,
                    "firmware_size_bytes": firmware_size,
                    "adapter_manifest_sha256": (
                        adapter.manifest_sha256
                    ),
                    "request_sha256": request_sha256,
                    "experiment_manifest_sha256": (
                        experiment_manifest_sha256
                    ),
                    "declared_candidate_source": (
                        adapter.manifest[
                            "candidate"
                        ]["source"]
                    ),
                },
            }

            _write_json_atomic(
                run_directory / "final-result.json",
                result,
            )

            return result

        before_snapshot = (
            self.environment_collector.snapshot()
        )

        supervisor: (
            DockerAdapterSupervisor | None
        ) = None
        watchdog: LifecycleWatchdog | None = None
        sampler: DockerComputeCostSampler | None = None
        probe_sidecar: (
            DockerNamespaceProbeSidecar | None
        ) = None

        lifecycle_observation: (
            LifecycleObservation | None
        ) = None
        compute_observation: (
            ComputeCostObservation | None
        ) = None

        residue_observation: (
            ResidueObservation | None
        ) = None
        remediation_observation: (
            RemediationObservation | None
        ) = None

        unpack_observation: (
            UnpackObservation | None
        ) = None

        boot_observation: (
            BootObservation | None
        ) = None

        runtime_error: dict[str, Any] | None = None
        readiness_event: str | None = None
        readiness_condition: str | None = None
        readiness_observed_event: dict[str, Any] | None = None
        blocking_stage_event: dict[str, Any] | None = None
        readiness_wait_error: str | None = None
        endpoint_wait_error: str | None = None

        orchestrator = IndependentProbeOrchestrator()
        network_probe_mode = (
            "docker-shared-network-namespace"
            if runtime_manifest["network"] == "none"
            else "host-network"
        )

        container_id: str | None = None

        try:
            supervisor = DockerAdapterSupervisor(
                backend=self.backend,
                adapter=adapter,
                image=image,
                request_path=request_path,
                contract_root=contract_root,
                event_schema_path=(
                    self.event_schema_path
                ),
            )

            supervisor.start()
            container_id = supervisor.container_id

            sampler = DockerComputeCostSampler(
                backend=self.backend,
                container_id=container_id,
                contract_root=contract_root,
                sample_interval_seconds=(
                    policy
                    .compute_sample_interval_seconds
                ),
            )
            sampler.start()

            watchdog = LifecycleWatchdog(
                supervisor
            )
            watchdog.start()

            if runtime_manifest["network"] == "none":
                if probe_image is None:
                    raise RunCoordinatorError(
                        "Neutral probe image was not built"
                    )

                probe_sidecar = (
                    DockerNamespaceProbeSidecar(
                        backend=self.backend,
                        image=probe_image,
                        candidate_container_id=container_id,
                        run_id=run_id,
                    )
                )
                probe_sidecar.start()
                orchestrator = (
                    IndependentProbeOrchestrator(
                        reachability_probe=(
                            probe_sidecar
                            .probe_endpoint_event
                        ),
                        http_snapshot_probe=(
                            probe_sidecar
                            .capture_http_snapshot
                        ),
                    )
                )

            requested_stages = set(
                policy.requested_stages
            )

            if "emulate" in requested_stages:
                readiness_condition = (
                    "emulation-stage completion or an earlier "
                    "terminal candidate-stage outcome"
                )

                def readiness_predicate(
                    event: dict[str, Any],
                ) -> bool:
                    return (
                        event.get("event") == "stage_completed"
                        and event.get("stage") == "emulate"
                    ) or candidate_stage_blocks_following_work(
                        event,
                        {"unpack"},
                    )

            elif "unpack" in requested_stages:
                readiness_condition = "unpack-stage completion"

                def readiness_predicate(
                    event: dict[str, Any],
                ) -> bool:
                    return (
                        event.get("event") == "stage_completed"
                        and event.get("stage") == "unpack"
                    )

            else:
                readiness_condition = "candidate start"

                def readiness_predicate(
                    event: dict[str, Any],
                ) -> bool:
                    return event.get("event") == "candidate_started"

            try:
                readiness_observed_event = (
                    watchdog.wait_for_matching_event(
                        readiness_predicate,
                        description=readiness_condition,
                        timeout_seconds=float(
                            policy.timeout_seconds
                        ),
                    )
                )
                readiness_event = str(
                    readiness_observed_event.get("event")
                )

                if candidate_stage_blocks_following_work(
                    readiness_observed_event,
                    {"unpack", "emulate"},
                ):
                    blocking_stage_event = dict(
                        readiness_observed_event
                    )

            except LifecycleWatchdogError as exc:
                readiness_wait_error = str(exc)

            if (
                readiness_wait_error is None
                and blocking_stage_event is None
                and "endpoint-discovery"
                in requested_stages
            ):
                try:
                    endpoint_stage_event = (
                        watchdog.wait_for_matching_event(
                            lambda event: (
                                event.get("event")
                                == "stage_completed"
                                and event.get("stage")
                                == "endpoint-discovery"
                            ) or candidate_stage_blocks_following_work(
                                event,
                                {"unpack", "emulate"},
                            ),
                            description=(
                                "endpoint-discovery completion"
                            ),
                            timeout_seconds=(
                                policy.endpoint_wait_timeout_seconds
                                + max(
                                    5.0,
                                    float(
                                        policy
                                        .heartbeat_interval_seconds
                                    ),
                                )
                            ),
                        )
                    )

                    if candidate_stage_blocks_following_work(
                        endpoint_stage_event,
                        {
                            "unpack",
                            "emulate",
                            "endpoint-discovery",
                        },
                    ):
                        blocking_stage_event = dict(
                            endpoint_stage_event
                        )

                except LifecycleWatchdogError as exc:
                    endpoint_wait_error = str(exc)

            handled_sequences: set[int] = set()

            for event in watchdog.observed_events:
                if (
                    event.get("event")
                    != "endpoint_reported"
                ):
                    continue

                sequence = event.get("sequence")

                if not isinstance(sequence, int):
                    continue

                if sequence in handled_sequences:
                    continue

                handled_sequences.add(sequence)
                orchestrator.handle_event(event)

            if orchestrator.endpoint_records:
                orchestrator.measure_stability(
                    sample_count=(
                        policy
                        .stability_sample_count
                    ),
                    interval_seconds=(
                        policy
                        .stability_interval_seconds
                    ),
                    timeout_seconds=(
                        policy
                        .stability_probe_timeout_seconds
                    ),
                    should_continue=(
                        lambda: (
                            supervisor
                            is not None
                            and supervisor
                            .container_running
                        )
                    ),
                )

            watchdog.request_benchmark_shutdown()

            lifecycle_wait_timeout = (
                float(policy.timeout_seconds)
                + float(
                    policy.shutdown_grace_seconds
                )
                + 30.0
            )

            lifecycle_observation = (
                watchdog.wait(
                    timeout_seconds=(
                        lifecycle_wait_timeout
                    )
                )
            )

            if sampler is not None:
                compute_observation = sampler.wait(
                    timeout_seconds=30
                )

            orchestrator.finalize_authenticity(
                trusted_content_sha256=(
                    trusted_content_sha256
                )
            )

        except Exception as exc:
            runtime_error = {
                "phase": "run_coordination",
                "error_type": type(exc).__name__,
                "message": str(exc),
            }

        finally:
            if sampler is not None:
                sampler.request_stop()

                if compute_observation is None:
                    try:
                        compute_observation = (
                            sampler.wait(
                                timeout_seconds=30
                            )
                        )
                    except Exception:
                        pass

            if probe_sidecar is not None:
                try:
                    probe_sidecar.close()
                except Exception:
                    pass

            if supervisor is not None:
                try:
                    supervisor.force_terminate()
                except Exception:
                    pass

                try:
                    supervisor.remove()
                except Exception:
                    pass

        observed_events = (
            watchdog.observed_events
            if watchdog is not None
            else (
                list(supervisor.events)
                if supervisor is not None
                else []
            )
        )

        candidate_stage_results = (
            derive_candidate_stage_results(
                observed_events
            )
        )
        unpack_stage_result = _stage_result(
            candidate_stage_results,
            "unpack",
        )
        emulation_blocked_by_unpack = (
            "emulate" in policy.requested_stages
            and unpack_stage_result is not None
            and unpack_stage_result.get("outcome")
            != "succeeded"
        )

        boot_observation = validate_boot_evidence(
            contract_root=contract_root,
            requested=(
                "emulate" in policy.requested_stages
                and not emulation_blocked_by_unpack
            ),
            not_attempted_reason=(
                "Emulation was requested but not attempted because "
                "the candidate unpack stage did not succeed"
                if emulation_blocked_by_unpack
                else None
            ),
            candidate_events=observed_events,
            lifecycle=lifecycle_observation,
            authenticity_records=(
                orchestrator.authenticity_records
            ),
        )

        write_boot_evidence(
            contract_root=contract_root,
            observation=boot_observation,
        )

        unpack_observation = validate_unpack_export(
            contract_root=contract_root,
            requested=(
                "unpack"
                in policy.requested_stages
            ),
        )

        write_unpack_evidence(
            contract_root=contract_root,
            observation=unpack_observation,
        )

        after_snapshot = (
            self.environment_collector.snapshot()
        )

        residue_observation = (
            evaluate_environment_residue(
                before_snapshot,
                after_snapshot,
            )
        )

        write_environment_evidence(
            contract_root=contract_root,
            before=before_snapshot,
            after=after_snapshot,
            observation=residue_observation,
        )

        remediation_plan = build_remediation_plan(
            run_id=run_id,
            residue=residue_observation,
        )

        janitor = EnvironmentJanitor(
            backend=self.backend,
            contract_root=contract_root,
        )

        remediation_observation = janitor.execute(
            remediation_plan
        )

        event_path = (
            contract_root
            / "events"
            / "events.jsonl"
        )

        event_stream_sha256 = (
            _sha256_file(event_path)
            if event_path.is_file()
            else None
        )

        candidate_claims: list[dict[str, Any]] = []

        for event in observed_events:
            if event.get("event") in {
                "extraction_complete",
                "candidate_boot_reported",
                "endpoint_reported",
            }:
                candidate_claims.append(
                    dict(event)
                )

        if runtime_error is not None:
            overall_status = "orchestration_error"

        elif lifecycle_observation is None:
            overall_status = "run_incomplete"

        else:
            overall_status = (
                lifecycle_observation.run_outcome
            )

        result = {
            **base_result,
            "completed_at": _utc_now(),
            "overall_status": overall_status,
            "setup": {
                "status": "ready",
                "build_duration_seconds": (
                    build_duration_seconds
                ),
                "probe_build_duration_seconds": (
                    probe_build_duration_seconds
                ),
                "error": None,
            },
            "execution": {
                "container_id": container_id,
                "network_probe_mode": (
                    network_probe_mode
                ),
                "runtime_error": runtime_error,
                "readiness_condition": readiness_condition,
                "readiness_event": readiness_event,
                "readiness_observed_event": (
                    readiness_observed_event
                ),
                "blocking_stage_event": (
                    blocking_stage_event
                ),
                "readiness_wait_error": (
                    readiness_wait_error
                ),
                "endpoint_wait_error": (
                    endpoint_wait_error
                ),
                "lifecycle": (
                    lifecycle_observation.to_dict()
                    if lifecycle_observation
                    is not None
                    else _not_attempted(
                        "No lifecycle observation "
                        "was completed"
                    )
                ),
            },
            "candidate_claims": candidate_claims,
            "candidate_stage_results": (
                candidate_stage_results
            ),
            "independent_measurements": {
                "unpack": (
                    _observation_to_dict(
                        unpack_observation,
                        missing_reason=(
                            "No independent unpack "
                            "observation was completed"
                        ),
                    )
                ),
                "boot": (
                    _observation_to_dict(
                        boot_observation,
                        missing_reason=(
                            "No independent boot "
                            "observation was completed"
                        ),
                    )
                ),
                "reachability": [
                    record.to_dict()
                    for record in (
                        orchestrator
                        .endpoint_records
                    )
                ],
                "stability": [
                    record.to_dict()
                    for record in (
                        orchestrator
                        .stability_records
                    )
                ],
                "authenticity": [
                    record.to_dict()
                    for record in (
                        orchestrator
                        .authenticity_records
                    )
                ],
                "compute_cost": (
                    _observation_to_dict(
                        compute_observation,
                        missing_reason=(
                            "No compute-cost observation "
                            "was completed"
                        ),
                    )
                ),
                "environmental_residue": (
                    residue_observation.to_dict()
                ),
                "remediation": (
                    remediation_observation.to_dict()
                ),
            },
            "provenance": {
                "firmware_sha256": firmware_sha256,
                "firmware_size_bytes": firmware_size,
                "adapter_id": adapter.adapter_id,
                "adapter_version": (
                    adapter.manifest[
                        "adapter"
                    ]["version"]
                ),
                "adapter_contract_version": (
                    adapter.manifest[
                        "adapter"
                    ]["contract_version"]
                ),
                "adapter_manifest_sha256": (
                    adapter.manifest_sha256
                ),
                "declared_candidate_source": (
                    adapter.manifest[
                        "candidate"
                    ]["source"]
                ),
                "declared_candidate_patches": (
                    adapter.manifest[
                        "candidate"
                    ].get("patches", [])
                ),
                "build_platform": (
                    adapter.manifest[
                        "build"
                    ]["platform"]
                ),
                "declared_base_images": (
                    adapter.manifest[
                        "build"
                    ]["base_images"]
                ),
                "candidate_image_reference": (
                    image.reference
                ),
                "candidate_image_id": (
                    image.image_id
                ),
                "candidate_image_repo_digests": (
                    list(image.repo_digests)
                ),
                "neutral_probe_runtime": {
                    "mode": network_probe_mode,
                    "image_reference": (
                        probe_image.reference
                        if probe_image is not None
                        else None
                    ),
                    "image_id": (
                        probe_image.image_id
                        if probe_image is not None
                        else None
                    ),
                    "image_repo_digests": (
                        list(
                            probe_image.repo_digests
                        )
                        if probe_image is not None
                        else []
                    ),
                    "platform": (
                        probe_image.platform
                        if probe_image is not None
                        else None
                    ),
                    "worker": (
                        "/opt/veritas-probe/"
                        "worker.py"
                        if probe_image is not None
                        else None
                    ),
                },
                "request_sha256": request_sha256,
                "experiment_manifest_sha256": (
                    experiment_manifest_sha256
                ),
                "event_stream_sha256": (
                    event_stream_sha256
                ),
            },
            "artifacts": {
                "request": (
                    "contract/input/request.json"
                ),
                "event_stream": (
                    "contract/events/events.jsonl"
                ),
                "adapter_stdout": (
                    "contract/artifacts/"
                    "adapter.stdout.log"
                ),
                "adapter_stderr": (
                    "contract/artifacts/"
                    "adapter.stderr.log"
                ),
                "lifecycle_observation": (
                    "contract/artifacts/"
                    "lifecycle-observation.json"
                ),
                "compute_cost_observation": (
                    "contract/artifacts/"
                    "compute-cost-observation.json"
                ),
                "unpack_observation": (
                    "contract/artifacts/"
                    "unpack-observation.json"
                ),
                "boot_observation": (
                    "contract/artifacts/"
                    "boot-observation.json"
                ),
                "environment_before": (
                    "contract/artifacts/"
                    "environment-before.json"
                ),
                "environment_after": (
                    "contract/artifacts/"
                    "environment-after.json"
                ),
                "environment_residue": (
                    "contract/artifacts/"
                    "environment-residue.json"
                ),
                "remediation_plan": (
                    "contract/artifacts/"
                    "remediation-plan.json"
                ),
                "remediation_observation": (
                    "contract/artifacts/"
                    "remediation-observation.json"
                ),
            },
        }

        result_sha256 = _write_json_atomic(
            run_directory / "final-result.json",
            result,
        )

        result["provenance"][
            "final_result_sha256_before_embedding"
        ] = result_sha256

        # The detached hash avoids making the JSON hash
        # self-referential.
        (
            run_directory / "final-result.sha256"
        ).write_text(
            f"{result_sha256}  final-result.json\n",
            encoding="utf-8",
        )

        return result
