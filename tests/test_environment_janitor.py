from __future__ import annotations

import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any

from veritas_core.adapter_registry import discover_adapters
from veritas_core.docker_backend import (
    DockerBackend,
    DockerBackendError,
)
from veritas_core.environment_janitor import (
    EnvironmentJanitor,
    build_remediation_plan,
)
from veritas_core.environment_residue import (
    ContainerRecord,
    HostEnvironmentSnapshot,
    LoopDeviceRecord,
    ProcessRecord,
    evaluate_environment_residue,
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

ADAPTER_ROOT = (
    PROJECT_ROOT
    / "tests"
    / "fixtures"
    / "docker-probe-adapters"
)


def snapshot(
    *,
    qemu_processes: tuple[ProcessRecord, ...] = (),
    loop_devices: tuple[LoopDeviceRecord, ...] = (),
    containers: tuple[ContainerRecord, ...] = (),
) -> HostEnvironmentSnapshot:
    return HostEnvironmentSnapshot(
        observed_at="2026-06-25T16:30:00Z",
        qemu_processes=qemu_processes,
        tun_tap_interfaces=(),
        loop_devices=loop_devices,
        device_mapper_entries=(),
        containers=containers,
        source_errors={},
    )


class FakeBackend:
    def __init__(
        self,
        inspections: dict[str, dict[str, Any]],
    ) -> None:
        self.inspections = inspections
        self.removed: list[str] = []

    def inspect_container(
        self,
        container_id: str,
    ) -> dict[str, Any]:
        if container_id not in self.inspections:
            raise RuntimeError("container not found")

        return self.inspections[container_id]

    def remove_container(
        self,
        container_id: str,
    ) -> None:
        self.removed.append(container_id)
        self.inspections.pop(container_id, None)


class EnvironmentJanitorUnitTests(unittest.TestCase):
    def test_only_current_run_container_is_authorised(
        self,
    ) -> None:
        run_id = "experiment.case.adapter.attempt-1"

        owned = ContainerRecord(
            container_id="owned-container-id",
            name="owned",
            image="example:test",
            state="exited",
            labels={
                "veritas.managed": "true",
                "veritas.run_id": run_id,
            },
        )

        unrelated = ContainerRecord(
            container_id="unrelated-container-id",
            name="unrelated",
            image="example:test",
            state="running",
            labels={
                "veritas.managed": "true",
                "veritas.run_id": "another-run",
            },
        )

        before = snapshot()
        after = snapshot(
            containers=(owned, unrelated),
        )

        residue = evaluate_environment_residue(
            before,
            after,
        )

        plan = build_remediation_plan(
            run_id=run_id,
            residue=residue,
        )

        actions = {
            action.resource_identity: action
            for action in plan.actions
        }

        self.assertTrue(
            actions["owned-container-id"].authorised
        )
        self.assertEqual(
            actions["owned-container-id"].action,
            "remove",
        )

        self.assertFalse(
            actions["unrelated-container-id"].authorised
        )
        self.assertEqual(
            actions["unrelated-container-id"].action,
            "preserve",
        )

    def test_host_resources_are_preserved(
        self,
    ) -> None:
        before = snapshot()

        after = snapshot(
            qemu_processes=(
                ProcessRecord(
                    pid=9000,
                    start_time_ticks=1234,
                    executable="/usr/bin/qemu-system-arm",
                    command=("qemu-system-arm",),
                ),
            ),
            loop_devices=(
                LoopDeviceRecord(
                    name="/dev/loop9",
                    backing_file="/tmp/image",
                    offset_bytes=0,
                    autoclear=False,
                ),
            ),
        )

        residue = evaluate_environment_residue(
            before,
            after,
        )

        plan = build_remediation_plan(
            run_id="run-1",
            residue=residue,
        )

        self.assertTrue(
            all(
                action.action == "preserve"
                for action in plan.actions
            )
        )

        self.assertTrue(
            all(
                not action.authorised
                for action in plan.actions
            )
        )

    def test_labels_are_revalidated_before_removal(
        self,
    ) -> None:
        run_id = "run-1"
        container_id = "container-id"

        container = ContainerRecord(
            container_id=container_id,
            name="test",
            image="example:test",
            state="exited",
            labels={
                "veritas.managed": "true",
                "veritas.run_id": run_id,
            },
        )

        residue = evaluate_environment_residue(
            snapshot(),
            snapshot(containers=(container,)),
        )

        plan = build_remediation_plan(
            run_id=run_id,
            residue=residue,
        )

        # Current labels no longer prove ownership.
        backend = FakeBackend(
            {
                container_id: {
                    "Id": container_id,
                    "Config": {
                        "Labels": {
                            "veritas.managed": "true",
                            "veritas.run_id": "different-run",
                        }
                    },
                }
            }
        )

        with tempfile.TemporaryDirectory() as temporary:
            janitor = EnvironmentJanitor(
                backend=backend,
                contract_root=Path(temporary),
            )

            result = janitor.execute(plan)

        self.assertEqual(
            result.removed_resources,
            0,
        )
        self.assertEqual(
            result.preserved_resources,
            1,
        )
        self.assertEqual(backend.removed, [])


class EnvironmentJanitorDockerTests(unittest.TestCase):
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

    def test_owned_container_is_removed_but_unowned_is_kept(
        self,
    ) -> None:
        run_id = (
            "janitor-test."
            + uuid.uuid4().hex[:12]
        )

        owned_name = (
            "veritas-owned-"
            + uuid.uuid4().hex[:10]
        )

        unowned_name = (
            "veritas-unowned-"
            + uuid.uuid4().hex[:10]
        )

        owned_id: str | None = None
        unowned_id: str | None = None

        try:
            owned = subprocess.run(
                [
                    "docker",
                    "container",
                    "create",
                    "--name",
                    owned_name,
                    "--label",
                    "veritas.managed=true",
                    "--label",
                    f"veritas.run_id={run_id}",
                    self.image.reference,
                    "/veritas/input/request.json",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )

            owned_id = owned.stdout.strip()

            unowned = subprocess.run(
                [
                    "docker",
                    "container",
                    "create",
                    "--name",
                    unowned_name,
                    self.image.reference,
                    "/veritas/input/request.json",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )

            unowned_id = unowned.stdout.strip()

            owned_inspection = (
                self.backend.inspect_container(
                    owned_id
                )
            )

            unowned_inspection = (
                self.backend.inspect_container(
                    unowned_id
                )
            )

            owned_record = ContainerRecord(
                container_id=owned_id,
                name=owned_name,
                image=self.image.reference,
                state="created",
                labels={
                    str(key): str(value)
                    for key, value in (
                        owned_inspection["Config"][
                            "Labels"
                        ]
                        or {}
                    ).items()
                },
            )

            unowned_record = ContainerRecord(
                container_id=unowned_id,
                name=unowned_name,
                image=self.image.reference,
                state="created",
                labels={
                    str(key): str(value)
                    for key, value in (
                        unowned_inspection["Config"][
                            "Labels"
                        ]
                        or {}
                    ).items()
                },
            )

            residue = evaluate_environment_residue(
                snapshot(),
                snapshot(
                    containers=(
                        owned_record,
                        unowned_record,
                    )
                ),
            )

            plan = build_remediation_plan(
                run_id=run_id,
                residue=residue,
            )

            with tempfile.TemporaryDirectory() as temporary:
                janitor = EnvironmentJanitor(
                    backend=self.backend,
                    contract_root=Path(temporary),
                )

                result = janitor.execute(plan)

                artifact_directory = (
                    Path(temporary) / "artifacts"
                )

                self.assertTrue(
                    (
                        artifact_directory
                        / "remediation-plan.json"
                    ).is_file()
                )

                self.assertTrue(
                    (
                        artifact_directory
                        / "remediation-observation.json"
                    ).is_file()
                )

            self.assertEqual(
                result.removed_resources,
                1,
            )

            self.assertEqual(
                result.preserved_resources,
                1,
            )

            with self.assertRaises(
                DockerBackendError
            ):
                self.backend.inspect_container(
                    owned_id
                )

            # The unowned container must remain untouched.
            remaining = (
                self.backend.inspect_container(
                    unowned_id
                )
            )

            self.assertEqual(
                remaining["Id"],
                unowned_id,
            )

        finally:
            if owned_id is not None:
                self.backend.remove_container(
                    owned_id
                )

            if unowned_id is not None:
                self.backend.remove_container(
                    unowned_id
                )


if __name__ == "__main__":
    unittest.main()
