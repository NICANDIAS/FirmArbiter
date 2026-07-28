from __future__ import annotations

import json
import math
import re
import threading
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Protocol


class ComputeCostBackend(Protocol):
    def container_running(
        self,
        container_id: str,
    ) -> bool:
        ...

    def container_stats_snapshot(
        self,
        container_id: str,
    ) -> dict[str, Any]:
        ...

    def inspect_container(
        self,
        container_id: str,
    ) -> dict[str, Any]:
        ...


class ComputeCostError(RuntimeError):
    """Raised when compute-cost measurement cannot be completed."""


@dataclass(frozen=True)
class ResourceSample:
    sequence: int
    observed_at: str
    offset_seconds: float
    cpu_percent: float
    memory_usage_bytes: int
    memory_limit_bytes: int
    memory_percent: float
    network_rx_bytes: int
    network_tx_bytes: int
    block_read_bytes: int
    block_write_bytes: int
    pids_tasks: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ComputeCostObservation:
    metric: str
    status: str
    started_at: str
    completed_at: str
    sampler_elapsed_seconds: float
    container_started_at: str | None
    container_finished_at: str | None
    container_wall_time_seconds: float | None
    sample_interval_seconds: float
    samples_collected: int
    sample_errors: int
    mean_cpu_percent: float | None
    peak_cpu_percent: float | None
    estimated_cpu_seconds: float | None
    mean_memory_usage_bytes: float | None
    peak_memory_usage_bytes: int | None
    memory_limit_bytes: int | None
    peak_memory_percent: float | None
    peak_pids_tasks: int | None
    final_network_rx_bytes: int | None
    final_network_tx_bytes: int | None
    final_block_read_bytes: int | None
    final_block_write_bytes: int | None
    exit_code: int | None
    oom_killed: bool | None
    container_error: str | None
    image_id: str | None
    reason: str
    errors: tuple[str, ...]
    samples: tuple[ResourceSample, ...]

    def to_dict(self) -> dict[str, Any]:
        document = asdict(self)
        document["errors"] = list(self.errors)
        document["samples"] = [
            sample.to_dict()
            for sample in self.samples
        ]
        return document


_UNIT_FACTORS = {
    "b": 1,
    "kb": 1000,
    "kib": 1024,
    "mb": 1000**2,
    "mib": 1024**2,
    "gb": 1000**3,
    "gib": 1024**3,
    "tb": 1000**4,
    "tib": 1024**4,
}

_SIZE_PATTERN = re.compile(
    r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)\s*$"
)


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def parse_percentage(value: Any) -> float:
    text = str(value).strip()

    if text.endswith("%"):
        text = text[:-1]

    number = float(text)

    if not math.isfinite(number) or number < 0:
        raise ValueError(
            f"Invalid percentage value: {value!r}"
        )

    return number


def parse_size_bytes(value: Any) -> int:
    text = str(value).strip()

    if text in {"0", "0B", "0 B"}:
        return 0

    match = _SIZE_PATTERN.match(text)

    if match is None:
        raise ValueError(
            f"Invalid Docker size value: {value!r}"
        )

    magnitude = float(match.group(1))
    unit = match.group(2).lower()

    if unit not in _UNIT_FACTORS:
        raise ValueError(
            f"Unsupported Docker size unit: {unit!r}"
        )

    return int(round(magnitude * _UNIT_FACTORS[unit]))


def parse_usage_pair(value: Any) -> tuple[int, int]:
    parts = str(value).split("/")

    if len(parts) != 2:
        raise ValueError(
            f"Expected Docker usage pair, received {value!r}"
        )

    return (
        parse_size_bytes(parts[0]),
        parse_size_bytes(parts[1]),
    )


def parse_pids_tasks(value: Any) -> int:
    number = int(str(value).strip())

    if number < 0:
        raise ValueError(
            f"Invalid PIDs/tasks value: {value!r}"
        )

    return number


