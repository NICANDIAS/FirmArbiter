from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from firmarbiter_core.adapter_registry import discover_adapters
from firmarbiter_core.docker_backend import DockerBackend
from firmarbiter_core.docker_supervisor import (
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
    firmware_content = b"immutable nested-containers test firmware\n"

    input_directory = root / "input"
    input_directory.mkdir(parents=True)

    firmware_path = input_directory / "firmware"
    firmware_path.write_bytes(firmware_content)

    request = {
        "schema_version": "1.0",
        "contract_version": "1.0",
        "run": {
            "experiment_id": "nested-containers-contract-test",
            "run_id": (
                "nested-containers-contract-test.case-001."
                "mock-nested-containers-adapter.attempt-1"
            ),
            "adapter_id": "mock-nested-containers-adapter",
            "attempt": 1,
            "created_at": "2026-08-27T00:00:00Z",
            "requested_stages": [
                "unpack",
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
            "timeout_seconds": 120,
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
            "requirements": ["nested-containers"]
        },
        "paths": {
            "workspace": "/firmarbiter/work",
            "artifacts": "/firmarbiter/artifacts",
            "events": "/firmarbiter/events/events.jsonl",
            "control": "/firmarbiter/control"
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


class NestedContainersLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        adapters = discover_adapters(
            ADAPTER_ROOT,
            MANIFEST_SCHEMA,
        )

        cls.adapter = adapters[
            "mock-nested-containers-adapter"
        ]
        cls.backend = DockerBackend(REQUEST_SCHEMA)

        if not cls.backend.docker_available():
            raise RuntimeError(
                "Docker daemon is not available"
            )

        cls.image = cls.backend.build_adapter(
            cls.adapter
        )

    def test_nested_containers_reaches_real_daemon(
        self,
    ) -> None:
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

            sidecar_container_id: str | None = None
            network_name: str | None = None

            try:
                supervisor.start()

                self.assertIsNotNone(supervisor.container)
                sidecar_container_id = (
                    supervisor.container
                    .sidecar_container_id
                )
                network_name = (
                    supervisor.container.network_name
                )

                self.assertIsNotNone(
                    sidecar_container_id,
                    "create_container() did not create a "
                    "DinD sidecar for a candidate that "
                    "declared nested-containers",
                )
                self.assertIsNotNone(
                    network_name,
                    "create_container() did not create a "
                    "per-run network for nested-containers",
                )

                stage_event = supervisor.wait_for_event(
                    "stage_completed",
                    timeout_seconds=90,
                )

                self.assertEqual(
                    stage_event["stage_outcome"],
                    "succeeded",
                    (
                        "nested-containers mechanism did not "
                        "work end to end: "
                        f"{stage_event.get('message')}"
                    ),
                )
                self.assertIn(
                    "Reached nested Docker daemon",
                    stage_event["message"],
                )

                inspection = supervisor.inspect()

                env_lines = inspection["Config"]["Env"]
                docker_host_lines = [
                    line
                    for line in env_lines
                    if line.startswith("DOCKER_HOST=")
                ]
                self.assertEqual(
                    len(docker_host_lines),
                    1,
                    "candidate container was not given "
                    "exactly one DOCKER_HOST env var",
                )

                network_settings = inspection[
                    "NetworkSettings"
                ]["Networks"]
                self.assertIn(
                    network_name,
                    network_settings,
                    "candidate was not actually connected "
                    "to the DinD network",
                )

                supervisor.request_shutdown()

                exit_code = supervisor.wait_for_exit(
                    timeout_seconds=10,
                )
                self.assertEqual(exit_code, 0)

            finally:
                supervisor.force_terminate()
                supervisor.remove()

                if sidecar_container_id is not None:
                    self.backend.stop_container(
                        sidecar_container_id
                    )
                    self.backend.remove_container(
                        sidecar_container_id
                    )

                if network_name is not None:
                    if sidecar_container_id is not None:
                        self.backend.disconnect_network(
                            network_name,
                            sidecar_container_id,
                        )
                    self.backend.remove_network(
                        network_name
                    )

                    remaining = subprocess.run(
                        [
                            "docker", "network", "ls",
                            "--filter", f"name={network_name}",
                            "--format", "{{.Name}}",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    self.assertNotIn(
                        network_name,
                        remaining.stdout,
                        (
                            f"network {network_name} was "
                            "not actually removed — cleanup "
                            "silently failed"
                        ),
                    )


if __name__ == "__main__":
    unittest.main()
