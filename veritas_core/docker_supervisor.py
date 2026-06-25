from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from veritas_core.adapter_registry import AdapterRecord
from veritas_core.docker_backend import (
    BuiltAdapterImage,
    CreatedContainer,
    DockerBackend,
    DockerBackendError,
)
from veritas_core.event_protocol import (
    AdapterEventStream,
    EventProtocolError,
)


class DockerSupervisorError(RuntimeError):
    """Raised when container lifecycle supervision fails."""


class DockerAdapterSupervisor:
    def __init__(
        self,
        backend: DockerBackend,
        adapter: AdapterRecord,
        image: BuiltAdapterImage,
        request_path: Path,
        contract_root: Path,
        event_schema_path: Path,
    ) -> None:
        self.backend = backend
        self.adapter = adapter
        self.image = image
        self.request_path = request_path.resolve()
        self.contract_root = contract_root.resolve()

        self.request = json.loads(
            self.request_path.read_text(encoding="utf-8")
        )

        self.events_path = (
            self.contract_root / "events" / "events.jsonl"
        )

        self.shutdown_path = (
            self.contract_root
            / "control"
            / "shutdown.json"
        )

        self.event_stream = AdapterEventStream(
            event_path=self.events_path,
            schema_path=event_schema_path.resolve(),
            expected_run_id=self.request["run"]["run_id"],
            expected_adapter_id=self.adapter.adapter_id,
        )

        self.container: CreatedContainer | None = None
        self.events: list[dict[str, Any]] = []

    @property
    def container_id(self) -> str:
        if self.container is None:
            raise DockerSupervisorError(
                "Container has not been created"
            )
        return self.container.container_id

    @property
    def container_running(self) -> bool:
        if self.container is None:
            return False

        try:
            return self.backend.container_running(
                self.container.container_id
            )
        except DockerBackendError:
            return False

    def exit_code_if_exited(self) -> int | None:
        """
        Return the container exit code only after it has stopped.
        """
        if self.container is None:
            return None

        if self.container_running:
            return None

        return self.backend.exit_code(
            self.container.container_id
        )

    def start(self) -> None:
        if self.container is not None:
            raise DockerSupervisorError(
                "Container has already been created"
            )

        self.container = self.backend.create_container(
            adapter=self.adapter,
            image=self.image,
            request_path=self.request_path,
            contract_root=self.contract_root,
        )

        self.backend.start_container(
            self.container.container_id
        )

    def drain_events(self) -> list[dict[str, Any]]:
        try:
            new_events = self.event_stream.read_new()
        except EventProtocolError:
            self.force_terminate()
            raise

        self.events.extend(new_events)
        return new_events

    def wait_for_event(
        self,
        event_name: str,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds

        while time.monotonic() < deadline:
            for event in self.drain_events():
                if event["event"] == event_name:
                    return event

            if (
                self.container is not None
                and not self.container_running
            ):
                self.drain_events()
                raise DockerSupervisorError(
                    f"Container exited with code "
                    f"{self.backend.exit_code(self.container_id)} "
                    f"before emitting {event_name!r}"
                )

            time.sleep(0.05)

        raise DockerSupervisorError(
            f"Timed out waiting for event {event_name!r}"
        )

    def request_shutdown(
        self,
        reason: str = "benchmark_complete",
    ) -> None:
        if self.container is None:
            raise DockerSupervisorError(
                "Container has not been started"
            )

        document = {
            "schema_version": "1.0",
            "command": "shutdown",
            "run_id": self.request["run"]["run_id"],
            "reason": reason,
        }

        self.shutdown_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        temporary_path = self.shutdown_path.with_suffix(
            ".tmp"
        )

        temporary_path.write_text(
            json.dumps(document, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        temporary_path.replace(self.shutdown_path)

    def wait_for_exit(
        self,
        timeout_seconds: float,
    ) -> int:
        deadline = time.monotonic() + timeout_seconds

        while time.monotonic() < deadline:
            self.drain_events()

            if not self.container_running:
                self.drain_events()
                self._save_logs()
                return self.backend.exit_code(
                    self.container_id
                )

            time.sleep(0.05)

        raise DockerSupervisorError(
            "Container did not exit during the shutdown "
            "grace period"
        )

    def inspect(self) -> dict[str, Any]:
        return self.backend.inspect_container(
            self.container_id
        )

    def force_terminate(self) -> None:
        if self.container is None:
            return

        if self.container_running:
            self.backend.stop_container(
                self.container_id,
                timeout_seconds=2,
            )

        if self.container_running:
            self.backend.kill_container(
                self.container_id
            )

        self._save_logs()

    def _save_logs(self) -> None:
        if self.container is None:
            return

        stdout, stderr = self.backend.container_logs(
            self.container_id
        )

        artifact_directory = (
            self.contract_root / "artifacts"
        )
        artifact_directory.mkdir(
            parents=True,
            exist_ok=True,
        )

        (
            artifact_directory / "adapter.stdout.log"
        ).write_text(
            stdout,
            encoding="utf-8",
        )

        (
            artifact_directory / "adapter.stderr.log"
        ).write_text(
            stderr,
            encoding="utf-8",
        )

    def remove(self) -> None:
        if self.container is None:
            return

        self.backend.remove_container(
            self.container.container_id
        )
