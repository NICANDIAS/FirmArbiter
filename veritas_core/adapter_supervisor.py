from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Sequence

from veritas_core.event_protocol import (
    AdapterEventStream,
    EventProtocolError,
)


class AdapterSupervisorError(RuntimeError):
    """Raised when adapter supervision fails."""


class AdapterProcessSupervisor:
    """
    Candidate-neutral supervisor for an Adapter Contract process.

    This first implementation uses a local process for contract testing.
    The later Docker backend will expose the same lifecycle operations.
    """

    def __init__(
        self,
        command: Sequence[str],
        request_path: Path,
        contract_root: Path,
        event_schema_path: Path,
    ) -> None:
        self.command = list(command)
        self.request_path = request_path.resolve()
        self.contract_root = contract_root.resolve()

        try:
            self.request = json.loads(
                self.request_path.read_text(encoding="utf-8")
            )
        except FileNotFoundError as exc:
            raise AdapterSupervisorError(
                f"Run request not found: {self.request_path}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise AdapterSupervisorError(
                f"Invalid run request JSON: {exc}"
            ) from exc

        self.run_id = self.request["run"]["run_id"]
        self.adapter_id = self.request["run"]["adapter_id"]

        self.events_path = (
            self.contract_root / "events" / "events.jsonl"
        )
        self.control_path = (
            self.contract_root / "control" / "shutdown.json"
        )

        self.events_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        self.control_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        artifacts_dir = self.contract_root / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        self.stdout_path = artifacts_dir / "adapter.stdout.log"
        self.stderr_path = artifacts_dir / "adapter.stderr.log"

        self.event_stream = AdapterEventStream(
            event_path=self.events_path,
            schema_path=event_schema_path.resolve(),
            expected_run_id=self.run_id,
            expected_adapter_id=self.adapter_id,
        )

        self.process: subprocess.Popen[bytes] | None = None
        self.events: list[dict[str, Any]] = []

        self._stdout_handle: Any = None
        self._stderr_handle: Any = None

    @property
    def process_alive(self) -> bool:
        return (
            self.process is not None
            and self.process.poll() is None
        )

    def start(self) -> None:
        if self.process is not None:
            raise AdapterSupervisorError(
                "Adapter process has already been started"
            )

        self._stdout_handle = self.stdout_path.open("wb")
        self._stderr_handle = self.stderr_path.open("wb")

        environment = os.environ.copy()

        # Test-only host mapping for fixed /veritas contract paths.
        # Real adapters will see /veritas directly inside their container.
        environment["VERITAS_CONTRACT_ROOT"] = str(
            self.contract_root
        )

        self.process = subprocess.Popen(
            [*self.command, str(self.request_path)],
            stdout=self._stdout_handle,
            stderr=self._stderr_handle,
            env=environment,
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

            if self.process is not None:
                return_code = self.process.poll()

                if return_code is not None:
                    self.drain_events()
                    raise AdapterSupervisorError(
                        f"Adapter exited with code {return_code} "
                        f"before emitting {event_name!r}"
                    )

            time.sleep(0.05)

        raise AdapterSupervisorError(
            f"Timed out waiting for event {event_name!r}"
        )

    def request_shutdown(
        self,
        reason: str = "benchmark_complete",
    ) -> None:
        if self.process is None:
            raise AdapterSupervisorError(
                "Adapter process has not been started"
            )

        shutdown_document = {
            "schema_version": "1.0",
            "command": "shutdown",
            "run_id": self.run_id,
            "reason": reason,
        }

        temporary_path = self.control_path.with_suffix(".tmp")

        temporary_path.write_text(
            json.dumps(
                shutdown_document,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

        temporary_path.replace(self.control_path)

    def wait_for_exit(
        self,
        timeout_seconds: float,
    ) -> int:
        if self.process is None:
            raise AdapterSupervisorError(
                "Adapter process has not been started"
            )

        try:
            return_code = self.process.wait(
                timeout=timeout_seconds
            )
        except subprocess.TimeoutExpired as exc:
            raise AdapterSupervisorError(
                "Adapter did not exit within the shutdown grace "
                "period"
            ) from exc
        finally:
            self._close_log_handles()

        return return_code

    def force_terminate(self) -> None:
        if self.process is None:
            self._close_log_handles()
            return

        if self.process.poll() is None:
            self.process.terminate()

            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)

        self._close_log_handles()

    def _close_log_handles(self) -> None:
        for handle in (
            self._stdout_handle,
            self._stderr_handle,
        ):
            if handle is not None and not handle.closed:
                handle.close()
