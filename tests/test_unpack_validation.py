from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from firmarbiter_core.probes.unpack_validation import (
    validate_unpack_export,
    write_unpack_evidence,
)


def create_valid_rootfs(
    contract_root: Path,
) -> Path:
    rootfs = (
        contract_root
        / "artifacts"
        / "unpack"
        / "rootfs"
    )

    (rootfs / "bin").mkdir(
        parents=True
    )
    (rootfs / "etc" / "init.d").mkdir(
        parents=True
    )
    (rootfs / "lib").mkdir(
        parents=True
    )

    busybox = rootfs / "bin" / "busybox"
    busybox.write_bytes(
        b"\x7fELFtest-binary"
    )
    busybox.chmod(0o755)

    (rootfs / "bin" / "sh").symlink_to(
        "busybox"
    )

    (rootfs / "etc" / "passwd").write_text(
        "root:x:0:0:root:/root:/bin/sh\n",
        encoding="utf-8",
    )

    startup = rootfs / "etc" / "init.d" / "rcS"
    startup.write_text(
        "#!/bin/sh\nexit 0\n",
        encoding="utf-8",
    )
    startup.chmod(0o755)

    return rootfs


class UnpackValidationTests(unittest.TestCase):
    def test_valid_linux_rootfs_is_true(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            contract_root = Path(temporary)

            create_valid_rootfs(contract_root)

            result = validate_unpack_export(
                contract_root=contract_root,
                requested=True,
            )

            self.assertEqual(
                result.status,
                "true",
            )

            self.assertGreaterEqual(
                result.regular_files,
                3,
            )

            self.assertGreaterEqual(
                result.elf_files,
                1,
            )

            self.assertGreaterEqual(
                result.shebang_scripts,
                1,
            )

            self.assertGreaterEqual(
                len(
                    result.anchor_groups_detected
                ),
                3,
            )

            self.assertEqual(
                len(result.tree_sha256 or ""),
                64,
            )

            self.assertTrue(
                result.inventory_complete
            )

            write_unpack_evidence(
                contract_root=contract_root,
                observation=result,
            )

            self.assertTrue(
                (
                    contract_root
                    / "artifacts"
                    / "unpack-observation.json"
                ).is_file()
            )

    def test_missing_export_is_false(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = validate_unpack_export(
                contract_root=Path(temporary),
                requested=True,
            )

            self.assertEqual(
                result.status,
                "false",
            )

            self.assertEqual(
                result.entry_count,
                0,
            )

    def test_unrequested_unpack_is_not_attempted(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = validate_unpack_export(
                contract_root=Path(temporary),
                requested=False,
            )

            self.assertEqual(
                result.status,
                "not_attempted",
            )

    def test_symlink_target_is_not_followed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            contract_root = root / "contract"

            rootfs = (
                contract_root
                / "artifacts"
                / "unpack"
                / "rootfs"
            )
            rootfs.mkdir(parents=True)

            external = root / "external-elf"
            external.write_bytes(
                b"\x7fELFoutside-rootfs"
            )

            (
                rootfs / "external-link"
            ).symlink_to(external)

            result = validate_unpack_export(
                contract_root=contract_root,
                requested=True,
            )

            self.assertEqual(
                result.symlinks,
                1,
            )

            self.assertEqual(
                result.elf_files,
                0,
            )

            self.assertEqual(
                result.status,
                "false",
            )


if __name__ == "__main__":
    unittest.main()
