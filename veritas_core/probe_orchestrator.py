from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from veritas_core.probes.http_snapshot import (
    HttpServiceSnapshot,
    capture_http_snapshot,
)
from veritas_core.probes.service_authenticity import (
    AuthenticityObservation,
    evaluate_http_authenticity,
)
from veritas_core.probes.service_reachability import (
    ReachabilityObservation,
    probe_endpoint_event,
)


@dataclass(frozen=True)
class EndpointProbeRecord:
    source_event_sequence: int
    candidate_claim: dict[str, Any]
    independent_measurement: ReachabilityObservation
    active_http_snapshot: HttpServiceSnapshot | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_event_sequence": self.source_event_sequence,
            "candidate_claim": dict(self.candidate_claim),
            "independent_measurement": (
                self.independent_measurement.to_dict()
            ),
            "active_http_snapshot": (
                self.active_http_snapshot.to_dict()
                if self.active_http_snapshot is not None
                else None
            ),
        }


@dataclass(frozen=True)
class EndpointAuthenticityRecord:
    source_event_sequence: int
    candidate_claim: dict[str, Any]
    active_snapshot: HttpServiceSnapshot
    post_shutdown_snapshot: HttpServiceSnapshot
    independent_measurement: AuthenticityObservation

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_event_sequence": self.source_event_sequence,
            "candidate_claim": dict(self.candidate_claim),
            "active_snapshot": self.active_snapshot.to_dict(),
            "post_shutdown_snapshot": (
                self.post_shutdown_snapshot.to_dict()
            ),
            "independent_measurement": (
                self.independent_measurement.to_dict()
            ),
        }


class IndependentProbeOrchestrator:
    """
    Dispatch independent probes from validated adapter events.

    Candidate events trigger measurements but never determine their results.
    """

    def __init__(self) -> None:
        self.endpoint_records: list[EndpointProbeRecord] = []
        self.authenticity_records: list[
            EndpointAuthenticityRecord
        ] = []

    def handle_event(
        self,
        event: dict[str, Any],
    ) -> list[EndpointProbeRecord]:
        if event.get("event") != "endpoint_reported":
            return []

        reachability = probe_endpoint_event(
            event,
            timeout_seconds=2.0,
            attempts=2,
            retry_delay_seconds=0.1,
        )

        active_snapshot: HttpServiceSnapshot | None = None

        if reachability.status == "true":
            active_snapshot = capture_http_snapshot(
                event,
                timeout_seconds=3.0,
            )

        record = EndpointProbeRecord(
            source_event_sequence=int(event["sequence"]),
            candidate_claim=dict(event["endpoint"]),
            independent_measurement=reachability,
            active_http_snapshot=active_snapshot,
        )

        self.endpoint_records.append(record)
        return [record]

    def finalize_authenticity(
        self,
        *,
        trusted_content_sha256: Iterable[str],
    ) -> list[EndpointAuthenticityRecord]:
        """
        Perform post-shutdown checks and evaluate authenticity.

        Call only after the adapter has completed graceful shutdown or has
        been forcefully terminated.
        """
        self.authenticity_records = []

        for record in self.endpoint_records:
            if record.active_http_snapshot is None:
                continue

            endpoint_event = {
                "event": "endpoint_reported",
                "sequence": record.source_event_sequence,
                "endpoint": dict(record.candidate_claim),
            }

            post_shutdown_snapshot = capture_http_snapshot(
                endpoint_event,
                timeout_seconds=1.0,
            )

            observation = evaluate_http_authenticity(
                record.active_http_snapshot,
                post_shutdown_snapshot,
                trusted_content_sha256=trusted_content_sha256,
            )

            authenticity_record = EndpointAuthenticityRecord(
                source_event_sequence=(
                    record.source_event_sequence
                ),
                candidate_claim=dict(record.candidate_claim),
                active_snapshot=record.active_http_snapshot,
                post_shutdown_snapshot=post_shutdown_snapshot,
                independent_measurement=observation,
            )

            self.authenticity_records.append(
                authenticity_record
            )

        return list(self.authenticity_records)
