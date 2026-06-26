from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from adapters.firmae.entrypoint import dispatch_candidate
from run_veritas import milestone_lines
from veritas_core.lifecycle_watchdog import LifecycleWatchdog
from veritas_core.run_coordinator import derive_candidate_stage_results


class RecordingEventWriter:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def emit(self, event_name: str, **fields: object) -> None:
        self.events.append({"event": event_name, **fields})


class StageOutcomeSupervisor:
    def __init__(self, root: Path) -> None:
        self.contract_root = root
        self.request = {
            "lifecycle": {
                "timeout_seconds": 5,
                "heartbeat_timeout_seconds": 2,
                "shutdown_grace_seconds": 1,
            }
        }
        self.events: list[dict[str, object]] = []
        self._pending: list[dict[str, object]] = [
            {
                "event": "adapter_started",
                "sequence": 1,
                "state": "starting",
            },
            {
                "event": "candidate_started",
                "sequence": 2,
                "state": "running",
            },
            {
                "event": "stage_completed",
                "sequence": 3,
                "state": "waiting_for_shutdown",
                "stage": "unpack",
                "stage_outcome": "failed",
                "message": "No root filesystem",
            },
        ]
        self._running = True
        self._exit_code: int | None = None

    @property
    def container_running(self) -> bool:
        return self._running

    def drain_events(self) -> list[dict[str, object]]:
        records = list(self._pending)
        self._pending.clear()
        self.events.extend(records)
        return records

    def request_shutdown(self, reason: str) -> None:
        next_sequence = len(self.events) + len(self._pending) + 1
        self._pending.extend(
            [
                {
                    "event": "shutdown_started",
                    "sequence": next_sequence,
                    "state": "shutting_down",
                },
                {
                    "event": "cleanup_complete",
                    "sequence": next_sequence + 1,
                    "state": "shutting_down",
                },
                {
                    "event": "adapter_stopped",
                    "sequence": next_sequence + 2,
                    "outcome": "completed",
                },
            ]
        )

    def wait_for_exit(self, timeout_seconds: float) -> int:
        self.drain_events()
        self._running = False
        self._exit_code = 0
        return 0

    def force_terminate(self) -> None:
        self._running = False
        self._exit_code = 137

    def exit_code_if_exited(self) -> int | None:
        return self._exit_code


class CandidateStageOutcomeTests(unittest.TestCase):
    def test_stage_durations_follow_candidate_stage_boundaries(self) -> None:
        events = [
            {
                "event": "candidate_started",
                "sequence": 1,
                "timestamp": "2026-06-26T10:00:00Z",
            },
            {
                "event": "stage_completed",
                "sequence": 2,
                "timestamp": "2026-06-26T10:00:10Z",
                "stage": "unpack",
                "stage_outcome": "succeeded",
                "message": "done",
            },
            {
                "event": "stage_completed",
                "sequence": 3,
                "timestamp": "2026-06-26T10:02:10Z",
                "stage": "emulate",
                "stage_outcome": "succeeded",
                "message": "ready",
            },
            {
                "event": "stage_completed",
                "sequence": 4,
                "timestamp": "2026-06-26T10:02:15Z",
                "stage": "endpoint-discovery",
                "stage_outcome": "succeeded",
                "message": "zero claims",
            },
        ]

        results = derive_candidate_stage_results(events)

        self.assertEqual(
            [record["elapsed_seconds"] for record in results],
            [10.0, 120.0, 5.0],
        )

    def test_terminal_milestone_is_printed_once(self) -> None:
        events = [
            {
                "event": "candidate_started",
                "sequence": 1,
                "timestamp": "2026-06-26T10:00:00Z",
            },
            {
                "event": "stage_completed",
                "sequence": 2,
                "timestamp": "2026-06-26T10:01:00Z",
                "stage": "unpack",
                "stage_outcome": "failed",
                "message": "No root filesystem",
            },
        ]
        printed: set[int] = set()

        first = milestone_lines(events, printed)
        second = milestone_lines(events, printed)

        self.assertEqual(len(first), 2)
        self.assertIn("Stage unpack failed", first[-1])
        self.assertIn("00:01:00", first[-1])
        self.assertEqual(second, [])

    def test_negative_stage_outcome_finishes_lifecycle_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            supervisor = StageOutcomeSupervisor(Path(temporary))
            watchdog = LifecycleWatchdog(
                supervisor,
                poll_interval_seconds=0.01,
            )
            watchdog.start()
            observed = watchdog.wait_for_matching_event(
                lambda event: (
                    event.get("event") == "stage_completed"
                    and event.get("stage") == "unpack"
                ),
                description="unpack-stage completion",
                timeout_seconds=1,
            )
            self.assertEqual(observed["stage_outcome"], "failed")

            watchdog.request_benchmark_shutdown()
            result = watchdog.wait(timeout_seconds=2)

            self.assertEqual(result.run_outcome, "completed")
            self.assertEqual(result.final_adapter_outcome, "completed")
            self.assertEqual(result.exit_code, 0)

    def test_firmae_unpack_failure_is_candidate_stage_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            firmware = root / "firmware.bin"
            workspace = root / "workspace"
            artifacts = root / "artifacts"
            firmware.write_bytes(b"firmware")
            artifacts.mkdir()
            (artifacts / "unpack-error.json").write_text(
                json.dumps(
                    {
                        "message": (
                            "FirmAE did not produce a root "
                            "filesystem archive"
                        )
                    }
                ),
                encoding="utf-8",
            )

            request = {
                "run": {
                    "requested_stages": ["unpack"],
                    "adapter_hints": {},
                },
                "firmware": {
                    "path": "/veritas/input/firmware",
                    "case_id": "case-001",
                    "sha256": "a" * 64,
                },
                "paths": {
                    "workspace": "/veritas/work",
                    "artifacts": "/veritas/artifacts",
                },
            }
            mapped = {
                "/veritas/input/firmware": firmware,
                "/veritas/work": workspace,
                "/veritas/artifacts": artifacts,
            }
            writer = RecordingEventWriter()

            with mock.patch(
                "adapters.firmae.entrypoint.map_contract_path",
                side_effect=lambda value: mapped[value],
            ), mock.patch(
                "adapters.firmae.entrypoint.subprocess.run",
                return_value=SimpleNamespace(returncode=1),
            ):
                runtime = dispatch_candidate(request, writer)

            self.assertIsNone(runtime)
            self.assertEqual(
                [event["event"] for event in writer.events],
                ["candidate_started", "stage_completed"],
            )
            stage_event = writer.events[-1]
            self.assertEqual(stage_event["stage"], "unpack")
            self.assertEqual(stage_event["stage_outcome"], "failed")
            self.assertNotEqual(stage_event["event"], "error")


if __name__ == "__main__":
    unittest.main()
