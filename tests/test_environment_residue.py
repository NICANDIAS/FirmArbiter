from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from firmarbiter_core.adapter_registry import discover_adapters
from firmarbiter_core.docker_backend import DockerBackend
from firmarbiter_core.docker_supervisor import (
    DockerAdapterSupervisor,
)
from firmarbiter_core.environment_residue import (
    ContainerRecord,
    DeviceMapperRecord,
    HostEnvironmentCollector,
    HostEnvironmentSnapshot,
    LoopDeviceRecord,
    NetworkInterfaceRecord,
    ProcessRecord,
    evaluate_environment_residue,
    write_environment_evidence,
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


def empty_snapshot(
    *,
    errors: dict[str, str] | None = None,
) -> HostEnvironmentSnapshot:
    return HostEnvironmentSnapshot(
        observed_at="2026-06-25T16:30:00Z",
        qemu_processes=(),
        tun_tap_interfaces=(),
        loop_devices=(),
        device_mapper_entries=(),
        containers=(),
        source_errors=errors or {},
    )


def create_contract_root(
    root: Path,
    manifest_sha256: str,
) -> Path:
    firmware_content = b"environment residue test\n"

    input_directory = root / "input"
    input_directory.mkdir(parents=True)

    firmware_path = input_directory / "firmware"
    firmware_path.write_bytes(firmware_content)

    suffix = uuid.uuid4().hex[:12]

    request = {
        "schema_version": "1.0",
        "contract_version": "1.0",
        "run": {
            "experiment_id": "residue-test",
            "run_id": (
                "residue-test.case-001."
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


class EnvironmentResidueUnitTests(unittest.TestCase):
    def test_new_resources_are_detected(self) -> None:
        before = empty_snapshot()

        after = HostEnvironmentSnapshot(
            observed_at="2026-06-25T16:31:00Z",
            qemu_processes=(
                ProcessRecord(
                    pid=5000,
                    start_time_ticks=12345,
                    executable="/usr/bin/qemu-system-mips",
                    command=(
                        "qemu-system-mips",
                        "-M",
                        "malta",
                    ),
                ),
            ),
            tun_tap_interfaces=(
                NetworkInterfaceRecord(
                    name="tap9",
                    ifindex=42,
                    kind="tun",
                    tun_flags="0x1002",
                ),
            ),
            loop_devices=(
                LoopDeviceRecord(
                    name="/dev/loop9",
                    backing_file="/tmp/firmware.img",
                    offset_bytes=0,
                    autoclear=False,
                ),
            ),
            device_mapper_entries=(
                DeviceMapperRecord(
                    name="firmarbiter-test-map",
                    uuid=None,
                    major=253,
                    minor=9,
                ),
            ),
            containers=(
                ContainerRecord(
                    container_id="abc123",
                    name="leftover-container",
                    image="example:test",
                    state="running",
                    labels={},
                ),
            ),
            source_errors={},
        )

        result = evaluate_environment_residue(
            before,
            after,
        )

        self.assertEqual(
            result.status,
            "residue_detected",
        )
        self.assertTrue(result.residue_detected)

        self.assertEqual(
            len(result.added_qemu_processes),
            1,
        )
        self.assertEqual(
            len(result.added_tun_tap_interfaces),
            1,
        )
        self.assertEqual(
            len(result.added_loop_devices),
            1,
        )
        self.assertEqual(
            len(result.added_device_mapper_entries),
            1,
        )
        self.assertEqual(
            len(result.added_containers),
            1,
        )

    def test_removed_resources_are_not_residue(self) -> None:
        existing_process = ProcessRecord(
            pid=100,
            start_time_ticks=500,
            executable="/usr/bin/qemu-system-arm",
            command=("qemu-system-arm",),
        )

        before = HostEnvironmentSnapshot(
            observed_at="2026-06-25T16:30:00Z",
            qemu_processes=(existing_process,),
            tun_tap_interfaces=(),
            loop_devices=(),
            device_mapper_entries=(),
            containers=(),
            source_errors={},
        )

        after = empty_snapshot()

        result = evaluate_environment_residue(
            before,
            after,
        )

        self.assertEqual(result.status, "clean")
        self.assertFalse(result.residue_detected)
        self.assertEqual(
            len(result.removed_qemu_processes),
            1,
        )

    def test_unavailable_source_is_partial_probe(
        self,
    ) -> None:
        before = empty_snapshot(
            errors={
                "device_mapper_entries": (
                    "PermissionError: unavailable"
                )
            }
        )

        after = empty_snapshot(
            errors={
                "device_mapper_entries": (
                    "PermissionError: unavailable"
                )
            }
        )

        result = evaluate_environment_residue(
            before,
            after,
        )

        self.assertEqual(
            result.status,
            "partial_probe",
        )
        self.assertFalse(result.residue_detected)


class EnvironmentResidueDockerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        adapters = discover_adapters(
            ADAPTER_ROOT,
            MANIFEST_SCHEMA,
        )

        cls.adapter = adapters[
            "mock-service-adapter"
        ]

        cls.backend = DockerBackend(
            REQUEST_SCHEMA
        )

        if not cls.backend.docker_available():
            raise RuntimeError(
                "Docker daemon is not available"
            )

        cls.image = cls.backend.build_adapter(
            cls.adapter
        )

    def test_mock_adapter_leaves_no_detected_residue(
        self,
    ) -> None:
        collector = HostEnvironmentCollector()

        with tempfile.TemporaryDirectory() as temporary:
            contract_root = Path(temporary)

            request_path = create_contract_root(
                contract_root,
                self.adapter.manifest_sha256,
            )

            before = collector.snapshot()

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

                watchdog = LifecycleWatchdog(
                    supervisor
                )
                watchdog.start()

                watchdog.wait_for_event(
                    "endpoint_reported",
                    timeout_seconds=10,
                )

                watchdog.request_benchmark_shutdown()

                lifecycle = watchdog.wait(
                    timeout_seconds=10,
                )

                self.assertEqual(
                    lifecycle.run_outcome,
                    "completed",
                )

            finally:
                supervisor.force_terminate()

                # This removes only the known core-created container.
                # It does not remove candidate-created host resources.
                supervisor.remove()

            after = collector.snapshot()

            observation = (
                evaluate_environment_residue(
                    before,
                    after,
                )
            )

            write_environment_evidence(
                contract_root=contract_root,
                before=before,
                after=after,
                observation=observation,
            )

            self.assertFalse(
                observation.residue_detected
            )

            self.assertIn(
                observation.status,
                {"clean", "partial_probe"},
            )

            self.assertEqual(
                len(
                    observation
                    .added_qemu_processes
                ),
                0,
            )
            self.assertEqual(
                len(
                    observation
                    .added_tun_tap_interfaces
                ),
                0,
            )
            self.assertEqual(
                len(
                    observation
                    .added_loop_devices
                ),
                0,
            )
            self.assertEqual(
                len(
                    observation
                    .added_device_mapper_entries
                ),
                0,
            )
            self.assertEqual(
                len(
                    observation
                    .added_containers
                ),
                0,
            )

            artifact_directory = (
                contract_root / "artifacts"
            )

            self.assertTrue(
                (
                    artifact_directory
                    / "environment-before.json"
                ).is_file()
            )
            self.assertTrue(
                (
                    artifact_directory
                    / "environment-after.json"
                ).is_file()
            )
            self.assertTrue(
                (
                    artifact_directory
                    / "environment-residue.json"
                ).is_file()
            )


if __name__ == "__main__":
    unittest.main()
