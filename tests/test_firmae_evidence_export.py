from __future__ import annotations

import hashlib
import json
import os
import stat
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

from adapters.firmae.entrypoint import (
    ConsoleMirror,
    export_rootfs_tree,
)


class FirmAEEvidenceExportTests(unittest.TestCase):
    def test_rootfs_tree_and_console_are_exported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            artifacts = root / "artifacts"
            archive = root / "rootfs.tar.gz"

            (source / "bin").mkdir(parents=True)
            (source / "etc").mkdir(parents=True)
            (source / "bin" / "busybox").write_bytes(
                b"\x7fELF-test"
            )
            (source / "etc" / "passwd").write_text(
                "root:x:0:0::/:/bin/sh\n",
                encoding="utf-8",
            )

            private_directory = source / "etc" / "private"
            private_directory.mkdir(mode=0o700)
            private_file = private_directory / "secret"
            private_file.write_text(
                "test-secret\n",
                encoding="utf-8",
            )
            private_file.chmod(0o600)
            os.symlink(
                "busybox",
                source / "bin" / "sh",
            )

            with tarfile.open(
                archive,
                mode="w:gz",
            ) as handle:
                handle.add(source, arcname=".")

            archive_hash_before = hashlib.sha256(
                archive.read_bytes()
            ).hexdigest()

            export_rootfs_tree(
                rootfs_archive=archive,
                artifacts_path=artifacts,
            )

            archive_hash_after = hashlib.sha256(
                archive.read_bytes()
            ).hexdigest()

            exported = (
                artifacts
                / "unpack"
                / "rootfs"
            )

            self.assertTrue(
                (exported / "bin" / "busybox").is_file()
            )
            self.assertEqual(
                os.readlink(exported / "bin" / "sh"),
                "busybox",
            )

            exported_private_directory = (
                exported / "etc" / "private"
            )
            exported_private_file = (
                exported_private_directory / "secret"
            )

            directory_mode = stat.S_IMODE(
                exported_private_directory.stat().st_mode
            )
            file_mode = stat.S_IMODE(
                exported_private_file.stat().st_mode
            )

            self.assertTrue(directory_mode & stat.S_IROTH)
            self.assertTrue(directory_mode & stat.S_IXOTH)
            self.assertTrue(file_mode & stat.S_IROTH)
            self.assertEqual(
                exported_private_file.read_text(
                    encoding="utf-8"
                ),
                "test-secret\n",
            )

            self.assertEqual(
                archive_hash_after,
                archive_hash_before,
            )

            export_metadata = json.loads(
                (
                    artifacts
                    / "unpack"
                    / "export-metadata.json"
                ).read_text(encoding="utf-8")
            )

            self.assertTrue(
                export_metadata["source_archive_unchanged"]
            )
            self.assertEqual(
                export_metadata["source_archive_sha256_before"],
                archive_hash_before,
            )
            self.assertEqual(
                export_metadata["source_archive_sha256_after"],
                archive_hash_after,
            )
            self.assertTrue(
                export_metadata["permission_normalised"]
            )
            self.assertGreaterEqual(
                export_metadata[
                    "permission_normalised_directories"
                ],
                1,
            )
            self.assertGreaterEqual(
                export_metadata[
                    "permission_normalised_files"
                ],
                1,
            )

            serial_source = root / "qemu.serial.log"
            serial_export = (
                artifacts
                / "boot"
                / "guest-console.log"
            )
            mirror = ConsoleMirror(
                source_path=serial_source,
                destination_path=serial_export,
            )
            mirror.start()

            serial_source.write_bytes(b"Linux version test\n")
            time.sleep(0.35)

            with serial_source.open("ab") as handle:
                handle.write(b"init started: BusyBox\n")

            time.sleep(0.35)
            mirror.stop()

            self.assertEqual(
                serial_export.read_bytes(),
                (
                    b"Linux version test\n"
                    b"init started: BusyBox\n"
                ),
            )


if __name__ == "__main__":
    unittest.main()
