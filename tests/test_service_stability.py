from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from veritas_core.adapter_registry import discover_adapters
from veritas_core.docker_backend import DockerBackend
from veritas_core.docker_supervisor import (
    DockerAdapterSupervisor,
)
from veritas_core.probe_orchestrator import (
    IndependentProbeOrchestrator,
)
from veritas_core.probes.service_reachability import (
    ReachabilityObservation,
)
from veritas_core.probes.service_stability import (
    measure_endpoint_stability,
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
    firmware_content = b"stability test firmware\n"

    input_directory = root / "input"
    input_directory.mkdir(parents=True)

    firmware_path = input_directory / "firmware"
    firmware_path.write_bytes(firmware_content)

    unique_suffix = uuid.uuid4().hex[:12]

    request = {
        "schema_version": "1.0",
        "contract_version": "1.0",
        "run": {
            "experiment_id": "stability-test",
            "run_id": (
                "stability-test.case-001."
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
            "network": "host",
            "requirements": []
        },
        "paths": {
            "workspace": "/veritas/work",
            "artifacts": "/veritas/artifacts",
            "events": "/veritas/events/events.jsonl",
            "control": "/veritas/control"
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


def reachability(
    status: str,
) -> ReachabilityObservation:
    return ReachabilityObservation(
        metric="service_reachability",
        status=status,
        observed_at="2026-06-25T16:30:00Z",
        host="127.0.0.1",
        port=12345,
        protocol="tcp",
        attempts=1,
        latency_ms=1.0 if status == "true" else None,
        error_type=None if status == "true" else "ConnectionRefusedError",
        error_message=None if status == "true" else "refused",
    )


class ServiceStabilityTests(unittest.TestCase):
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

    def test_stable_container_service_is_true(self) -> None:
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

                endpoint = supervisor.wait_for_event(
                    "endpoint_reported",
                    timeout_seconds=10,
                )

                orchestrator.handle_event(endpoint)

                records = orchestrator.measure_stability(
                    sample_count=3,
                    interval_seconds=0.2,
                    timeout_seconds=1.0,
                    should_continue=(
                        lambda: supervisor.container_running
                    ),
                )

                self.assertEqual(len(records), 1)

                observation = records[
                    0
                ].independent_measurement

                self.assertEqual(
                    observation.status,
                    "true",
                )

                self.assertEqual(
                    observation.completed_samples,
                    3,
                )

                self.assertEqual(
                    observation.successful_samples,
                    3,
                )

                self.assertEqual(
                    observation.failed_samples,
                    0,
                )

                self.assertEqual(
                    observation.availability_ratio,
                    1.0,
                )

                self.assertTrue(
                    supervisor.container_running
                )

                supervisor.request_shutdown()

                self.assertEqual(
                    supervisor.wait_for_exit(
                        timeout_seconds=5,
                    ),
                    0,
                )

            finally:
                supervisor.force_terminate()
                supervisor.remove()

    def test_intermittent_service_is_false(self) -> None:
        event = {
            "event": "endpoint_reported",
            "sequence": 1,
            "endpoint": {
                "host": "127.0.0.1",
                "port": 12345,
                "protocol": "tcp",
            },
        }

        observations = [
            reachability("true"),
            reachability("false"),
            reachability("true"),
        ]

        with patch(
            "veritas_core.probes.service_stability."
            "probe_endpoint_event",
            side_effect=observations,
        ):
            result = measure_endpoint_stability(
                event,
                sample_count=3,
                interval_seconds=0,
                timeout_seconds=1.0,
            )

        self.assertEqual(result.status, "false")
        self.assertEqual(result.successful_samples, 2)
        self.assertEqual(result.failed_samples, 1)
        self.assertEqual(
            result.availability_ratio,
            round(2 / 3, 6),
        )
        self.assertEqual(
            result.longest_failure_streak,
            1,
        )

    def test_interrupted_window_is_inconclusive(
        self,
    ) -> None:
        event = {
            "event": "endpoint_reported",
            "sequence": 1,
            "endpoint": {
                "host": "127.0.0.1",
                "port": 12345,
                "protocol": "tcp",
            },
        }

        lifecycle_checks = iter(
            [True, True, False]
        )

        def candidate_alive() -> bool:
            return next(
                lifecycle_checks,
                False,
            )

        with patch(
            "veritas_core.probes.service_stability."
            "probe_endpoint_event",
            return_value=reachability("true"),
        ):
            result = measure_endpoint_stability(
                event,
                sample_count=3,
                interval_seconds=0,
                timeout_seconds=1.0,
                should_continue=candidate_alive,
            )

        self.assertEqual(
            result.status,
            "inconclusive",
        )

        self.assertGreaterEqual(
            result.completed_samples,
            1,
        )

        self.assertLess(
            result.completed_samples,
            3,
        )


if __name__ == "__main__":
    unittest.main()
