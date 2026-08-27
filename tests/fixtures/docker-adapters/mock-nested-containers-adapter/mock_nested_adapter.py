#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import subprocess
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
    root_value = os.environ.get("FIRMARBITER_CONTRACT_ROOT")

    if not root_value:
        return Path(contract_path)

    relative = PurePosixPath(contract_path).relative_to(
        "/firmarbiter"
    )

    return Path(root_value) / Path(*relative.parts)


def check_nested_docker() -> tuple[str, str]:
    """
    The one real thing this fixture does. Returns (stage_outcome, message).

    Deliberately does NOT read DOCKER_HOST and pass it to `docker`
    explicitly — a real candidate's own `docker` CLI (or docker-compose,
    or the Python docker SDK's docker.from_env()) picks up DOCKER_HOST
    from the environment automatically, so this checks that the
    environment variable alone is enough, the same way a real candidate
    would use it.
    """
    docker_host = os.environ.get("DOCKER_HOST")

    if not docker_host:
        return (
            "failed",
            "DOCKER_HOST is not set — nested-containers requirement "
            "was not granted, or the env var was not injected",
        )

    try:
        result = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except FileNotFoundError:
        return (
            "failed",
            "docker CLI not found inside this adapter container",
        )
    except subprocess.TimeoutExpired:
        return (
            "failed",
            f"docker version timed out against DOCKER_HOST={docker_host}",
        )

    if result.returncode != 0:
        return (
            "failed",
            f"docker version failed against DOCKER_HOST={docker_host}: "
            f"{result.stderr.strip()}",
        )

    server_version = result.stdout.strip()
    return (
        "succeeded",
        f"Reached nested Docker daemon at DOCKER_HOST={docker_host}, "
        f"server version {server_version}",
    )


def main() -> int:
    if len(sys.argv) != 2:
        print(
            "Usage: mock_nested_adapter.py REQUEST_PATH",
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

        stage_outcome, message = check_nested_docker()

        emit(
            "stage_completed",
            state="waiting_for_shutdown",
            stage="emulate",
            stage_outcome=stage_outcome,
            message=message,
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
