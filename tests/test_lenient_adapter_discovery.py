# tests/test_lenient_adapter_discovery.py
"""
Onboarding Reliability -- one broken/incomplete adapter directory must
not prevent --list-candidates (or --run against a DIFFERENT, working
adapter) from working at all.

Confirmed live: adapters/greenhouse/ mid-onboarding (adapter.yaml
present, Dockerfile not yet written -- a completely normal in-progress
state) took down `python run_firmarbiter.py --list-candidates`
entirely, hiding firmae/firmadyne/emba/fact_extractor, which were all
fine. Root cause: discover_adapters() raises on the FIRST problem it
finds anywhere under adapters/, and run_firmarbiter.py's CLI had no
other discovery path.

discover_adapters() itself is untouched and still has this fail-fast
contract on purpose (see tests/test_adapter_registry.py -- those tests
still pass unmodified). This file tests the new, separate
discover_adapters_lenient(), which run_firmarbiter.py's CLI now uses
instead.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from firmarbiter_core.adapter_registry import (
    AdapterRegistryError,
    discover_adapters,
    discover_adapters_lenient,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = (
    PROJECT_ROOT / "schemas" / "adapter-manifest-v1.schema.json"
)
FIXTURE_ROOT = (
    PROJECT_ROOT / "tests" / "fixtures" / "adapters"
)


class LenientAdapterDiscoveryTests(unittest.TestCase):
    def test_broken_adapter_does_not_block_discovery_of_a_good_one(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "adapters"
            root.mkdir()

            shutil.copytree(
                FIXTURE_ROOT / "mock-adapter", root / "good-adapter"
            )
            # Mirrors the real greenhouse situation exactly: a manifest
            # that exists, but no Dockerfile yet.
            shutil.copytree(
                FIXTURE_ROOT / "mock-adapter", root / "broken-adapter"
            )
            (root / "broken-adapter" / "Dockerfile").unlink()

            # The strict function must still fail fast on this tree --
            # confirms this test's fixture actually reproduces the bug,
            # and confirms discover_adapters()'s existing contract is
            # unchanged by any of this.
            with self.assertRaises(AdapterRegistryError):
                discover_adapters(root, SCHEMA_PATH)

            discovered, errors = discover_adapters_lenient(
                root, SCHEMA_PATH
            )

            self.assertEqual(set(discovered), {"mock-adapter"})
            self.assertEqual(set(errors), {"broken-adapter"})
            self.assertIn("Dockerfile does not exist", errors["broken-adapter"])

    def test_no_broken_adapters_behaves_like_the_strict_version(
        self,
    ) -> None:
        discovered, errors = discover_adapters_lenient(
            FIXTURE_ROOT, SCHEMA_PATH
        )
        self.assertEqual(set(discovered), {"mock-adapter"})
        self.assertEqual(errors, {})

    def test_duplicate_adapter_id_is_reported_not_raised(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "adapters"
            root.mkdir()

            shutil.copytree(
                FIXTURE_ROOT / "mock-adapter", root / "first-copy"
            )
            shutil.copytree(
                FIXTURE_ROOT / "mock-adapter", root / "second-copy"
            )

            discovered, errors = discover_adapters_lenient(
                root, SCHEMA_PATH
            )

            # Exactly one of the two directories wins the id (sorted by
            # directory name, so "first-copy" loads first); the other is
            # reported as an error, neither one silently dropped nor a
            # hard crash for the whole tree.
            self.assertEqual(set(discovered), {"mock-adapter"})
            self.assertEqual(set(errors), {"second-copy"})
            self.assertIn("Duplicate adapter id", errors["second-copy"])

    def test_missing_adapters_root_still_raises(self) -> None:
        """The one thing that should still be fatal: no adapters/
        directory at all is a real setup problem, not a per-adapter
        one."""
        with self.assertRaises(AdapterRegistryError):
            discover_adapters_lenient(
                Path("/nonexistent/adapters/path"), SCHEMA_PATH
            )


if __name__ == "__main__":
    unittest.main()
