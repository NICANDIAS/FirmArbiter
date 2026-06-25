from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from veritas_core.probes.http_snapshot import (
    HttpServiceSnapshot,
)


@dataclass(frozen=True)
class AuthenticityObservation:
    metric: str
    status: str
    observed_at: str
    method: str
    reason: str
    trusted_content_match: bool | None
    endpoint_disappeared_after_shutdown: bool | None
    matched_sha256: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def evaluate_http_authenticity(
    active_snapshot: HttpServiceSnapshot,
    post_shutdown_snapshot: HttpServiceSnapshot,
    *,
    trusted_content_sha256: Iterable[str],
) -> AuthenticityObservation:
    """
    Evaluate authenticity using independent evidence.

    Trusted hashes must come from VERITAS-controlled corpus or firmware
    artifact analysis, never from an adapter claim.
    """
    trusted_hashes = {
        value.lower()
        for value in trusted_content_sha256
        if isinstance(value, str)
    }

    if active_snapshot.status == "not_applicable":
        return AuthenticityObservation(
            metric="service_authenticity",
            status="not_applicable",
            observed_at=_utc_now(),
            method="http_content_and_lifecycle_correlation",
            reason="Endpoint protocol is not HTTP or HTTPS",
            trusted_content_match=None,
            endpoint_disappeared_after_shutdown=None,
            matched_sha256=None,
        )

    if active_snapshot.status != "captured":
        return AuthenticityObservation(
            metric="service_authenticity",
            status="not_attempted",
            observed_at=_utc_now(),
            method="http_content_and_lifecycle_correlation",
            reason=(
                "No active HTTP response was captured while the "
                "candidate was running"
            ),
            trusted_content_match=None,
            endpoint_disappeared_after_shutdown=None,
            matched_sha256=None,
        )

    if post_shutdown_snapshot.status == "probe_error":
        return AuthenticityObservation(
            metric="service_authenticity",
            status="probe_error",
            observed_at=_utc_now(),
            method="http_content_and_lifecycle_correlation",
            reason=(
                "VERITAS could not complete the post-shutdown "
                "endpoint measurement"
            ),
            trusted_content_match=None,
            endpoint_disappeared_after_shutdown=None,
            matched_sha256=None,
        )

    active_hash = active_snapshot.body_sha256

    trusted_match = (
        active_hash is not None
        and not bool(active_snapshot.body_truncated)
        and active_hash.lower() in trusted_hashes
    )

    if post_shutdown_snapshot.status == "captured":
        return AuthenticityObservation(
            metric="service_authenticity",
            status="false",
            observed_at=_utc_now(),
            method="http_content_and_lifecycle_correlation",
            reason=(
                "The endpoint remained reachable after candidate "
                "shutdown; the response cannot be attributed "
                "exclusively to the candidate lifecycle"
            ),
            trusted_content_match=trusted_match,
            endpoint_disappeared_after_shutdown=False,
            matched_sha256=active_hash if trusted_match else None,
        )

    if post_shutdown_snapshot.status == "unreachable":
        if trusted_match:
            return AuthenticityObservation(
                metric="service_authenticity",
                status="true",
                observed_at=_utc_now(),
                method=(
                    "trusted_firmware_content_match_and_"
                    "lifecycle_disappearance"
                ),
                reason=(
                    "The active response matched independently trusted "
                    "firmware content and disappeared after shutdown"
                ),
                trusted_content_match=True,
                endpoint_disappeared_after_shutdown=True,
                matched_sha256=active_hash,
            )

        return AuthenticityObservation(
            metric="service_authenticity",
            status="inconclusive",
            observed_at=_utc_now(),
            method="http_content_and_lifecycle_correlation",
            reason=(
                "The endpoint disappeared after shutdown, but its "
                "response did not match independently trusted "
                "firmware content"
            ),
            trusted_content_match=False,
            endpoint_disappeared_after_shutdown=True,
            matched_sha256=None,
        )

    return AuthenticityObservation(
        metric="service_authenticity",
        status="probe_error",
        observed_at=_utc_now(),
        method="http_content_and_lifecycle_correlation",
        reason=(
            "Unexpected post-shutdown snapshot state: "
            f"{post_shutdown_snapshot.status}"
        ),
        trusted_content_match=None,
        endpoint_disappeared_after_shutdown=None,
        matched_sha256=None,
    )
