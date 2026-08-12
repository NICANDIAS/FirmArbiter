from __future__ import annotations

import hashlib
import json
import socket
import tempfile
import unittest
import uuid
from pathlib import Path

from firmarbiter_core.adapter_registry import discover_adapters
from firmarbiter_core.docker_backend import DockerBackend
from firmarbiter_core.docker_supervisor import (
    DockerAdapterSupervisor,
)
from firmarbiter_core.probe_orchestrator import (
    IndependentProbeOrchestrator,
)
from firmarbiter_core.probes.service_reachability import (
    probe_endpoint_event,
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
    / "docker-probe-adapters"
)


def create_contract_root(
    root: Path,
    manifest_sha256: str,
) -> Path:
    firmware_content = b"neutral service-probe firmware\n"

    input_directory = root / "input"
    input_directory.mkdir(parents=True)

    firmware_path = input_directory / "firmware"
    firmware_path.write_bytes(firmware_content)

    unique_suffix = uuid.uuid4().hex[:12]

    request = {
        "schema_version": "1.0",
        "contract_version": "1.0",
        "run": {
            "experiment_id": "reachability-test",
            "run_id": (
                "reachability-test.case-001."
                f"mock-service-adapter.{unique_suffix}"
            ),
            "adapter_id": "mock-service-adapter",
            "attempt": 1,
            "created_at": "2026-06-25T16:30:00Z",
            "requested_stages": [
                "emulate",
                "endpoint-discovery"
            ]
        },
        "firmware": {
            "case_id": "case-001",
            "path": "/firmarbiter/input/firmware",
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
            "network": "host",
            "requirements": []
        },
        "paths": {
            "workspace": "/firmarbiter/work",
            "artifacts": "/firmarbiter/artifacts",
            "events": "/firmarbiter/events/events.jsonl",
            "control": "/firmarbiter/control"
        },
        "integrity": {
            "adapter_manifest_sha256": manifest_sha256,
            "experiment_manifest_sha256": "b" * 64
        }
    }

    request_path = input_directory / "request.json"

    request_path.write_text(
        json.dumps(request, indent=2) + "\n",
        encoding="utf-8",
    )

    return request_path


class IndependentReachabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        adapters = discover_adapters(
            ADAPTER_ROOT,
            MANIFEST_SCHEMA,
        )

        cls.adapter = adapters["mock-service-adapter"]
        cls.backend = DockerBackend(REQUEST_SCHEMA)

        if not cls.backend.docker_available():
            raise RuntimeError(
                "Docker daemon is not available"
            )

        cls.image = cls.backend.build_adapter(
            cls.adapter
        )

    def test_reachability_is_measured_while_alive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            contract_root = Path(temporary)

            request_path = create_contract_root(
                contract_root,
                self.adapter.manifest_sha256,
            )

            supervisor = DockerAdapterSupervisor(
                backend=self.backend,
                adapter=self.adapter,
                image=self.image,
                request_path=request_path,
                contract_root=contract_root,
                event_schema_path=EVENT_SCHEMA,
            )

            orchestrator = IndependentProbeOrchestrator()

            try:
                supervisor.start()

                endpoint_event = supervisor.wait_for_event(
                    "endpoint_reported",
                    timeout_seconds=10,
                )

                self.assertTrue(
                    supervisor.container_running
                )

                records = orchestrator.handle_event(
                    endpoint_event
                )

                self.assertEqual(len(records), 1)

                record = records[0]

                self.assertEqual(
                    record.independent_measurement.status,
                    "true",
                )

                self.assertGreaterEqual(
                    record.independent_measurement.attempts,
                    1,
                )

                self.assertIsNotNone(
                    record.independent_measurement.latency_ms
                )

                self.assertEqual(
                    record.candidate_claim,
                    endpoint_event["endpoint"],
                )

                # Candidate claim remains separate from measurement.
                self.assertNotIn(
                    "reachable",
                    endpoint_event,
                )

                self.assertNotIn(
                    "verified",
                    endpoint_event,
                )

                # The service is still alive after verification.
                self.assertTrue(
                    supervisor.container_running
                )

                supervisor.request_shutdown()

                exit_code = supervisor.wait_for_exit(
                    timeout_seconds=5,
                )

                self.assertEqual(exit_code, 0)

                # The same endpoint must no longer be reachable.
                post_shutdown = probe_endpoint_event(
                    endpoint_event,
                    timeout_seconds=0.5,
                    attempts=2,
                )

                self.assertEqual(
                    post_shutdown.status,
                    "false",
                )

            finally:
                supervisor.force_terminate()
                supervisor.remove()

    def test_closed_port_is_not_false_positive(self) -> None:
        reserved_socket = socket.socket(
            socket.AF_INET,
            socket.SOCK_STREAM,
        )

        try:
            reserved_socket.bind(("127.0.0.1", 0))
            closed_port = int(
                reserved_socket.getsockname()[1]
            )

            event = {
                "event": "endpoint_reported",
                "sequence": 1,
                "endpoint": {
                    "host": "127.0.0.1",
                    "port": closed_port,
                    "protocol": "http",
                },
            }

            observation = probe_endpoint_event(
                event,
                timeout_seconds=0.5,
                attempts=2,
            )

            self.assertEqual(
                observation.status,
                "false",
            )

            self.assertIsNone(
                observation.latency_ms,
            )

        finally:
            reserved_socket.close()

    def test_udp_is_not_marked_false(self) -> None:
        event = {
            "event": "endpoint_reported",
            "sequence": 1,
            "endpoint": {
                "host": "127.0.0.1",
                "port": 9999,
                "protocol": "udp",
            },
        }

        observation = probe_endpoint_event(event)

        self.assertEqual(
            observation.status,
            "not_applicable",
        )


if __name__ == "__main__":
    unittest.main()
