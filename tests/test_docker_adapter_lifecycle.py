from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from veritas_core.adapter_registry import discover_adapters
from veritas_core.docker_backend import DockerBackend
from veritas_core.docker_supervisor import (
    DockerAdapterSupervisor,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]

MANIFEST_SCHEMA = (
    PROJECT_ROOT
    / "schemas"
    / "adapter-manifest-v1.schema.json"
)

REQUEST_SCHEMA = (
    PROJECT_ROOT
    / "schemas"
    / "run-request-v1.schema.json"
)

EVENT_SCHEMA = (
    PROJECT_ROOT
    / "schemas"
    / "adapter-event-v1.schema.json"
)

ADAPTER_ROOT = (
    PROJECT_ROOT
    / "tests"
    / "fixtures"
    / "docker-adapters"
)


def create_contract_root(root: Path) -> Path:
    firmware_content = b"immutable docker test firmware\n"

    input_directory = root / "input"
    input_directory.mkdir(parents=True)

    firmware_path = input_directory / "firmware"
    firmware_path.write_bytes(firmware_content)

    request = {
        "schema_version": "1.0",
        "contract_version": "1.0",
        "run": {
            "experiment_id": "docker-contract-test",
            "run_id": (
                "docker-contract-test.case-001."
                "mock-docker-adapter.attempt-1"
            ),
            "adapter_id": "mock-docker-adapter",
            "attempt": 1,
            "created_at": "2026-06-25T16:30:00Z",
            "requested_stages": [
                "unpack",
                "emulate",
                "endpoint-discovery"
            ]
        },
        "firmware": {
            "case_id": "case-001",
            "path": "/veritas/input/firmware",
            "sha256": hashlib.sha256(
                firmware_content
            ).hexdigest(),
            "size_bytes": len(firmware_content),
            "delivery_semantics": "opaque-original-bytes",
            "read_only": True
        },
        "lifecycle": {
            "timeout_seconds": 30,
            "heartbeat_interval_seconds": 1,
            "heartbeat_timeout_seconds": 3,
            "shutdown_grace_seconds": 5
        },
        "resources": {
            "cpu_cores": 1,
            "memory_bytes": 268435456,
            "pids_limit": 128
        },
        "runtime_grants": {
            "run_as_root": False,
            "network": "none",
            "requirements": []
        },
        "paths": {
            "workspace": "/veritas/work",
            "artifacts": "/veritas/artifacts",
            "events": "/veritas/events/events.jsonl",
            "control": "/veritas/control"
        },
        "integrity": {
            "adapter_manifest_sha256": "a" * 64,
            "experiment_manifest_sha256": "b" * 64
        }
    }

    request_path = input_directory / "request.json"

    request_path.write_text(
        json.dumps(request, indent=2) + "\n",
        encoding="utf-8",
    )

    return request_path


class DockerAdapterLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        adapters = discover_adapters(
            ADAPTER_ROOT,
            MANIFEST_SCHEMA,
        )

        cls.adapter = adapters["mock-docker-adapter"]
        cls.backend = DockerBackend(REQUEST_SCHEMA)

        if not cls.backend.docker_available():
            raise RuntimeError(
                "Docker daemon is not available"
            )

        cls.image = cls.backend.build_adapter(
            cls.adapter
        )

    def test_containerised_adapter_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            contract_root = Path(temporary)
            request_path = create_contract_root(
                contract_root
            )

            supervisor = DockerAdapterSupervisor(
                backend=self.backend,
                adapter=self.adapter,
                image=self.image,
                request_path=request_path,
                contract_root=contract_root,
                event_schema_path=EVENT_SCHEMA,
            )

            try:
                supervisor.start()

                endpoint = supervisor.wait_for_event(
                    "endpoint_reported",
                    timeout_seconds=10,
                )

                self.assertEqual(
                    endpoint["endpoint"]["port"],
                    8080,
                )

                # Reporting an endpoint must not end execution.
                self.assertTrue(
                    supervisor.container_running
                )

                supervisor.wait_for_event(
                    "heartbeat",
                    timeout_seconds=5,
                )

                self.assertTrue(
                    supervisor.container_running
                )

                inspection = supervisor.inspect()

                self.assertEqual(
                    inspection["Image"],
                    self.image.image_id,
                )

                self.assertEqual(
                    inspection["Config"]["Labels"][
                        "veritas.adapter_id"
                    ],
                    "mock-docker-adapter",
                )

                input_mounts = [
                    mount
                    for mount in inspection["Mounts"]
                    if mount["Destination"]
                    == "/veritas/input"
                ]

                self.assertEqual(
                    len(input_mounts),
                    1,
                )

                self.assertFalse(
                    input_mounts[0]["RW"]
                )

                host_config = inspection["HostConfig"]

                self.assertEqual(
                    host_config["NetworkMode"],
                    "none",
                )

                self.assertEqual(
                    host_config["Memory"],
                    268435456,
                )

                self.assertEqual(
                    host_config["PidsLimit"],
                    128,
                )

                # Endpoint events remain candidate claims.
                self.assertNotIn("verified", endpoint)
                self.assertNotIn("reachable", endpoint)

                supervisor.request_shutdown()

                exit_code = supervisor.wait_for_exit(
                    timeout_seconds=5,
                )

                self.assertEqual(exit_code, 0)

                event_names = [
                    event["event"]
                    for event in supervisor.events
                ]

                self.assertIn(
                    "shutdown_started",
                    event_names,
                )

                self.assertIn(
                    "cleanup_complete",
                    event_names,
                )

                self.assertEqual(
                    event_names[-1],
                    "adapter_stopped",
                )

                self.assertTrue(
                    self.image.image_id.startswith(
                        "sha256:"
                    )
                )

            finally:
                supervisor.force_terminate()
                supervisor.remove()


if __name__ == "__main__":
    unittest.main()
