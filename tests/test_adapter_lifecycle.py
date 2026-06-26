from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

from veritas_core.adapter_supervisor import (
    AdapterProcessSupervisor,
)
from veritas_core.event_protocol import (
    AdapterEventStream,
    EventProtocolError,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]

EVENT_SCHEMA = (
    PROJECT_ROOT / "schemas" / "adapter-event-v1.schema.json"
)

MOCK_ADAPTER = (
    PROJECT_ROOT / "tests" / "fixtures"
    / "mock_contract_adapter.py"
)


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def create_contract_root(root: Path) -> Path:
    firmware_content = b"neutral mock firmware input\n"

    input_directory = root / "input"
    input_directory.mkdir(parents=True)

    firmware_path = input_directory / "firmware"
    firmware_path.write_bytes(firmware_content)

    request = {
        "schema_version": "1.0",
        "contract_version": "1.0",
        "run": {
            "experiment_id": "contract-test",
            "run_id": "contract-test.case-001.mock-adapter.attempt-1",
            "adapter_id": "mock-adapter",
            "attempt": 1,
            "created_at": "2026-06-25T16:30:00Z",
            "requested_stages": [
                "unpack",
                "emulate",
                "endpoint-discovery"
            ]
        },
        "firmware": {
            "case_id": "case-001",
            "path": "/veritas/input/firmware",
            "sha256": sha256_bytes(firmware_content),
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
            "network": "none",
            "requirements": []
        },
        "paths": {
            "workspace": "/veritas/work",
            "artifacts": "/veritas/artifacts",
            "events": "/veritas/events/events.jsonl",
            "control": "/veritas/control"
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


class AdapterLifecycleTests(unittest.TestCase):
    def test_adapter_remains_alive_until_shutdown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            contract_root = Path(temporary_directory)
            request_path = create_contract_root(
                contract_root
            )

            supervisor = AdapterProcessSupervisor(
                command=[
                    sys.executable,
                    str(MOCK_ADAPTER),
                ],
                request_path=request_path,
                contract_root=contract_root,
                event_schema_path=EVENT_SCHEMA,
            )

            try:
                supervisor.start()

                endpoint_event = supervisor.wait_for_event(
                    "endpoint_reported",
                    timeout_seconds=5,
                )

                self.assertEqual(
                    endpoint_event["endpoint"]["port"],
                    8080,
                )

                # Critical contract requirement:
                # endpoint reporting must not end emulation.
                self.assertTrue(supervisor.process_alive)

                supervisor.wait_for_event(
                    "heartbeat",
                    timeout_seconds=3,
                )

                self.assertTrue(supervisor.process_alive)

                supervisor.request_shutdown()

                return_code = supervisor.wait_for_exit(
                    timeout_seconds=5,
                )

                self.assertEqual(return_code, 0)

                supervisor.drain_events()

                event_names = [
                    event["event"]
                    for event in supervisor.events
                ]

                self.assertEqual(
                    event_names[:6],
                    [
                        "adapter_started",
                        "candidate_started",
                        "candidate_boot_reported",
                        "stage_completed",
                        "endpoint_reported",
                        "stage_completed",
                    ],
                )

                stage_events = [
                    event
                    for event in supervisor.events
                    if event["event"] == "stage_completed"
                ]
                self.assertEqual(
                    [event["stage"] for event in stage_events],
                    ["emulate", "endpoint-discovery"],
                )
                self.assertTrue(
                    all(
                        event["stage_outcome"] == "succeeded"
                        for event in stage_events
                    )
                )

                heartbeat_events = [
                    event_name
                    for event_name in event_names
                    if event_name == "heartbeat"
                ]

                self.assertGreaterEqual(
                    len(heartbeat_events),
                    1,
                    "Adapter must emit at least one heartbeat before shutdown",
                )

                self.assertEqual(
                    event_names[-3:],
                    [
                        "shutdown_started",
                        "cleanup_complete",
                        "adapter_stopped",
                    ],
                )

                shutdown_index = event_names.index("shutdown_started")

                self.assertTrue(
                    all(
                        event_name == "heartbeat"
                        for event_name in event_names[6:shutdown_index]
                    ),
                    "Only heartbeat events are permitted while awaiting shutdown",
                )

                sequences = [
                    event["sequence"]
                    for event in supervisor.events
                ]

                self.assertEqual(
                    sequences,
                    list(
                        range(
                            1,
                            len(sequences) + 1,
                        )
                    ),
                )

                # Candidate endpoint claims are not measurements.
                self.assertNotIn(
                    "reachable",
                    endpoint_event,
                )
                self.assertNotIn(
                    "verified",
                    endpoint_event,
                )

            finally:
                supervisor.force_terminate()

    def test_stage_completed_event_is_validated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            event_path = root / "events.jsonl"
            events = [
                {
                    "schema_version": "1.0",
                    "contract_version": "1.0",
                    "event": "adapter_started",
                    "sequence": 1,
                    "timestamp": "2026-06-25T16:30:00Z",
                    "run_id": "contract-test.run-1",
                    "adapter_id": "mock-adapter",
                    "state": "starting",
                },
                {
                    "schema_version": "1.0",
                    "contract_version": "1.0",
                    "event": "stage_completed",
                    "sequence": 2,
                    "timestamp": "2026-06-25T16:30:01Z",
                    "run_id": "contract-test.run-1",
                    "adapter_id": "mock-adapter",
                    "state": "waiting_for_shutdown",
                    "stage": "unpack",
                    "stage_outcome": "failed",
                    "message": "Candidate produced no root filesystem",
                },
            ]
            event_path.write_text(
                "".join(json.dumps(item) + "\n" for item in events),
                encoding="utf-8",
            )
            stream = AdapterEventStream(
                event_path=event_path,
                schema_path=EVENT_SCHEMA,
                expected_run_id="contract-test.run-1",
                expected_adapter_id="mock-adapter",
            )
            records = stream.read_new()
            self.assertEqual(records[-1]["stage"], "unpack")
            self.assertEqual(
                records[-1]["stage_outcome"],
                "failed",
            )

    def test_success_field_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            event_path = root / "events.jsonl"

            invalid_event = {
                "schema_version": "1.0",
                "contract_version": "1.0",
                "event": "adapter_started",
                "sequence": 1,
                "timestamp": "2026-06-25T16:30:00Z",
                "run_id": "contract-test.run-1",
                "adapter_id": "mock-adapter",
                "success": True
            }

            event_path.write_text(
                json.dumps(invalid_event) + "\n",
                encoding="utf-8",
            )

            stream = AdapterEventStream(
                event_path=event_path,
                schema_path=EVENT_SCHEMA,
                expected_run_id="contract-test.run-1",
                expected_adapter_id="mock-adapter",
            )

            with self.assertRaises(
                EventProtocolError
            ) as context:
                stream.read_new()

            self.assertIn(
                "Additional properties are not allowed",
                str(context.exception),
            )


if __name__ == "__main__":
    unittest.main()