def resource_sample_from_docker(
    document: dict[str, Any],
    *,
    sequence: int,
    observed_at: str,
    offset_seconds: float,
) -> ResourceSample:
    memory_usage, memory_limit = parse_usage_pair(
        document["MemUsage"]
    )

    network_rx, network_tx = parse_usage_pair(
        document["NetIO"]
    )

    block_read, block_write = parse_usage_pair(
        document["BlockIO"]
    )

    return ResourceSample(
        sequence=sequence,
        observed_at=observed_at,
        offset_seconds=round(offset_seconds, 6),
        cpu_percent=parse_percentage(
            document["CPUPerc"]
        ),
        memory_usage_bytes=memory_usage,
        memory_limit_bytes=memory_limit,
        memory_percent=parse_percentage(
            document["MemPerc"]
        ),
        network_rx_bytes=network_rx,
        network_tx_bytes=network_tx,
        block_read_bytes=block_read,
        block_write_bytes=block_write,
        pids_tasks=parse_pids_tasks(
            document["PIDs"]
        ),
    )


def _parse_docker_timestamp(
    value: Any,
) -> datetime | None:
    if not isinstance(value, str):
        return None

    text = value.strip()

    if (
        not text
        or text.startswith("0001-01-01T00:00:00")
    ):
        return None

    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    # Python accepts microseconds; Docker may return nanoseconds.
    match = re.match(
        r"^(.*?\.)([0-9]+)([+-][0-9]{2}:[0-9]{2})$",
        text,
    )

    if match is not None:
        fraction = match.group(2)[:6].ljust(6, "0")
        text = (
            match.group(1)
            + fraction
            + match.group(3)
        )

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc)



def _configured_memory_limit(
    inspection: dict[str, Any],
) -> int | None:
    """
    Read the memory budget applied when Docker created the container.

    Docker stats may report a zero memory-limit denominator on some
    environments. HostConfig.Memory records the configured hard limit.
    """
    host_config = inspection.get("HostConfig")

    if not isinstance(host_config, dict):
        return None

    raw_limit = host_config.get("Memory")

    if isinstance(raw_limit, bool):
        return None

    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        return None

    return limit if limit > 0 else None


def _estimate_cpu_seconds(
    samples: list[ResourceSample],
) -> float | None:
    """
    Integrate sampled Docker CPU percentage using the trapezoidal rule.

    Docker CPU percentage may exceed 100 on multi-core workloads. Therefore,
    dividing by 100 converts the percentage into approximate CPU-core usage
    before integrating over elapsed time.
    """
    if len(samples) < 2:
        return None

    total = 0.0

    for previous, current in zip(
        samples,
        samples[1:],
    ):
        elapsed = (
            current.offset_seconds
            - previous.offset_seconds
        )

        if elapsed <= 0:
            continue

        average_cpu = (
            previous.cpu_percent
            + current.cpu_percent
        ) / 2.0

        total += (
            average_cpu / 100.0
        ) * elapsed

    return round(total, 6)


