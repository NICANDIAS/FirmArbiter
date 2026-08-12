from __future__ import annotations

import socket
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any


TCP_PROTOCOLS = {
    "tcp",
    "http",
    "https",
    "ssh",
    "telnet",
}


@dataclass(frozen=True)
class ReachabilityObservation:
    """
    Independent transport-level observation.

    `status` is deliberately not a boolean because the benchmark must
    distinguish false, not attempted, not applicable and probe error.
    """

    metric: str
    status: str
    observed_at: str
    host: str
    port: int
    protocol: str
    attempts: int
    latency_ms: float | None
    error_type: str | None
    error_message: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ReachabilityProbeError(RuntimeError):
    """Raised when the supplied event violates the probe interface."""


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def probe_endpoint_event(
    event: dict[str, Any],
    *,
    timeout_seconds: float = 2.0,
    attempts: int = 1,
    retry_delay_seconds: float = 0.1,
) -> ReachabilityObservation:
    """
    Independently test a structured endpoint claim.

    Candidate stdout, candidate success phrases and candidate-specific
    knowledge are never used.
    """
    if event.get("event") != "endpoint_reported":
        raise ReachabilityProbeError(
            "Reachability probe requires an endpoint_reported event"
        )

    endpoint = event.get("endpoint")

    if not isinstance(endpoint, dict):
        raise ReachabilityProbeError(
            "endpoint_reported event contains no endpoint object"
        )

    try:
        host = str(endpoint["host"])
        port = int(endpoint["port"])
        protocol = str(endpoint["protocol"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ReachabilityProbeError(
            "Endpoint claim is incomplete or malformed"
        ) from exc

    if not host:
        raise ReachabilityProbeError(
            "Endpoint host must not be empty"
        )

    if not 1 <= port <= 65535:
        raise ReachabilityProbeError(
            f"Endpoint port is outside the valid range: {port}"
        )

    if attempts < 1:
        raise ReachabilityProbeError(
            "Probe attempts must be at least one"
        )

    if protocol not in TCP_PROTOCOLS:
        return ReachabilityObservation(
            metric="service_reachability",
            status="not_applicable",
            observed_at=_utc_now(),
            host=host,
            port=port,
            protocol=protocol,
            attempts=0,
            latency_ms=None,
            error_type=None,
            error_message=(
                "Transport-level TCP reachability is not applicable "
                f"to protocol {protocol!r}"
            ),
        )

    final_exception: OSError | None = None

    for attempt_number in range(1, attempts + 1):
        started = time.monotonic()

        try:
            with socket.create_connection(
                (host, port),
                timeout=timeout_seconds,
            ):
                latency_ms = round(
                    (time.monotonic() - started) * 1000,
                    3,
                )

                return ReachabilityObservation(
                    metric="service_reachability",
                    status="true",
                    observed_at=_utc_now(),
                    host=host,
                    port=port,
                    protocol=protocol,
                    attempts=attempt_number,
                    latency_ms=latency_ms,
                    error_type=None,
                    error_message=None,
                )

        except OSError as exc:
            final_exception = exc

            if attempt_number < attempts:
                time.sleep(retry_delay_seconds)

    return ReachabilityObservation(
        metric="service_reachability",
        status="false",
        observed_at=_utc_now(),
        host=host,
        port=port,
        protocol=protocol,
        attempts=attempts,
        latency_ms=None,
        error_type=(
            type(final_exception).__name__
            if final_exception is not None
            else None
        ),
        error_message=(
            str(final_exception)
            if final_exception is not None
            else None
        ),
    )
