# tests/test_runtime_requirements.py
"""
Onboarding Reliability Phase 3 -- Runtime Capability Registry.

Kept as its own standalone test file, not folded into
test_contract_consistency.py (added by the separate, at-this-point-still
unmerged fix/contract-consistency branch), so this branch stays mergeable
independently of that one -- same reasoning as
fix/docker-backend-cleanup being kept independent.

The one thing this MUST guarantee: every requirement name the contract
schema allows an adapter to declare has an explicit, checked-in status
in firmarbiter_core/runtime_requirements.py. Without this test, adding a
new requirement to the schema (as already happened twice for real:
docker-socket, then nested-containers) could silently leave it with no
documented backend status again -- exactly the gap this whole registry
exists to close.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from firmarbiter_core.runtime_requirements import (
    CapabilityStatus,
    DOCKER_BACKEND_CAPABILITIES,
    RequirementStatus,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_SCHEMA_PATH = (
    PROJECT_ROOT / "schemas" / "adapter-manifest-v1.schema.json"
)


class RuntimeRequirementsRegistryTests(unittest.TestCase):
    def _schema_requirement_enum(self) -> set[str]:
        schema = json.loads(MANIFEST_SCHEMA_PATH.read_text(encoding="utf-8"))
        return set(
            schema["properties"]["runtime"]["properties"]["requirements"]
            ["items"]["enum"]
        )

    def test_every_schema_requirement_has_a_registered_status(self) -> None:
        schema_requirements = self._schema_requirement_enum()
        registered = set(DOCKER_BACKEND_CAPABILITIES.keys())

        undocumented = schema_requirements - registered
        self.assertFalse(
            undocumented,
            f"Requirement(s) {sorted(undocumented)} are legal in "
            f"schemas/adapter-manifest-v1.schema.json but have no entry "
            f"in firmarbiter_core/runtime_requirements.py's "
            f"DOCKER_BACKEND_CAPABILITIES. Add one -- SUPPORTED, "
            f"EXPERIMENTAL, or UNSUPPORTED -- before anyone can rely on "
            f"--list-candidates to warn about it.",
        )

    def test_no_stale_registry_entries(self) -> None:
        """The reverse check: a registry entry for a requirement the
        schema no longer recognises would be silently dead code, and
        worse, could give false confidence about something no adapter
        can even legally declare anymore."""
        schema_requirements = self._schema_requirement_enum()
        registered = set(DOCKER_BACKEND_CAPABILITIES.keys())

        stale = registered - schema_requirements
        self.assertFalse(
            stale,
            f"Registry entries {sorted(stale)} don't correspond to any "
            f"requirement in the current schema -- remove them or check "
            f"whether the schema regressed.",
        )

    def test_every_entry_has_a_real_status_and_a_nonempty_note(self) -> None:
        for name, entry in DOCKER_BACKEND_CAPABILITIES.items():
            self.assertIsInstance(entry, RequirementStatus)
            self.assertIn(entry.status, list(CapabilityStatus))
            self.assertTrue(
                entry.note.strip(),
                f"{name}'s registry entry has an empty note -- the whole "
                f"point of this file is to say WHY, not just log a "
                f"status enum nobody can act on.",
            )

    def test_nested_containers_is_not_silently_marked_supported(self) -> None:
        """Specific regression guard for the one requirement that's
        actually blocked today. If this ever flips to SUPPORTED, it
        should be a deliberate one-line change someone reviews, not
        something that happens by accident alongside an unrelated edit."""
        entry = DOCKER_BACKEND_CAPABILITIES["nested-containers"]
        self.assertEqual(entry.status, CapabilityStatus.UNSUPPORTED)


if __name__ == "__main__":
    unittest.main()
