#!/usr/bin/env python3

from __future__ import annotations

import json
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import (
    BaseHTTPRequestHandler,
    ThreadingHTTPServer,
)
from pathlib import Path
from typing import Any


class FirmwareContentHandler(BaseHTTPRequestHandler):
    response_body = b""

    def do_GET(self) -> None:
        body = type(self).response_body

        self.send_response(200)
        self.send_header(
            "Content-Type",
            "application/octet-stream",
        )
        self.send_header(
            "Content-Length",
            str(len(body)),
        )
        self.end_headers()
        self.wfile.write(body)

    def log_message(
        self,
        format: str,
        *args: Any,
    ) -> None:
        return


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def main() -> int:
    if len(sys.argv) != 2:
        print(
            "Usage: mock_service_adapter.py REQUEST_PATH",
            file=sys.stderr,
        )
        return 2

    request_path = Path(sys.argv[1])
    request = json.loads(
        request_path.read_text(encoding="utf-8")
    )

    run_id = request["run"]["run_id"]
    adapter_id = request["run"]["adapter_id"]

    events_path = Path(request["paths"]["events"])
    control_directory = Path(request["paths"]["control"])
    shutdown_path = control_directory / "shutdown.json"

    firmware_path = Path(request["firmware"]["path"])
    firmware_content = firmware_path.read_bytes()

    FirmwareContentHandler.response_body = firmware_content

    events_path.parent.mkdir(parents=True, exist_ok=True)
    control_directory.mkdir(parents=True, exist_ok=True)

    sequence = 0

    with events_path.open(
        "a",
        encoding="utf-8",
        buffering=1,
    ) as event_file:

        def emit(
            event_name: str,
            **additional_fields: Any,
        ) -> None:
            nonlocal sequence
            sequence += 1

            document = {
                "schema_version": "1.0",
                "contract_version": "1.0",
                "event": event_name,
                "sequence": sequence,
                "timestamp": utc_now(),
                "run_id": run_id,
                "adapter_id": adapter_id,
                **additional_fields,
            }

            event_file.write(
                json.dumps(
                    document,
                    sort_keys=True,
                )
                + "\n"
            )
            event_file.flush()

        emit(
            "adapter_started",
            state="starting",
        )

        emit(
            "candidate_started",
            state="running",
        )

        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            FirmwareContentHandler,
        )

        service_port = int(server.server_address[1])

        server_thread = threading.Thread(
            target=server.serve_forever,
            daemon=True,
        )
        server_thread.start()

        emit(
            "candidate_boot_reported",
            state="running",
        )

        emit(
            "endpoint_reported",
            state="waiting_for_shutdown",
            endpoint={
                "host": "127.0.0.1",
                "port": service_port,
                "protocol": "http",
            },
        )

        heartbeat_interval = max(
            0.1,
            float(
                request["lifecycle"][
                    "heartbeat_interval_seconds"
                ]
            ),
        )

        next_heartbeat = time.monotonic()

        while not shutdown_path.exists():
            current_time = time.monotonic()

            if current_time >= next_heartbeat:
                emit(
                    "heartbeat",
                    state="waiting_for_shutdown",
                )
                next_heartbeat = (
                    current_time + heartbeat_interval
                )

            time.sleep(0.05)

        emit(
            "shutdown_started",
            state="shutting_down",
        )

        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)

        emit(
            "cleanup_complete",
            state="shutting_down",
        )

        emit(
            "adapter_stopped",
            outcome="completed",
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
