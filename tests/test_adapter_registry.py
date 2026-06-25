from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from veritas_core.adapter_registry import (
    AdapterRegistryError,
    discover_adapters,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = (
    PROJECT_ROOT / "schemas" / "adapter-manifest-v1.schema.json"
)
FIXTURE_ROOT = (
    PROJECT_ROOT / "tests" / "fixtures" / "adapters"
)


class AdapterRegistryTests(unittest.TestCase):
    def test_discovers_valid_adapter_without_registry_entry(self) -> None:
        adapters = discover_adapters(FIXTURE_ROOT, SCHEMA_PATH)

        self.assertEqual(set(adapters), {"mock-adapter"})

        record = adapters["mock-adapter"]

        self.assertEqual(record.adapter_id, "mock-adapter")
        self.assertEqual(len(record.manifest_sha256), 64)
        self.assertEqual(
            record.manifest["adapter"]["contract_version"],
            "1.0",
        )

    def test_unknown_manifest_field_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory) / "adapters"

            shutil.copytree(FIXTURE_ROOT, temporary_root)

            manifest_path = (
                temporary_root / "mock-adapter" / "adapter.yaml"
            )

            with manifest_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    "\n"
                    "success_phrase: firmware booted successfully\n"
                )

            with self.assertRaises(AdapterRegistryError) as context:
                discover_adapters(temporary_root, SCHEMA_PATH)

            self.assertIn(
                "Additional properties are not allowed",
                str(context.exception),
            )

    def test_duplicate_adapter_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory) / "adapters"

            shutil.copytree(FIXTURE_ROOT, temporary_root)

            shutil.copytree(
                temporary_root / "mock-adapter",
                temporary_root / "second-package",
            )

            with self.assertRaises(AdapterRegistryError) as context:
                discover_adapters(temporary_root, SCHEMA_PATH)

            self.assertIn(
                "Duplicate adapter id 'mock-adapter'",
                str(context.exception),
            )


if __name__ == "__main__":
    unittest.main()