class DockerComputeCostSampler:
    """
    Independently sample resource use for one running container.

    Sampling starts only after container creation/start. Image build and
    adapter setup costs outside the container are not included.
    """

    def __init__(
        self,
        *,
        backend: ComputeCostBackend,
        container_id: str,
        contract_root: Path,
        sample_interval_seconds: float = 1.0,
    ) -> None:
        if sample_interval_seconds <= 0:
            raise ComputeCostError(
                "sample_interval_seconds must be greater than zero"
            )

        self.backend = backend
        self.container_id = container_id
        self.contract_root = contract_root.resolve()
        self.sample_interval_seconds = (
            sample_interval_seconds
        )

        self._stop_requested = threading.Event()
        self._finished = threading.Event()
        self._thread: threading.Thread | None = None

        self._samples: list[ResourceSample] = []
        self._errors: list[str] = []
        self._result: ComputeCostObservation | None = None
        self._thread_error: BaseException | None = None

    @property
    def samples(self) -> list[ResourceSample]:
        return list(self._samples)

    def start(self) -> None:
        if self._thread is not None:
            raise ComputeCostError(
                "Compute-cost sampler has already been started"
            )

        self._thread = threading.Thread(
            target=self._run,
            name="firmarbiter-compute-cost-sampler",
            daemon=True,
        )
        self._thread.start()

    def request_stop(self) -> None:
        self._stop_requested.set()

    def wait(
        self,
        timeout_seconds: float,
    ) -> ComputeCostObservation:
        if self._thread is None:
            raise ComputeCostError(
                "Compute-cost sampler has not been started"
            )

        if not self._finished.wait(timeout_seconds):
            raise ComputeCostError(
                "Timed out waiting for compute-cost sampler"
            )

        self._thread.join(timeout=1)

        if self._thread_error is not None:
            raise ComputeCostError(
                "Compute-cost sampler failed"
            ) from self._thread_error

        if self._result is None:
            raise ComputeCostError(
                "Compute-cost sampler produced no result"
            )

        return self._result

    def stop_and_wait(
        self,
        timeout_seconds: float,
    ) -> ComputeCostObservation:
        self.request_stop()
        return self.wait(timeout_seconds)

    def _persist(
        self,
        observation: ComputeCostObservation,
    ) -> None:
        artifact_directory = (
            self.contract_root / "artifacts"
        )
        artifact_directory.mkdir(
            parents=True,
            exist_ok=True,
        )

        output_path = (
            artifact_directory
            / "compute-cost-observation.json"
        )

        temporary_path = output_path.with_suffix(".tmp")

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

    def _run(self) -> None:
        started_at = _utc_now()
        started_monotonic = time.monotonic()

        inspection: dict[str, Any] = {}
        configured_memory_limit: int | None = None

        try:
            try:
                initial_inspection = (
                    self.backend.inspect_container(
                        self.container_id
                    )
                )
                configured_memory_limit = (
                    _configured_memory_limit(
                        initial_inspection
                    )
                )
            except Exception as exc:
                self._errors.append(
                    "initial-container-inspect: "
                    f"{type(exc).__name__}: {exc}"
                )

            next_sample_time = started_monotonic

            while not self._stop_requested.is_set():
                try:
                    running = self.backend.container_running(
                        self.container_id
                    )
                except Exception as exc:
                    self._errors.append(
                        "container-state: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    running = False

                if not running:
                    break

                remaining = (
                    next_sample_time
                    - time.monotonic()
                )

                if remaining > 0:
                    if self._stop_requested.wait(
                        timeout=remaining
                    ):
                        break

                if self._stop_requested.is_set():
                    break

                try:
                    raw_stats = (
                        self.backend
                        .container_stats_snapshot(
                            self.container_id
                        )
                    )

                    observed_monotonic = time.monotonic()

                    sample = resource_sample_from_docker(
                        raw_stats,
                        sequence=len(self._samples) + 1,
                        observed_at=_utc_now(),
                        offset_seconds=(
                            observed_monotonic
                            - started_monotonic
                        ),
                    )

                    if (
                        sample.memory_limit_bytes == 0
                        and configured_memory_limit is not None
                    ):
                        recalculated_memory_percent = round(
                            (
                                sample.memory_usage_bytes
                                / configured_memory_limit
                            )
                            * 100.0,
                            6,
                        )

                        sample = replace(
                            sample,
                            memory_limit_bytes=(
                                configured_memory_limit
                            ),
                            memory_percent=(
                                recalculated_memory_percent
                            ),
                        )

                    self._samples.append(sample)

                except Exception as exc:
                    self._errors.append(
                        "stats-sample: "
                        f"{type(exc).__name__}: {exc}"
                    )

                next_sample_time += (
                    self.sample_interval_seconds
                )

                # Avoid rapid catch-up loops after a slow stats call.
                if next_sample_time < time.monotonic():
                    next_sample_time = (
                        time.monotonic()
                        + self.sample_interval_seconds
                    )

            try:
                inspection = self.backend.inspect_container(
                    self.container_id
                )
            except Exception as exc:
                self._errors.append(
                    "container-inspect: "
                    f"{type(exc).__name__}: {exc}"
                )
                inspection = {}

            completed_at = _utc_now()
            sampler_elapsed = round(
                time.monotonic() - started_monotonic,
                6,
            )

            state = inspection.get("State", {})
            image_id = inspection.get("Image")

            final_configured_memory_limit = (
                _configured_memory_limit(inspection)
            )

            if final_configured_memory_limit is not None:
                configured_memory_limit = (
                    final_configured_memory_limit
                )

            if not isinstance(state, dict):
                state = {}

            container_started_raw = state.get("StartedAt")
            container_finished_raw = state.get("FinishedAt")

            container_started = _parse_docker_timestamp(
                container_started_raw
            )
            container_finished = _parse_docker_timestamp(
                container_finished_raw
            )

            container_wall_time: float | None = None

            if (
                container_started is not None
                and container_finished is not None
                and container_finished >= container_started
            ):
                container_wall_time = round(
                    (
                        container_finished
                        - container_started
                    ).total_seconds(),
                    6,
                )

            sample_count = len(self._samples)

            if sample_count > 0 and not self._errors:
                status = "complete"
                reason = (
                    "Container resource sampling completed "
                    "without measurement errors"
                )
            elif sample_count > 0:
                status = "partial"
                reason = (
                    "Resource samples were collected, but one or "
                    "more measurements failed"
                )
            elif self._errors:
                status = "probe_error"
                reason = (
                    "FIRMARBITER could not collect a valid container "
                    "resource sample"
                )
            else:
                status = "not_attempted"
                reason = (
                    "The container stopped before resource "
                    "sampling could begin"
                )

            cpu_values = [
                sample.cpu_percent
                for sample in self._samples
            ]

            memory_values = [
                sample.memory_usage_bytes
                for sample in self._samples
            ]

            memory_percent_values = [
                sample.memory_percent
                for sample in self._samples
            ]

            pids_values = [
                sample.pids_tasks
                for sample in self._samples
            ]

            final_sample = (
                self._samples[-1]
                if self._samples
                else None
            )

            exit_code_raw = state.get("ExitCode")
            exit_code = (
                int(exit_code_raw)
                if isinstance(exit_code_raw, int)
                else None
            )

            oom_raw = state.get("OOMKilled")
            oom_killed = (
                bool(oom_raw)
                if isinstance(oom_raw, bool)
                else None
            )

            container_error_raw = state.get("Error")
            container_error = (
                str(container_error_raw)
                if container_error_raw
                else None
            )

            observation = ComputeCostObservation(
                metric="compute_cost",
                status=status,
                started_at=started_at,
                completed_at=completed_at,
                sampler_elapsed_seconds=sampler_elapsed,
                container_started_at=(
                    str(container_started_raw)
                    if container_started_raw
                    else None
                ),
                container_finished_at=(
                    str(container_finished_raw)
                    if container_finished_raw
                    else None
                ),
                container_wall_time_seconds=(
                    container_wall_time
                ),
                sample_interval_seconds=(
                    self.sample_interval_seconds
                ),
                samples_collected=sample_count,
                sample_errors=len(self._errors),
                mean_cpu_percent=(
                    round(mean(cpu_values), 6)
                    if cpu_values
                    else None
                ),
                peak_cpu_percent=(
                    max(cpu_values)
                    if cpu_values
                    else None
                ),
                estimated_cpu_seconds=(
                    _estimate_cpu_seconds(
                        self._samples
                    )
                ),
                mean_memory_usage_bytes=(
                    round(mean(memory_values), 3)
                    if memory_values
                    else None
                ),
                peak_memory_usage_bytes=(
                    max(memory_values)
                    if memory_values
                    else None
                ),
                memory_limit_bytes=(
                    configured_memory_limit
                    if configured_memory_limit is not None
                    else (
                        final_sample.memory_limit_bytes
                        if final_sample is not None
                        else None
                    )
                ),
                peak_memory_percent=(
                    max(memory_percent_values)
                    if memory_percent_values
                    else None
                ),
                peak_pids_tasks=(
                    max(pids_values)
                    if pids_values
                    else None
                ),
                final_network_rx_bytes=(
                    final_sample.network_rx_bytes
                    if final_sample is not None
                    else None
                ),
                final_network_tx_bytes=(
                    final_sample.network_tx_bytes
                    if final_sample is not None
                    else None
                ),
                final_block_read_bytes=(
                    final_sample.block_read_bytes
                    if final_sample is not None
                    else None
                ),
                final_block_write_bytes=(
                    final_sample.block_write_bytes
                    if final_sample is not None
                    else None
                ),
                exit_code=exit_code,
                oom_killed=oom_killed,
                container_error=container_error,
                image_id=(
                    str(image_id)
                    if image_id
                    else None
                ),
                reason=reason,
                errors=tuple(self._errors),
                samples=tuple(self._samples),
            )

            self._result = observation
            self._persist(observation)

        except BaseException as exc:
            self._thread_error = exc

        finally:
            self._finished.set()
