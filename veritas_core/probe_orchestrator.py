from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from veritas_core.probes.service_reachability import (
    ReachabilityObservation,
    probe_endpoint_event,
)


@dataclass(frozen=True)
class EndpointProbeRecord:
    source_event_sequence: int
    candidate_claim: dict[str, Any]
    independent_measurement: ReachabilityObservation

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_event_sequence": self.source_event_sequence,
            "candidate_claim": dict(self.candidate_claim),
            "independent_measurement": (
                self.independent_measurement.to_dict()
            ),
        }


class IndependentProbeOrchestrator:
    """
    Dispatches independent probes from validated adapter events.

    Adapter events trigger measurement but never determine its result.
    """

    def __init__(self) -> None:
        self.endpoint_records: list[EndpointProbeRecord] = []

    def handle_event(
        self,
        event: dict[str, Any],
    ) -> list[EndpointProbeRecord]:
        if event.get("event") != "endpoint_reported":
            return []

        observation = probe_endpoint_event(
            event,
            timeout_seconds=2.0,
            attempts=2,
            retry_delay_seconds=0.1,
        )

        record = EndpointProbeRecord(
            source_event_sequence=int(event["sequence"]),
            candidate_claim=dict(event["endpoint"]),
            independent_measurement=observation,
        )

        self.endpoint_records.append(record)

        return [record]
