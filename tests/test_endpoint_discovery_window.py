from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jsonschema import Draft202012Validator

from adapters.firmae.entrypoint import (
    discover_candidate_endpoints,
)
from run_veritas import summarise_result


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REQUEST_SCHEMA = (
    PROJECT_ROOT / "schemas" / "run-request-v1.schema.json"
)


class FakeClock:
    def __init__(self) -> None:
        self.current = 0.0

    def monotonic(self) -> float:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.current += seconds


class RuntimeStub:
    def __init__(
        self,
        poll_results: list[int | None] | None = None,
    ) -> None:
        self.poll_results = list(poll_results or [])
        self.poll_count = 0
        self.unexpected_exit_recorded = False
        self.last_exit_code = next(
            (
                value
                for value in reversed(self.poll_results)
                if value is not None
            ),
            0,
        )

    def poll(self) -> int | None:
        self.poll_count += 1

        if self.poll_results:
            return self.poll_results.pop(0)

        return None

    def record_unexpected_exit(self) -> int:
        self.unexpected_exit_recorded = True
        return self.last_exit_code


class EndpointDiscoveryWindowTests(unittest.TestCase):
    def test_retries_until_endpoint_is_found(self) -> None:
        endpoint = {
            "host": "192.168.0.1",
            "port": 80,
            "protocol": "http",
        }
        clock = FakeClock()
        runtime = RuntimeStub()

        with tempfile.TemporaryDirectory() as temporary:
            artifacts = Path(temporary)

            with mock.patch(
                "adapters.firmae.entrypoint.time.monotonic",
                side_effect=clock.monotonic,
            ), mock.patch(
                "adapters.firmae.entrypoint.time.sleep",
                side_effect=clock.sleep,
            ), mock.patch(
                "adapters.firmae.entrypoint._scan_candidate_endpoints",
                side_effect=[[], [], [endpoint]],
            ) as scan:
                endpoints, runtime_exit_code = (
                    discover_candidate_endpoints(
                        runtime=runtime,
                        addresses=["192.168.0.1"],
                        artifacts_path=artifacts,
                        timeout_seconds=30.0,
                        scan_interval_seconds=5.0,
                        scan_timeout_seconds=1.0,
                    )
                )

            self.assertIsNone(runtime_exit_code)
            self.assertEqual(endpoints, [endpoint])
            self.assertEqual(scan.call_count, 3)

            evidence = json.loads(
                (
                    artifacts
                    / "candidate-endpoint-discovery.json"
                ).read_text(encoding="utf-8")
            )
            claims = json.loads(
                (
                    artifacts
                    / "candidate-endpoint-claims.json"
                ).read_text(encoding="utf-8")
            )

            self.assertEqual(
                evidence["outcome"],
                "endpoint_found",
            )
            self.assertEqual(evidence["attempt_count"], 3)
            self.assertEqual(
                evidence["elapsed_seconds"],
                10.0,
            )
            self.assertEqual(claims["endpoints"], [endpoint])

    def test_zero_claims_are_recorded_after_timeout(self) -> None:
        clock = FakeClock()
        runtime = RuntimeStub()

        with tempfile.TemporaryDirectory() as temporary:
            artifacts = Path(temporary)

            with mock.patch(
                "adapters.firmae.entrypoint.time.monotonic",
                side_effect=clock.monotonic,
            ), mock.patch(
                "adapters.firmae.entrypoint.time.sleep",
                side_effect=clock.sleep,
            ), mock.patch(
                "adapters.firmae.entrypoint._scan_candidate_endpoints",
                return_value=[],
            ) as scan:
                endpoints, runtime_exit_code = (
                    discover_candidate_endpoints(
                        runtime=runtime,
                        addresses=["192.168.0.1"],
                        artifacts_path=artifacts,
                        timeout_seconds=12.0,
                        scan_interval_seconds=5.0,
                        scan_timeout_seconds=1.0,
                    )
                )

            self.assertIsNone(runtime_exit_code)
            self.assertEqual(endpoints, [])
            self.assertEqual(scan.call_count, 3)

            evidence = json.loads(
                (
                    artifacts
                    / "candidate-endpoint-discovery.json"
                ).read_text(encoding="utf-8")
            )

            self.assertEqual(evidence["outcome"], "timeout")
            self.assertEqual(evidence["attempt_count"], 3)
            self.assertEqual(
                evidence["elapsed_seconds"],
                12.0,
            )

    def test_runtime_exit_during_discovery_is_recorded(self) -> None:
        clock = FakeClock()
        runtime = RuntimeStub([None, 17])

        with tempfile.TemporaryDirectory() as temporary:
            artifacts = Path(temporary)

            with mock.patch(
                "adapters.firmae.entrypoint.time.monotonic",
                side_effect=clock.monotonic,
            ), mock.patch(
                "adapters.firmae.entrypoint.time.sleep",
                side_effect=clock.sleep,
            ), mock.patch(
                "adapters.firmae.entrypoint._scan_candidate_endpoints",
                return_value=[],
            ):
                endpoints, runtime_exit_code = (
                    discover_candidate_endpoints(
                        runtime=runtime,
                        addresses=["192.168.0.1"],
                        artifacts_path=artifacts,
                        timeout_seconds=30.0,
                        scan_interval_seconds=5.0,
                        scan_timeout_seconds=1.0,
                    )
                )

            self.assertEqual(endpoints, [])
            self.assertEqual(runtime_exit_code, 17)
            self.assertTrue(runtime.unexpected_exit_recorded)

            evidence = json.loads(
                (
                    artifacts
                    / "candidate-endpoint-discovery.json"
                ).read_text(encoding="utf-8")
            )

            self.assertEqual(
                evidence["outcome"],
                "runtime_exited",
            )
            self.assertEqual(
                evidence["runtime_exit_code"],
                17,
            )
            self.assertEqual(evidence["attempt_count"], 1)

    def test_request_schema_accepts_endpoint_wait_timeout(self) -> None:
        schema = json.loads(
            REQUEST_SCHEMA.read_text(encoding="utf-8")
        )
        validator = Draft202012Validator(schema)
        request = {
            "schema_version": "1.0",
            "contract_version": "1.0",
            "run": {
                "experiment_id": "endpoint-window-test",
                "run_id": (
                    "endpoint-window-test.case-001."
                    "firmae.attempt-1"
                ),
                "adapter_id": "firmae",
                "attempt": 1,
                "created_at": "2026-06-27T12:00:00Z",
                "requested_stages": [
                    "unpack",
                    "emulate",
                    "endpoint-discovery",
                ],
            },
            "firmware": {
                "case_id": "case-001",
                "path": "/veritas/input/firmware",
                "sha256": "a" * 64,
                "size_bytes": 1,
                "delivery_semantics": "opaque-original-bytes",
                "read_only": True,
            },
            "lifecycle": {
                "timeout_seconds": 120,
                "heartbeat_interval_seconds": 5,
                "heartbeat_timeout_seconds": 15,
                "shutdown_grace_seconds": 10,
                "boot_wait_timeout_seconds": 60.0,
                "endpoint_wait_timeout_seconds": 30.0,
            },
            "resources": {
                "cpu_cores": 1.0,
                "memory_bytes": 268435456,
                "pids_limit": 128,
            },
            "runtime_grants": {
                "run_as_root": True,
                "network": "none",
                "requirements": [],
            },
            "paths": {
                "workspace": "/veritas/work",
                "artifacts": "/veritas/artifacts",
                "events": "/veritas/events/events.jsonl",
                "control": "/veritas/control",
            },
            "integrity": {
                "adapter_manifest_sha256": "b" * 64,
                "experiment_manifest_sha256": "c" * 64,
            },
        }

        self.assertEqual(
            list(validator.iter_errors(request)),
            [],
        )

        request["lifecycle"][
            "endpoint_wait_timeout_seconds"
        ] = 0

        self.assertTrue(
            list(validator.iter_errors(request))
        )

    def test_cli_marks_empty_reachability_as_not_attempted(self) -> None:
        result = {
            "overall_status": "completed",
            "independent_measurements": {
                "unpack": {"status": "true"},
                "boot": {"status": "true"},
                "reachability": [],
            },
        }

        summary = summarise_result(result)

        self.assertIn(
            "reachable=not_attempted",
            summary,
        )


if __name__ == "__main__":
    unittest.main()
