from __future__ import annotations

import hashlib
import json
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from typing import Any

from firmarbiter_core.adapter_registry import discover_adapters
from firmarbiter_core.docker_backend import DockerBackend
from firmarbiter_core.docker_supervisor import (
    DockerAdapterSupervisor,
    DockerSupervisorError,
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


def event(
    sequence: int,
    event_name: str,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "contract_version": "1.0",
        "event": event_name,
        "sequence": sequence,
        "timestamp": "2026-06-25T16:30:00Z",
        "run_id": "watchdog-test.run-1",
        "adapter_id": "mock-adapter",
        **extra,
    }


class FakeSupervisor:
    def __init__(
        self,
        root: Path,
        *,
        running: bool,
        mode: str,
        initial_events: list[dict[str, Any]],
        exit_code: int | None = None,
        timeout_seconds: float = 5.0,
        heartbeat_timeout_seconds: float = 0.2,
        shutdown_grace_seconds: float = 0.1,
    ) -> None:
        self.contract_root = root
        self.request = {
            "lifecycle": {
                "timeout_seconds": timeout_seconds,
                "heartbeat_timeout_seconds": (
                    heartbeat_timeout_seconds
                ),
                "shutdown_grace_seconds": (
                    shutdown_grace_seconds
                ),
            }
        }

        self.events: list[dict[str, Any]] = []
        self._pending = list(initial_events)
        self._running = running
        self._mode = mode
        self._exit_code = exit_code
        self.shutdown_reasons: list[str] = []
        self.force_called = False

    @property
    def container_running(self) -> bool:
        return self._running

    def drain_events(self) -> list[dict[str, Any]]:
        new_events = list(self._pending)
        self._pending.clear()
        self.events.extend(new_events)
        return new_events

    def request_shutdown(self, reason: str) -> None:
        self.shutdown_reasons.append(reason)

        if self._mode == "graceful":
            next_sequence = len(self.events) + len(
                self._pending
            ) + 1

            self._pending.extend(
                [
                    event(
                        next_sequence,
                        "shutdown_started",
                        state="shutting_down",
                    ),
                    event(
                        next_sequence + 1,
                        "cleanup_complete",
                        state="shutting_down",
                    ),
                    event(
                        next_sequence + 2,
                        "adapter_stopped",
                        outcome="completed",
                    ),
                ]
            )

            self._running = False
            self._exit_code = 0

    def wait_for_exit(
        self,
        timeout_seconds: float,
    ) -> int:
        self.drain_events()

        if self._running:
            time.sleep(min(timeout_seconds, 0.02))
            raise DockerSupervisorError(
                "Simulated shutdown timeout"
            )

        return int(
            self._exit_code
            if self._exit_code is not None
            else -1
        )

    def force_terminate(self) -> None:
        self.force_called = True
        self._running = False
        self._exit_code = 137

    def exit_code_if_exited(self) -> int | None:
        if self._running:
            return None
        return self._exit_code


def create_contract_root(
    root: Path,
    manifest_sha256: str,
    *,
    timeout_seconds: int,
) -> Path:
    firmware_content = b"lifecycle watchdog firmware\n"

    input_directory = root / "input"
    input_directory.mkdir(parents=True)

    firmware_path = input_directory / "firmware"
    firmware_path.write_bytes(firmware_content)

    unique_suffix = uuid.uuid4().hex[:12]

    request = {
        "schema_version": "1.0",
        "contract_version": "1.0",
        "run": {
            "experiment_id": "watchdog-test",
            "run_id": (
                "watchdog-test.case-001."
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
            "timeout_seconds": timeout_seconds,
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


class LifecycleWatchdogTests(unittest.TestCase):
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

    def test_normal_container_shutdown_is_completed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            contract_root = Path(temporary)

            request_path = create_contract_root(
                contract_root,
                self.adapter.manifest_sha256,
                timeout_seconds=30,
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

                self.assertTrue(
                    supervisor.container_running
                )

                watchdog.request_benchmark_shutdown()

                observation = watchdog.wait(
                    timeout_seconds=10,
                )

                self.assertEqual(
                    observation.run_outcome,
                    "completed",
                )

                self.assertEqual(
                    observation.trigger,
                    "benchmark_complete",
                )

                self.assertEqual(
                    observation.termination_mode,
                    "graceful_shutdown",
                )

                self.assertTrue(
                    observation.cleanup_complete_seen
                )

                self.assertTrue(
                    observation.adapter_stopped_seen
                )

                self.assertFalse(
                    observation.force_termination_used
                )

                self.assertEqual(
                    observation.exit_code,
                    0,
                )

                result_path = (
                    contract_root
                    / "artifacts"
                    / "lifecycle-observation.json"
                )

                self.assertTrue(result_path.is_file())

            finally:
                supervisor.force_terminate()
                supervisor.remove()

    def test_container_experiment_timeout_is_separate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            contract_root = Path(temporary)

            request_path = create_contract_root(
                contract_root,
                self.adapter.manifest_sha256,
                timeout_seconds=1,
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

                watchdog = LifecycleWatchdog(supervisor)
                watchdog.start()

                observation = watchdog.wait(
                    timeout_seconds=10,
                )

                self.assertEqual(
                    observation.run_outcome,
                    "experiment_timeout",
                )

                self.assertEqual(
                    observation.trigger,
                    "experiment_timeout",
                )

                # Timeout and cleanup outcome are separate.
                self.assertEqual(
                    observation.termination_mode,
                    "graceful_shutdown",
                )

                self.assertFalse(
                    observation.force_termination_used
                )

            finally:
                supervisor.force_terminate()
                supervisor.remove()

    def test_heartbeat_loss_is_not_boot_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            supervisor = FakeSupervisor(
                Path(temporary),
                running=True,
                mode="graceful",
                initial_events=[
                    event(
                        1,
                        "adapter_started",
                        state="starting",
                    ),
                    event(
                        2,
                        "candidate_started",
                        state="running",
                    ),
                ],
            )

            watchdog = LifecycleWatchdog(
                supervisor,
                poll_interval_seconds=0.01,
            )
            watchdog.start()

            observation = watchdog.wait(
                timeout_seconds=2,
            )

            self.assertEqual(
                observation.run_outcome,
                "heartbeat_lost",
            )

            self.assertEqual(
                observation.trigger,
                "heartbeat_lost",
            )

            self.assertEqual(
                observation.termination_mode,
                "graceful_shutdown",
            )

            self.assertNotEqual(
                observation.run_outcome,
                "boot_failed",
            )

    def test_unexpected_exit_is_recorded_separately(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            supervisor = FakeSupervisor(
                Path(temporary),
                running=False,
                mode="already-exited",
                initial_events=[
                    event(
                        1,
                        "adapter_started",
                        state="starting",
                    ),
                    event(
                        2,
                        "candidate_started",
                        state="running",
                    ),
                ],
                exit_code=7,
            )

            watchdog = LifecycleWatchdog(
                supervisor,
                poll_interval_seconds=0.01,
            )
            watchdog.start()

            observation = watchdog.wait(
                timeout_seconds=1,
            )

            self.assertEqual(
                observation.run_outcome,
                "unexpected_early_exit",
            )

            self.assertEqual(
                observation.termination_mode,
                "already_exited",
            )

            self.assertFalse(
                observation.shutdown_requested
            )

            self.assertEqual(
                observation.exit_code,
                7,
            )

    def test_failed_shutdown_uses_forced_termination(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            supervisor = FakeSupervisor(
                Path(temporary),
                running=True,
                mode="stubborn",
                initial_events=[
                    event(
                        1,
                        "adapter_started",
                        state="starting",
                    ),
                    event(
                        2,
                        "candidate_started",
                        state="running",
                    ),
                    event(
                        3,
                        "heartbeat",
                        state="running",
                    ),
                ],
                heartbeat_timeout_seconds=5,
            )

            watchdog = LifecycleWatchdog(
                supervisor,
                poll_interval_seconds=0.01,
            )
            watchdog.start()

            watchdog.wait_for_event(
                "heartbeat",
                timeout_seconds=1,
            )

            watchdog.request_benchmark_shutdown()

            observation = watchdog.wait(
                timeout_seconds=2,
            )

            self.assertEqual(
                observation.run_outcome,
                "shutdown_failed",
            )

            self.assertEqual(
                observation.termination_mode,
                "forced_termination",
            )

            self.assertTrue(
                observation.force_termination_used
            )

            self.assertTrue(supervisor.force_called)

            self.assertEqual(
                observation.exit_code,
                137,
            )


if __name__ == "__main__":
    unittest.main()
