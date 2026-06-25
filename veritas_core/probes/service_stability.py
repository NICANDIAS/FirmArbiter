from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from veritas_core.probes.http_snapshot import (
    capture_http_snapshot,
)
from veritas_core.probes.service_reachability import (
    TCP_PROTOCOLS,
    probe_endpoint_event,
)


@dataclass(frozen=True)
class StabilitySample:
    sample_number: int
    observed_at: str
    available: bool
    reachability_status: str
    latency_ms: float | None
    http_snapshot_status: str | None
    http_status: int | None
    response_fingerprint_sha256: str | None
    error_type: str | None
    error_message: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StabilityObservation:
    metric: str
    status: str
    started_at: str
    completed_at: str
    requested_samples: int
    completed_samples: int
    successful_samples: int
    failed_samples: int
    availability_ratio: float | None
    longest_failure_streak: int
    interval_seconds: float
    reason: str
    samples: tuple[StabilitySample, ...]

    def to_dict(self) -> dict[str, Any]:
        document = asdict(self)
        document["samples"] = [
            sample.to_dict()
            for sample in self.samples
        ]
        return document


class StabilityProbeError(RuntimeError):
    """Raised when stability-probe input is invalid."""


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _longest_failure_streak(
    samples: list[StabilitySample],
) -> int:
    longest = 0
    current = 0

    for sample in samples:
        if sample.available:
            current = 0
        else:
            current += 1
            longest = max(longest, current)

    return longest


def measure_endpoint_stability(
    event: dict[str, Any],
    *,
    sample_count: int,
    interval_seconds: float,
    timeout_seconds: float,
    should_continue: Callable[[], bool] | None = None,
) -> StabilityObservation:
    """
    Repeatedly measure a structured endpoint claim.

    The candidate claim supplies only the endpoint coordinates. Each sample
    is independently performed by VERITAS.

    `should_continue` allows the lifecycle supervisor to stop sampling when
    the adapter or candidate is no longer alive.
    """
    if event.get("event") != "endpoint_reported":
        raise StabilityProbeError(
            "Stability measurement requires an endpoint_reported event"
        )

    endpoint = event.get("endpoint")

    if not isinstance(endpoint, dict):
        raise StabilityProbeError(
            "endpoint_reported event has no endpoint object"
        )

    try:
        protocol = str(endpoint["protocol"])
    except KeyError as exc:
        raise StabilityProbeError(
            "Endpoint claim does not contain a protocol"
        ) from exc

    if sample_count < 1:
        raise StabilityProbeError(
            "sample_count must be at least one"
        )

    if interval_seconds < 0:
        raise StabilityProbeError(
            "interval_seconds cannot be negative"
        )

    if timeout_seconds <= 0:
        raise StabilityProbeError(
            "timeout_seconds must be greater than zero"
        )

    started_at = _utc_now()

    if protocol not in TCP_PROTOCOLS:
        completed_at = _utc_now()

        return StabilityObservation(
            metric="service_stability",
            status="not_applicable",
            started_at=started_at,
            completed_at=completed_at,
            requested_samples=sample_count,
            completed_samples=0,
            successful_samples=0,
            failed_samples=0,
            availability_ratio=None,
            longest_failure_streak=0,
            interval_seconds=interval_seconds,
            reason=(
                "The current stability probe supports TCP-based "
                f"protocols only; received {protocol!r}"
            ),
            samples=(),
        )

    samples: list[StabilitySample] = []
    interrupted = False

    next_sample_time = time.monotonic()

    for sample_number in range(1, sample_count + 1):
        if should_continue is not None and not should_continue():
            interrupted = True
            break

        remaining_delay = next_sample_time - time.monotonic()

        if remaining_delay > 0:
            time.sleep(remaining_delay)

        if should_continue is not None and not should_continue():
            interrupted = True
            break

        try:
            reachability = probe_endpoint_event(
                event,
                timeout_seconds=timeout_seconds,
                attempts=1,
                retry_delay_seconds=0,
            )

            available = reachability.status == "true"
            http_snapshot_status: str | None = None
            http_status: int | None = None
            response_fingerprint: str | None = None
            error_type = reachability.error_type
            error_message = reachability.error_message

            if available and protocol in {"http", "https"}:
                snapshot = capture_http_snapshot(
                    event,
                    timeout_seconds=timeout_seconds,
                )

                http_snapshot_status = snapshot.status
                http_status = snapshot.http_status
                response_fingerprint = (
                    snapshot.response_fingerprint_sha256
                )

                available = snapshot.status == "captured"

                if not available:
                    error_type = snapshot.error_type
                    error_message = snapshot.error_message

            samples.append(
                StabilitySample(
                    sample_number=sample_number,
                    observed_at=_utc_now(),
                    available=available,
                    reachability_status=reachability.status,
                    latency_ms=reachability.latency_ms,
                    http_snapshot_status=http_snapshot_status,
                    http_status=http_status,
                    response_fingerprint_sha256=(
                        response_fingerprint
                    ),
                    error_type=error_type,
                    error_message=error_message,
                )
            )

        except Exception as exc:
            samples.append(
                StabilitySample(
                    sample_number=sample_number,
                    observed_at=_utc_now(),
                    available=False,
                    reachability_status="probe_error",
                    latency_ms=None,
                    http_snapshot_status=None,
                    http_status=None,
                    response_fingerprint_sha256=None,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
            )

        next_sample_time += interval_seconds

    completed_at = _utc_now()

    completed_samples = len(samples)
    successful_samples = sum(
        1 for sample in samples if sample.available
    )
    failed_samples = completed_samples - successful_samples

    availability_ratio = (
        round(successful_samples / completed_samples, 6)
        if completed_samples > 0
        else None
    )

    longest_failure_streak = _longest_failure_streak(
        samples
    )

    if completed_samples == 0:
        status = "not_attempted"
        reason = (
            "The candidate lifecycle ended before stability "
            "sampling could begin"
        )

    elif interrupted or completed_samples < sample_count:
        status = "inconclusive"
        reason = (
            "The candidate lifecycle ended before the stability "
            "observation window completed"
        )

    elif any(
        sample.reachability_status == "probe_error"
        for sample in samples
    ):
        status = "probe_error"
        reason = (
            "VERITAS encountered an internal error during one or "
            "more scheduled stability samples"
        )

    elif failed_samples == 0:
        status = "true"
        reason = (
            "The endpoint responded successfully during every "
            "scheduled stability sample"
        )

    else:
        status = "false"
        reason = (
            "The observation window completed, but one or more "
            "scheduled samples did not receive a service response"
        )

    return StabilityObservation(
        metric="service_stability",
        status=status,
        started_at=started_at,
        completed_at=completed_at,
        requested_samples=sample_count,
        completed_samples=completed_samples,
        successful_samples=successful_samples,
        failed_samples=failed_samples,
        availability_ratio=availability_ratio,
        longest_failure_streak=longest_failure_streak,
        interval_seconds=interval_seconds,
        reason=reason,
        samples=tuple(samples),
    )
