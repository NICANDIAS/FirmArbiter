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
from veritas_core.run_coordinator import (
    candidate_stage_blocks_following_work,
    derive_candidate_stage_results,
)


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

    def test_inconclusive_emulation_does_not_block_endpoint_discovery(self) -> None:
        event = {
            "event": "stage_completed",
            "stage": "emulate",
            "stage_outcome": "inconclusive",
        }

        self.assertFalse(
            candidate_stage_blocks_following_work(
                event,
                {"emulate"},
            )
        )

        failed_event = {
            **event,
            "stage_outcome": "failed",
        }
        self.assertTrue(
            candidate_stage_blocks_following_work(
                failed_event,
                {"emulate"},
            )
        )

        unpack_event = {
            **event,
            "stage": "unpack",
        }
        self.assertTrue(
            candidate_stage_blocks_following_work(
                unpack_event,
                {"unpack"},
            )
        )

    def test_inconclusive_emulation_starts_endpoint_timing(self) -> None:
        events = [
            {
                "event": "candidate_started",
                "sequence": 1,
                "timestamp": "2026-06-27T10:00:00Z",
            },
            {
                "event": "stage_completed",
                "sequence": 2,
                "timestamp": "2026-06-27T10:00:10Z",
                "stage": "unpack",
                "stage_outcome": "succeeded",
                "message": "unpacked",
            },
            {
                "event": "stage_completed",
                "sequence": 3,
                "timestamp": "2026-06-27T10:02:10Z",
                "stage": "emulate",
                "stage_outcome": "inconclusive",
                "message": "no network readiness",
            },
            {
                "event": "stage_completed",
                "sequence": 4,
                "timestamp": "2026-06-27T10:02:25Z",
                "stage": "endpoint-discovery",
                "stage_outcome": "succeeded",
                "message": "zero claims",
            },
        ]

        results = derive_candidate_stage_results(events)

        self.assertEqual(
            [record["elapsed_seconds"] for record in results],
            [10.0, 120.0, 15.0],
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

    def test_firmae_network_readiness_timeout_completes_normally(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            firmware = root / "firmware.bin"
            workspace = root / "workspace"
            artifacts = root / "artifacts"
            scratch = root / "scratch"
            runtime_evidence = artifacts / "firmae-runtime-evidence"
            candidate_scratch = scratch / "1"

            firmware.write_bytes(b"firmware")
            artifacts.mkdir()
            runtime_evidence.mkdir()
            candidate_scratch.mkdir(parents=True)

            (artifacts / "rootfs.tar.gz").write_bytes(b"archive")
            (artifacts / "rootfs-extractor.log").write_text(
                "rootfs log",
                encoding="utf-8",
            )
            (artifacts / "kernel-extractor.log").write_text(
                "kernel log",
                encoding="utf-8",
            )
            (artifacts / "unpack-metadata.json").write_text(
                json.dumps({"firmae_image_id": "1"}),
                encoding="utf-8",
            )
            (candidate_scratch / "run.sh").write_text(
                "#!/bin/sh\n",
                encoding="utf-8",
            )
            (candidate_scratch / "qemu.initial.serial.log").write_text(
                "Linux version\nBusyBox init\n",
                encoding="utf-8",
            )

            request = {
                "run": {
                    "requested_stages": [
                        "unpack",
                        "emulate",
                        "endpoint-discovery",
                    ],
                    "adapter_hints": {},
                },
                "firmware": {
                    "path": "/veritas/input/firmware",
                    "case_id": "case-001",
                    "sha256": "a" * 64,
                },
                "lifecycle": {
                    "timeout_seconds": 120,
                    "boot_wait_timeout_seconds": 12,
                    "endpoint_wait_timeout_seconds": 5,
                    "shutdown_grace_seconds": 1,
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
            runtime = SimpleNamespace(pid=1234)

            with mock.patch(
                "adapters.firmae.entrypoint.map_contract_path",
                side_effect=lambda value: mapped[value],
            ), mock.patch(
                "adapters.firmae.entrypoint.subprocess.run",
                return_value=SimpleNamespace(returncode=0),
            ), mock.patch(
                "adapters.firmae.entrypoint.export_rootfs_tree",
            ), mock.patch(
                "adapters.firmae.entrypoint.prepare_runtime_layout",
                return_value=(scratch, runtime_evidence),
            ), mock.patch(
                "adapters.firmae.entrypoint.detect_firmae_architecture",
                return_value="armel",
            ), mock.patch(
                "adapters.firmae.entrypoint.build_runtime_environment",
                return_value={},
            ), mock.patch(
                "adapters.firmae.entrypoint.run_checked_helper",
            ), mock.patch(
                "adapters.firmae.entrypoint.load_inferred_addresses",
                return_value=["192.168.0.1"],
            ), mock.patch(
                "adapters.firmae.entrypoint.RuntimeController.start",
                return_value=runtime,
            ), mock.patch(
                "adapters.firmae.entrypoint.wait_for_candidate_network_readiness",
                return_value=([], False),
            ), mock.patch(
                "adapters.firmae.entrypoint.discover_candidate_endpoints",
                return_value=([], None),
            ):
                returned_runtime = dispatch_candidate(
                    request,
                    writer,
                )

            self.assertIs(returned_runtime, runtime)
            event_names = [
                event["event"]
                for event in writer.events
            ]
            self.assertNotIn(
                "candidate_boot_reported",
                event_names,
            )
            self.assertNotIn("error", event_names)

            stage_events = [
                event
                for event in writer.events
                if event["event"] == "stage_completed"
            ]
            self.assertEqual(
                [event["stage"] for event in stage_events],
                [
                    "unpack",
                    "emulate",
                    "endpoint-discovery",
                ],
            )
            self.assertEqual(
                [event["stage_outcome"] for event in stage_events],
                ["succeeded", "inconclusive", "succeeded"],
            )
            self.assertIn(
                "no ICMP or supported TCP readiness signal",
                stage_events[1]["message"],
            )
            self.assertIn(
                "0 candidate endpoint claims",
                stage_events[2]["message"],
            )

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
