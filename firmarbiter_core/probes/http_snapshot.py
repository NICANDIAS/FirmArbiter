from __future__ import annotations

import hashlib
import http.client
import json
import socket
import ssl
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class HttpServiceSnapshot:
    """
    Independently captured HTTP service evidence.

    The body hash covers the retained response bytes. If body_truncated is
    true, that hash must not be treated as a complete-file hash.
    """

    status: str
    observed_at: str
    host: str
    port: int
    protocol: str
    request_path: str
    http_status: int | None
    body_sha256: str | None
    body_size_bytes: int | None
    body_truncated: bool | None
    content_type: str | None
    server_header: str | None
    response_fingerprint_sha256: str | None
    latency_ms: float | None
    error_type: str | None
    error_message: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class HttpSnapshotError(RuntimeError):
    """Raised when an endpoint claim is malformed."""


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def capture_http_snapshot(
    event: dict[str, Any],
    *,
    timeout_seconds: float = 3.0,
    max_body_bytes: int = 1024 * 1024,
) -> HttpServiceSnapshot:
    """
    Capture an HTTP response independently of candidate console output.

    TLS certificate verification is deliberately disabled for HTTPS because
    embedded firmware commonly uses self-signed certificates. Certificate
    evidence will be recorded by a later TLS-specific probe.
    """
    if event.get("event") != "endpoint_reported":
        raise HttpSnapshotError(
            "HTTP snapshot requires an endpoint_reported event"
        )

    endpoint = event.get("endpoint")

    if not isinstance(endpoint, dict):
        raise HttpSnapshotError(
            "endpoint_reported event has no endpoint object"
        )

    try:
        host = str(endpoint["host"])
        port = int(endpoint["port"])
        protocol = str(endpoint["protocol"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HttpSnapshotError(
            "Endpoint claim is incomplete or malformed"
        ) from exc

    request_path = "/"

    if protocol not in {"http", "https"}:
        return HttpServiceSnapshot(
            status="not_applicable",
            observed_at=_utc_now(),
            host=host,
            port=port,
            protocol=protocol,
            request_path=request_path,
            http_status=None,
            body_sha256=None,
            body_size_bytes=None,
            body_truncated=None,
            content_type=None,
            server_header=None,
            response_fingerprint_sha256=None,
            latency_ms=None,
            error_type=None,
            error_message=(
                f"HTTP snapshot is not applicable to {protocol!r}"
            ),
        )

    connection: http.client.HTTPConnection

    if protocol == "https":
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

        connection = http.client.HTTPSConnection(
            host,
            port,
            timeout=timeout_seconds,
            context=context,
        )
    else:
        connection = http.client.HTTPConnection(
            host,
            port,
            timeout=timeout_seconds,
        )

    started = time.monotonic()

    try:
        connection.request(
            "GET",
            request_path,
            headers={
                "User-Agent": "FIRMARBITER-Independent-Probe/1.0",
                "Accept": "*/*",
                "Connection": "close",
            },
        )

        response = connection.getresponse()

        received = response.read(max_body_bytes + 1)
        body_truncated = len(received) > max_body_bytes
        retained_body = received[:max_body_bytes]

        latency_ms = round(
            (time.monotonic() - started) * 1000,
            3,
        )

        body_sha256 = hashlib.sha256(
            retained_body
        ).hexdigest()

        content_type = response.getheader("Content-Type")
        server_header = response.getheader("Server")

        fingerprint_document = {
            "protocol": protocol,
            "http_status": int(response.status),
            "body_sha256": body_sha256,
            "body_truncated": body_truncated,
            "content_type": content_type,
            "server_header": server_header,
        }

        response_fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_document,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

        return HttpServiceSnapshot(
            status="captured",
            observed_at=_utc_now(),
            host=host,
            port=port,
            protocol=protocol,
            request_path=request_path,
            http_status=int(response.status),
            body_sha256=body_sha256,
            body_size_bytes=len(retained_body),
            body_truncated=body_truncated,
            content_type=content_type,
            server_header=server_header,
            response_fingerprint_sha256=response_fingerprint,
            latency_ms=latency_ms,
            error_type=None,
            error_message=None,
        )

    except (
        ConnectionRefusedError,
        ConnectionResetError,
        TimeoutError,
        socket.timeout,
        socket.gaierror,
        ssl.SSLError,
        OSError,
    ) as exc:
        return HttpServiceSnapshot(
            status="unreachable",
            observed_at=_utc_now(),
            host=host,
            port=port,
            protocol=protocol,
            request_path=request_path,
            http_status=None,
            body_sha256=None,
            body_size_bytes=None,
            body_truncated=None,
            content_type=None,
            server_header=None,
            response_fingerprint_sha256=None,
            latency_ms=None,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )

    except http.client.HTTPException as exc:
        return HttpServiceSnapshot(
            status="probe_error",
            observed_at=_utc_now(),
            host=host,
            port=port,
            protocol=protocol,
            request_path=request_path,
            http_status=None,
            body_sha256=None,
            body_size_bytes=None,
            body_truncated=None,
            content_type=None,
            server_header=None,
            response_fingerprint_sha256=None,
            latency_ms=None,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )

    finally:
        connection.close()
