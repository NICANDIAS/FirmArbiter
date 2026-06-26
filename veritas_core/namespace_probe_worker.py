#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import http.client
import json
import socket
import ssl
import sys
import time
from datetime import datetime, timezone
from typing import Any


TCP_PROTOCOLS = {
    "tcp",
    "http",
    "https",
    "ssh",
    "telnet",
}


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def load_payload() -> dict[str, Any]:
    if len(sys.argv) != 3:
        raise SystemExit(
            "Usage: worker.py OPERATION JSON_PAYLOAD"
        )

    payload = json.loads(sys.argv[2])

    if not isinstance(payload, dict):
        raise ValueError("Probe payload must be an object")

    return payload


def endpoint_fields(
    payload: dict[str, Any],
) -> tuple[str, int, str]:
    endpoint = payload.get("endpoint")

    if not isinstance(endpoint, dict):
        raise ValueError("Probe payload has no endpoint object")

    host = str(endpoint["host"])
    port = int(endpoint["port"])
    protocol = str(endpoint["protocol"])

    if not host:
        raise ValueError("Endpoint host must not be empty")

    if not 1 <= port <= 65535:
        raise ValueError("Endpoint port is invalid")

    return host, port, protocol


def probe_reachability(
    payload: dict[str, Any],
) -> dict[str, Any]:
    host, port, protocol = endpoint_fields(payload)
    timeout_seconds = float(
        payload.get("timeout_seconds", 2.0)
    )
    attempts = int(payload.get("attempts", 1))
    retry_delay = float(
        payload.get("retry_delay_seconds", 0.1)
    )

    if protocol not in TCP_PROTOCOLS:
        return {
            "metric": "service_reachability",
            "status": "not_applicable",
            "observed_at": utc_now(),
            "host": host,
            "port": port,
            "protocol": protocol,
            "attempts": 0,
            "latency_ms": None,
            "error_type": None,
            "error_message": (
                "Transport-level TCP reachability is not "
                f"applicable to protocol {protocol!r}"
            ),
        }

    final_exception: OSError | None = None

    for attempt_number in range(1, attempts + 1):
        started = time.monotonic()

        try:
            with socket.create_connection(
                (host, port),
                timeout=timeout_seconds,
            ):
                return {
                    "metric": "service_reachability",
                    "status": "true",
                    "observed_at": utc_now(),
                    "host": host,
                    "port": port,
                    "protocol": protocol,
                    "attempts": attempt_number,
                    "latency_ms": round(
                        (
                            time.monotonic() - started
                        )
                        * 1000,
                        3,
                    ),
                    "error_type": None,
                    "error_message": None,
                }

        except OSError as exc:
            final_exception = exc

            if attempt_number < attempts:
                time.sleep(retry_delay)

    return {
        "metric": "service_reachability",
        "status": "false",
        "observed_at": utc_now(),
        "host": host,
        "port": port,
        "protocol": protocol,
        "attempts": attempts,
        "latency_ms": None,
        "error_type": (
            type(final_exception).__name__
            if final_exception is not None
            else None
        ),
        "error_message": (
            str(final_exception)
            if final_exception is not None
            else None
        ),
    }


def capture_http(
    payload: dict[str, Any],
) -> dict[str, Any]:
    host, port, protocol = endpoint_fields(payload)
    timeout_seconds = float(
        payload.get("timeout_seconds", 3.0)
    )
    max_body_bytes = int(
        payload.get("max_body_bytes", 1024 * 1024)
    )
    request_path = "/"

    base = {
        "observed_at": utc_now(),
        "host": host,
        "port": port,
        "protocol": protocol,
        "request_path": request_path,
    }

    if protocol not in {"http", "https"}:
        return {
            **base,
            "status": "not_applicable",
            "http_status": None,
            "body_sha256": None,
            "body_size_bytes": None,
            "body_truncated": None,
            "content_type": None,
            "server_header": None,
            "response_fingerprint_sha256": None,
            "latency_ms": None,
            "error_type": None,
            "error_message": (
                f"HTTP snapshot is not applicable to "
                f"{protocol!r}"
            ),
        }

    if protocol == "https":
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        connection: http.client.HTTPConnection = (
            http.client.HTTPSConnection(
                host,
                port,
                timeout=timeout_seconds,
                context=context,
            )
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
                "User-Agent": (
                    "VERITAS-Independent-Probe/1.0"
                ),
                "Accept": "*/*",
                "Connection": "close",
            },
        )
        response = connection.getresponse()
        received = response.read(max_body_bytes + 1)
        body_truncated = (
            len(received) > max_body_bytes
        )
        retained_body = received[:max_body_bytes]
        body_sha256 = hashlib.sha256(
            retained_body
        ).hexdigest()
        content_type = response.getheader(
            "Content-Type"
        )
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

        return {
            **base,
            "status": "captured",
            "http_status": int(response.status),
            "body_sha256": body_sha256,
            "body_size_bytes": len(retained_body),
            "body_truncated": body_truncated,
            "content_type": content_type,
            "server_header": server_header,
            "response_fingerprint_sha256": (
                response_fingerprint
            ),
            "latency_ms": round(
                (
                    time.monotonic() - started
                )
                * 1000,
                3,
            ),
            "error_type": None,
            "error_message": None,
        }

    except (
        ConnectionRefusedError,
        ConnectionResetError,
        TimeoutError,
        socket.timeout,
        socket.gaierror,
        ssl.SSLError,
        OSError,
    ) as exc:
        return {
            **base,
            "status": "unreachable",
            "http_status": None,
            "body_sha256": None,
            "body_size_bytes": None,
            "body_truncated": None,
            "content_type": None,
            "server_header": None,
            "response_fingerprint_sha256": None,
            "latency_ms": None,
            "error_type": type(exc).__name__,
            "error_message": str(exc),
        }

    except http.client.HTTPException as exc:
        return {
            **base,
            "status": "probe_error",
            "http_status": None,
            "body_sha256": None,
            "body_size_bytes": None,
            "body_truncated": None,
            "content_type": None,
            "server_header": None,
            "response_fingerprint_sha256": None,
            "latency_ms": None,
            "error_type": type(exc).__name__,
            "error_message": str(exc),
        }

    finally:
        connection.close()


def main() -> int:
    operation = sys.argv[1]
    payload = load_payload()

    if operation == "reachability":
        result = probe_reachability(payload)
    elif operation == "http_snapshot":
        result = capture_http(payload)
    else:
        raise ValueError(
            f"Unsupported probe operation: {operation}"
        )

    print(
        json.dumps(
            result,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
