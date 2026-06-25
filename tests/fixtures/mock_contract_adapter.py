#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def map_contract_path(contract_path: str) -> Path:
    """
    Map /veritas paths into a temporary host directory for tests.

    Real container adapters will use the /veritas paths directly.
    """
    root_value = os.environ.get("VERITAS_CONTRACT_ROOT")

    if not root_value:
        return Path(contract_path)

    relative = PurePosixPath(contract_path).relative_to(
        "/veritas"
    )

    return Path(root_value) / Path(*relative.parts)


def main() -> int:
    if len(sys.argv) != 2:
        print(
            "Usage: mock_contract_adapter.py REQUEST_PATH",
            file=sys.stderr,
        )
        return 2

    request_path = Path(sys.argv[1])
    request = json.loads(
        request_path.read_text(encoding="utf-8")
    )

    run_id = request["run"]["run_id"]
    adapter_id = request["run"]["adapter_id"]

    events_path = map_contract_path(
        request["paths"]["events"]
    )
    control_directory = map_contract_path(
        request["paths"]["control"]
    )

    shutdown_path = control_directory / "shutdown.json"

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

        time.sleep(0.1)

        # This is a candidate claim, not an independently verified metric.
        emit(
            "candidate_boot_reported",
            state="running",
        )

        # 192.0.2.0/24 is reserved for documentation and testing.
        emit(
            "endpoint_reported",
            state="waiting_for_shutdown",
            endpoint={
                "host": "192.0.2.10",
                "port": 8080,
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

        # A real adapter would stop its native candidate here.
        time.sleep(0.1)

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
