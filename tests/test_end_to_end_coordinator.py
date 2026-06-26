from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from veritas_core.adapter_registry import (
    discover_adapters,
)
from veritas_core.docker_backend import (
    DockerBackend,
)
from veritas_core.run_coordinator import (
    CandidateRunCoordinator,
    RunCoordinatorError,
    RunPolicy,
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


class EndToEndCoordinatorTests(unittest.TestCase):
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

    def test_complete_neutral_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            firmware_content = (
                b"<html><title>"
                b"Neutral Firmware Service"
                b"</title></html>\n"
            )

            firmware_path = root / "source-firmware.bin"
            firmware_path.write_bytes(
                firmware_content
            )

            firmware_hash = hashlib.sha256(
                firmware_content
            ).hexdigest()

            experiment_id = (
                "e2e-"
                + uuid.uuid4().hex[:12]
            )

            policy = RunPolicy(
                experiment_id=experiment_id,
                attempt=1,
                requested_stages=(
                    "unpack",
                    "emulate",
                    "endpoint-discovery",
                ),
                timeout_seconds=30,
                heartbeat_interval_seconds=1,
                heartbeat_timeout_seconds=3,
                shutdown_grace_seconds=5,
                cpu_cores=1,
                memory_bytes=268435456,
                pids_limit=128,
                endpoint_wait_timeout_seconds=10,
                stability_sample_count=3,
                stability_interval_seconds=0.2,
                stability_probe_timeout_seconds=1.0,
                compute_sample_interval_seconds=0.25,
            )

            results_root = root / "results"

            coordinator = CandidateRunCoordinator(
                backend=self.backend,
                event_schema_path=EVENT_SCHEMA,
                results_root=results_root,
            )

            result = coordinator.execute(
                adapter=self.adapter,
                firmware_path=firmware_path,
                case_id="case-001",
                policy=policy,
                trusted_content_sha256={
                    firmware_hash
                },
            )

            self.assertEqual(
                result["overall_status"],
                "completed",
            )

            request_document = json.loads(
                (
                    Path(result["run"]["result_directory"])
                    / "contract"
                    / "input"
                    / "request.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                request_document["lifecycle"][
                    "endpoint_wait_timeout_seconds"
                ],
                10,
            )

            self.assertEqual(
                result["setup"]["status"],
                "ready",
            )

            self.assertEqual(
                result[
                    "independent_measurements"
                ]["unpack"]["status"],
                "true",
            )

            self.assertEqual(
                len(
                    result[
                        "independent_measurements"
                    ]["unpack"]["tree_sha256"]
                ),
                64,
            )

            self.assertEqual(
                result[
                    "independent_measurements"
                ]["boot"]["status"],
                "true",
            )

            self.assertIn(
                "linux_kernel_and_userspace_console",
                result[
                    "independent_measurements"
                ]["boot"]["methods"],
            )

            reachability = result[
                "independent_measurements"
            ]["reachability"]

            self.assertEqual(len(reachability), 1)

            self.assertEqual(
                reachability[0][
                    "independent_measurement"
                ]["status"],
                "true",
            )

            stability = result[
                "independent_measurements"
            ]["stability"]

            self.assertEqual(len(stability), 1)

            self.assertEqual(
                stability[0][
                    "independent_measurement"
                ]["status"],
                "true",
            )

            authenticity = result[
                "independent_measurements"
            ]["authenticity"]

            self.assertEqual(
                len(authenticity),
                1,
            )

            self.assertEqual(
                authenticity[0][
                    "independent_measurement"
                ]["status"],
                "true",
            )

            compute_cost = result[
                "independent_measurements"
            ]["compute_cost"]

            self.assertIn(
                compute_cost["status"],
                {"complete", "partial"},
            )

            self.assertGreaterEqual(
                compute_cost[
                    "samples_collected"
                ],
                1,
            )

            residue = result[
                "independent_measurements"
            ]["environmental_residue"]

            self.assertFalse(
                residue["residue_detected"]
            )

            self.assertIn(
                residue["status"],
                {"clean", "partial_probe"},
            )

            remediation = result[
                "independent_measurements"
            ]["remediation"]

            self.assertIn(
                remediation["status"],
                {
                    "no_action_required",
                    "already_clean",
                },
            )

            claim_names = {
                claim["event"]
                for claim in result[
                    "candidate_claims"
                ]
            }

            self.assertIn(
                "extraction_complete",
                claim_names,
            )

            self.assertIn(
                "candidate_boot_reported",
                claim_names,
            )

            self.assertIn(
                "endpoint_reported",
                claim_names,
            )

            provenance = result["provenance"]

            self.assertEqual(
                provenance["firmware_sha256"],
                firmware_hash,
            )

            self.assertTrue(
                provenance[
                    "candidate_image_id"
                ].startswith("sha256:")
            )

            self.assertEqual(
                len(
                    provenance[
                        "adapter_manifest_sha256"
                    ]
                ),
                64,
            )

            self.assertEqual(
                len(
                    provenance[
                        "request_sha256"
                    ]
                ),
                64,
            )

            self.assertEqual(
                len(
                    provenance[
                        "event_stream_sha256"
                    ]
                ),
                64,
            )

            run_directory = Path(
                result["run"][
                    "result_directory"
                ]
            )

            final_result_path = (
                run_directory
                / "final-result.json"
            )

            final_hash_path = (
                run_directory
                / "final-result.sha256"
            )

            self.assertTrue(
                final_result_path.is_file()
            )

            self.assertTrue(
                final_hash_path.is_file()
            )

            saved_result = json.loads(
                final_result_path.read_text(
                    encoding="utf-8"
                )
            )

            self.assertEqual(
                saved_result["run"]["run_id"],
                result["run"]["run_id"],
            )

            # Historical result isolation:
            # the same run identity cannot overwrite this run.
            with self.assertRaises(
                RunCoordinatorError
            ):
                coordinator.execute(
                    adapter=self.adapter,
                    firmware_path=firmware_path,
                    case_id="case-001",
                    policy=policy,
                    trusted_content_sha256={
                        firmware_hash
                    },
                )


if __name__ == "__main__":
    unittest.main()
