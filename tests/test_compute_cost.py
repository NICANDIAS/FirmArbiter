from __future__ import annotations

import hashlib
import json
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from firmarbiter_core.adapter_registry import discover_adapters
from firmarbiter_core.compute_cost import (
    DockerComputeCostSampler,
    _configured_memory_limit,
    parse_percentage,
    parse_size_bytes,
    parse_usage_pair,
    resource_sample_from_docker,
)
from firmarbiter_core.docker_backend import DockerBackend
from firmarbiter_core.docker_supervisor import (
    DockerAdapterSupervisor,
)
from firmarbiter_core.lifecycle_watchdog import (
    LifecycleWatchdog,
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
    firmware_content = b"compute-cost test firmware\n"

    input_directory = root / "input"
    input_directory.mkdir(parents=True)

    firmware_path = input_directory / "firmware"
    firmware_path.write_bytes(firmware_content)

    suffix = uuid.uuid4().hex[:12]

    request = {
        "schema_version": "1.0",
        "contract_version": "1.0",
        "run": {
            "experiment_id": "compute-cost-test",
            "run_id": (
                "compute-cost-test.case-001."
                f"mock-service-adapter.{suffix}"
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


class ComputeCostParsingTests(unittest.TestCase):
    def test_size_parsing(self) -> None:
        self.assertEqual(
            parse_size_bytes("1KiB"),
            1024,
        )
        self.assertEqual(
            parse_size_bytes("1.5MiB"),
            1572864,
        )
        self.assertEqual(
            parse_size_bytes("2 MB"),
            2000000,
        )
        self.assertEqual(
            parse_usage_pair("3MiB / 256MiB"),
            (3145728, 268435456),
        )
    def test_configured_memory_limit_from_inspect(
        self,
    ) -> None:
        inspection = {
            "HostConfig": {
                "Memory": 268435456,
            }
        }

        self.assertEqual(
            _configured_memory_limit(inspection),
            268435456,
        )

    def test_zero_or_missing_configured_limit(
        self,
    ) -> None:
        self.assertIsNone(
            _configured_memory_limit(
                {"HostConfig": {"Memory": 0}}
            )
        )

        self.assertIsNone(
            _configured_memory_limit({})
        )

    def test_resource_sample_parsing(self) -> None:
        document = {
            "CPUPerc": "12.50%",
            "MemUsage": "10MiB / 256MiB",
            "MemPerc": "3.91%",
            "NetIO": "2kB / 3kB",
            "BlockIO": "4KiB / 5KiB",
            "PIDs": "7",
        }

        sample = resource_sample_from_docker(
            document,
            sequence=1,
            observed_at="2026-06-25T16:30:00Z",
            offset_seconds=0.5,
        )

        self.assertEqual(sample.cpu_percent, 12.5)
        self.assertEqual(
            sample.memory_usage_bytes,
            10 * 1024 * 1024,
        )
        self.assertEqual(sample.network_rx_bytes, 2000)
        self.assertEqual(sample.block_write_bytes, 5120)
        self.assertEqual(sample.pids_tasks, 7)
        self.assertEqual(
            parse_percentage("0.00%"),
            0.0,
        )


class ComputeCostDockerTests(unittest.TestCase):
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

    def test_container_compute_cost_is_recorded(
        self,
    ) -> None:
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

            sampler = None

            try:
                supervisor.start()

                sampler = DockerComputeCostSampler(
                    backend=self.backend,
                    container_id=supervisor.container_id,
                    contract_root=contract_root,
                    sample_interval_seconds=0.25,
                )
                sampler.start()

                watchdog = LifecycleWatchdog(supervisor)
                watchdog.start()

                watchdog.wait_for_event(
                    "endpoint_reported",
                    timeout_seconds=10,
                )

                watchdog.wait_for_event(
                    "heartbeat",
                    timeout_seconds=5,
                )

                # Permit multiple independent resource samples.
                time.sleep(1.0)

                watchdog.request_benchmark_shutdown()

                lifecycle = watchdog.wait(
                    timeout_seconds=10,
                )

                self.assertEqual(
                    lifecycle.run_outcome,
                    "completed",
                )

                cost = sampler.wait(
                    timeout_seconds=10,
                )

                self.assertIn(
                    cost.status,
                    {"complete", "partial"},
                )

                self.assertGreaterEqual(
                    cost.samples_collected,
                    1,
                )

                self.assertIsNotNone(
                    cost.peak_memory_usage_bytes
                )

                self.assertGreater(
                    cost.peak_memory_usage_bytes,
                    0,
                )

                self.assertIsNotNone(
                    cost.peak_pids_tasks
                )

                self.assertGreaterEqual(
                    cost.peak_pids_tasks,
                    1,
                )

                self.assertEqual(
                    cost.memory_limit_bytes,
                    268435456,
                )

                self.assertEqual(cost.exit_code, 0)
                self.assertFalse(cost.oom_killed)

                self.assertEqual(
                    cost.image_id,
                    self.image.image_id,
                )

                result_path = (
                    contract_root
                    / "artifacts"
                    / "compute-cost-observation.json"
                )

                self.assertTrue(result_path.is_file())

                saved = json.loads(
                    result_path.read_text(
                        encoding="utf-8"
                    )
                )

                self.assertEqual(
                    saved["metric"],
                    "compute_cost",
                )

                self.assertEqual(
                    saved["samples_collected"],
                    cost.samples_collected,
                )

            finally:
                if sampler is not None:
                    sampler.request_stop()

                supervisor.force_terminate()
                supervisor.remove()


if __name__ == "__main__":
    unittest.main()
