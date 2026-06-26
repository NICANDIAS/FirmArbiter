from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from veritas_core.probes.boot_validation import (
    validate_boot_evidence,
    write_boot_evidence,
)


def write_console(
    contract_root: Path,
    content: str,
) -> None:
    console_path = (
        contract_root
        / "artifacts"
        / "boot"
        / "guest-console.log"
    )

    console_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    console_path.write_text(
        content,
        encoding="utf-8",
    )


def authenticity_record(
    status: str,
) -> SimpleNamespace:
    return SimpleNamespace(
        independent_measurement=SimpleNamespace(
            status=status
        )
    )


class BootValidationTests(unittest.TestCase):
    def test_kernel_and_userspace_console_is_true(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            contract_root = Path(temporary)

            write_console(
                contract_root,
                "Linux version 6.6.0-test\n"
                "Kernel command line: console=ttyS0\n"
                "Freeing unused kernel memory\n"
                "Run /sbin/init as init process\n",
            )

            result = validate_boot_evidence(
                contract_root=contract_root,
                requested=True,
                candidate_events=[],
                lifecycle=SimpleNamespace(
                    run_outcome="completed"
                ),
                authenticity_records=[],
            )

            self.assertEqual(
                result.status,
                "true",
            )

            self.assertIn(
                "linux_kernel_and_userspace_console",
                result.methods,
            )

            self.assertGreaterEqual(
                len(result.console.kernel_markers),
                1,
            )

            self.assertGreaterEqual(
                len(result.console.userspace_markers),
                1,
            )

            write_boot_evidence(
                contract_root=contract_root,
                observation=result,
            )

            self.assertTrue(
                (
                    contract_root
                    / "artifacts"
                    / "boot-observation.json"
                ).is_file()
            )

    def test_kernel_panic_is_false(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            contract_root = Path(temporary)

            write_console(
                contract_root,
                "Linux version 5.10.0-test\n"
                "VFS: Unable to mount root fs\n"
                "Kernel panic - not syncing: "
                "VFS: Unable to mount root fs\n",
            )

            result = validate_boot_evidence(
                contract_root=contract_root,
                requested=True,
                candidate_events=[],
                lifecycle=SimpleNamespace(
                    run_outcome="completed"
                ),
                authenticity_records=[],
            )

            self.assertEqual(
                result.status,
                "false",
            )

            self.assertIn(
                "kernel_panic",
                result.console.failure_markers,
            )

    def test_authenticated_service_is_true(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = validate_boot_evidence(
                contract_root=Path(temporary),
                requested=True,
                candidate_events=[],
                lifecycle=SimpleNamespace(
                    run_outcome="completed"
                ),
                authenticity_records=[
                    authenticity_record("true")
                ],
            )

            self.assertEqual(
                result.status,
                "true",
            )

            self.assertIn(
                "authenticated_firmware_service",
                result.methods,
            )

    def test_candidate_claim_alone_is_inconclusive(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = validate_boot_evidence(
                contract_root=Path(temporary),
                requested=True,
                candidate_events=[
                    {
                        "event": (
                            "candidate_boot_reported"
                        )
                    }
                ],
                lifecycle=SimpleNamespace(
                    run_outcome="completed"
                ),
                authenticity_records=[],
            )

            self.assertEqual(
                result.status,
                "inconclusive",
            )

            self.assertTrue(
                result.candidate_boot_claim_present
            )

    def test_timeout_without_evidence_is_false(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = validate_boot_evidence(
                contract_root=Path(temporary),
                requested=True,
                candidate_events=[],
                lifecycle=SimpleNamespace(
                    run_outcome=(
                        "experiment_timeout"
                    )
                ),
                authenticity_records=[],
            )

            self.assertEqual(
                result.status,
                "false",
            )

    def test_unrequested_emulation_is_not_attempted(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = validate_boot_evidence(
                contract_root=Path(temporary),
                requested=False,
                candidate_events=[],
                lifecycle=None,
                authenticity_records=[],
            )

            self.assertEqual(
                result.status,
                "not_attempted",
            )

    def test_blocked_emulation_uses_supplied_reason(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = validate_boot_evidence(
                contract_root=Path(temporary),
                requested=False,
                candidate_events=[],
                lifecycle=None,
                authenticity_records=[],
                not_attempted_reason=(
                    "Emulation was blocked by unpack failure"
                ),
            )

            self.assertEqual(result.status, "not_attempted")
            self.assertEqual(
                result.reason,
                "Emulation was blocked by unpack failure",
            )


if __name__ == "__main__":
    unittest.main()
