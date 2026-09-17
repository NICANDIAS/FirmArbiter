"""
tests/test_contract_consistency.py
-----------------------------------
Onboarding Reliability M1 — Contract Consistency.

These tests don't test any one adapter's behaviour. They test that
FIRMARBITER's OWN contract definitions agree with each other and with
themselves. Every check here corresponds to a real bug found while
reviewing the repo for onboarding difficulty:

  - the event schema couldn't represent a stage the manifest schema
    allowed a candidate to declare (EMBA's real "static-analysis" stage)
  - the template told authors to return a stage_outcome value the event
    schema doesn't accept ("completed" instead of "succeeded") — and
    EMBA's real entrypoint.py had exactly that bug
  - per-adapter copies of schemas/adapter-event-v1.schema.json and
    lifecycle/request.py are physically duplicated, not shared, so
    nothing stopped them drifting from the canonical version
  - tools/test_adapter.py's synthetic request was missing several
    fields the real run-request schema requires, so the smoke test
    that's supposed to catch onboarding problems failed on every
    adapter, including working ones

The goal is not "test today's bugs are fixed" — it's "make this whole
class of bug loud and immediate the next time someone changes a schema,
a template, or an adapter, instead of silent until a real run fails."
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMAS_DIR = PROJECT_ROOT / "schemas"
ADAPTERS_DIR = PROJECT_ROOT / "adapters"

MANIFEST_SCHEMA_PATH = SCHEMAS_DIR / "adapter-manifest-v1.schema.json"
EVENT_SCHEMA_PATH = SCHEMAS_DIR / "adapter-event-v1.schema.json"
REQUEST_SCHEMA_PATH = SCHEMAS_DIR / "run-request-v1.schema.json"

# Adapters that are allowed to exist without a real, buildable
# candidate behind them in this environment (the template itself).
NON_CANDIDATE_DIRS = {"_template"}


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _discover_adapter_dirs() -> list[Path]:
    return sorted(
        d for d in ADAPTERS_DIR.iterdir()
        if d.is_dir() and d.name not in NON_CANDIDATE_DIRS
        and (d / "adapter.yaml").exists()
    )


class StageVocabularyConsistencyTests(unittest.TestCase):
    """
    The manifest/request schemas define which stages a candidate can
    legally declare and be asked to run. The event schema defines which
    stages an adapter can legally REPORT completing. These must agree —
    a candidate that can legally declare a stage it can never legally
    report finishing is a framework contradiction, not a candidate bug.
    """

    def _stage_enum(self, schema: dict, *path: str) -> set[str]:
        node = schema
        for key in path:
            node = node["properties"][key] if "properties" in node else node[key]
        return set(node["items"]["enum"])

    def test_manifest_and_event_stage_enums_match(self) -> None:
        manifest_schema = _load_json(MANIFEST_SCHEMA_PATH)
        event_schema = _load_json(EVENT_SCHEMA_PATH)

        manifest_stages = set(
            manifest_schema["properties"]["capabilities"]["properties"]["stages"]["items"]["enum"]
        )
        event_stage_values = set(event_schema["properties"]["stage"]["enum"])

        undeclarable = manifest_stages - event_stage_values
        self.assertFalse(
            undeclarable,
            f"Stage(s) {sorted(undeclarable)} can be declared in an adapter "
            f"manifest but can never appear in a stage_completed event. "
            f"An adapter that declares one of these has no legal way to "
            f"report finishing it. Add these to schemas/adapter-event-v1"
            f".schema.json's 'stage' enum, or remove them from "
            f"schemas/adapter-manifest-v1.schema.json's 'capabilities.stages' "
            f"enum — don't leave the two disagreeing.",
        )

    def test_request_and_event_stage_enums_match(self) -> None:
        request_schema = _load_json(REQUEST_SCHEMA_PATH)
        event_schema = _load_json(EVENT_SCHEMA_PATH)

        request_stages = set(
            request_schema["properties"]["run"]["properties"]["requested_stages"]["items"]["enum"]
        )
        event_stage_values = set(event_schema["properties"]["stage"]["enum"])

        self.assertEqual(
            request_stages, event_stage_values,
            "run-request-v1.schema.json's requested_stages enum and "
            "adapter-event-v1.schema.json's stage enum must be exactly "
            "the same set — a stage that can be requested but never "
            "reported (or vice versa) is a contract contradiction.",
        )

    def test_every_real_adapter_only_declares_reportable_stages(self) -> None:
        """
        This is the test that would have caught EMBA's real
        adapter.yaml declaring 'static-analysis' before the event
        schema could represent it.
        """
        event_schema = _load_json(EVENT_SCHEMA_PATH)
        event_stage_values = set(event_schema["properties"]["stage"]["enum"])

        for adapter_dir in _discover_adapter_dirs():
            manifest = yaml.safe_load((adapter_dir / "adapter.yaml").read_text())
            declared = set(manifest["capabilities"]["stages"])
            unreportable = declared - event_stage_values
            self.assertFalse(
                unreportable,
                f"{adapter_dir.name}/adapter.yaml declares stage(s) "
                f"{sorted(unreportable)} that adapter-event-v1.schema.json "
                f"cannot represent in a stage_completed event.",
            )


class StageOutcomeConsistencyTests(unittest.TestCase):
    """
    'completed' is a valid outcome for the adapter's overall run
    (adapter_stopped(outcome=...)) but NOT a valid stage_outcome for an
    individual stage_completed event. Confusing the two is exactly the
    bug EMBA's entrypoint.py had.
    """

    VALID_STAGE_OUTCOMES = {"succeeded", "failed", "inconclusive", "not_applicable"}

    def test_event_schema_stage_outcome_enum_is_what_we_think_it_is(self) -> None:
        event_schema = _load_json(EVENT_SCHEMA_PATH)
        enum = set(event_schema["properties"]["stage_outcome"]["enum"])
        self.assertEqual(enum, self.VALID_STAGE_OUTCOMES)

    def test_no_adapter_entrypoint_returns_completed_as_a_stage_outcome(self) -> None:
        """
        Cheap static guard: 'return "completed", ...' (or the same with
        single quotes) inside an entrypoint.py is always wrong — it's
        the exact shape of EMBA's real bug. This won't catch every way
        of constructing the string, but it catches the common,
        literal case cheaply on every commit.
        """
        pattern = re.compile(r'return\s+["\']completed["\']\s*,')

        for adapter_dir in list(_discover_adapter_dirs()) + [ADAPTERS_DIR / "_template"]:
            entrypoint = adapter_dir / "entrypoint.py"
            if not entrypoint.exists():
                continue
            text = entrypoint.read_text(encoding="utf-8")
            match = pattern.search(text)
            self.assertIsNone(
                match,
                f"{entrypoint.relative_to(PROJECT_ROOT)} returns \"completed\" "
                f"as what looks like a stage_outcome. Valid stage_outcome "
                f"values are {sorted(self.VALID_STAGE_OUTCOMES)} — "
                f"\"completed\" is only valid for the adapter's overall "
                f"outcome (adapter_stopped(outcome=...)), not for a single "
                f"stage.",
            )


class SharedInfrastructureDriftTests(unittest.TestCase):
    """
    schemas/adapter-event-v1.schema.json and lifecycle/request.py are
    physically copied into every adapter directory rather than referenced
    from one canonical location (see adapters/*/schemas/,
    adapters/*/lifecycle/). Nothing stops these copies drifting from the
    canonical version except a test noticing. This is that test.

    This does not fix the duplication — it just makes drift loud instead
    of silent, until the duplication itself is addressed.
    """

    def test_event_schema_copies_match_canonical(self) -> None:
        canonical = EVENT_SCHEMA_PATH.read_text(encoding="utf-8")
        for adapter_dir in list(_discover_adapter_dirs()) + [ADAPTERS_DIR / "_template"]:
            copy_path = adapter_dir / "schemas" / "adapter-event-v1.schema.json"
            if not copy_path.exists():
                continue
            self.assertEqual(
                copy_path.read_text(encoding="utf-8"), canonical,
                f"{copy_path.relative_to(PROJECT_ROOT)} has drifted from "
                f"the canonical schemas/adapter-event-v1.schema.json. "
                f"Re-sync it (copy the canonical file over this one) — "
                f"this adapter is validating its own events against a "
                f"stale contract.",
            )

    def test_request_validator_copies_match_template(self) -> None:
        canonical = (ADAPTERS_DIR / "_template" / "lifecycle" / "request.py").read_text(
            encoding="utf-8"
        )
        for adapter_dir in _discover_adapter_dirs():
            copy_path = adapter_dir / "lifecycle" / "request.py"
            if not copy_path.exists():
                continue
            self.assertEqual(
                copy_path.read_text(encoding="utf-8"), canonical,
                f"{copy_path.relative_to(PROJECT_ROOT)} has drifted from "
                f"adapters/_template/lifecycle/request.py. This means this "
                f"adapter is validating its incoming request.json against "
                f"different rules than every other adapter.",
            )


class ScaffoldedAdapterIsSchemaValidTests(unittest.TestCase):
    """
    Runs the real --new-adapter scaffolding function and checks that
    what it produces would actually survive contact with the rest of
    the system: a schema-valid manifest, and a Dockerfile that installs
    what the copied lifecycle code needs at runtime.
    """

    def setUp(self) -> None:
        import sys
        sys.path.insert(0, str(PROJECT_ROOT))
        from run_firmarbiter import scaffold_new_adapter  # noqa: PLC0415
        self.scaffold_new_adapter = scaffold_new_adapter

        self.tmp_dir = Path(tempfile.mkdtemp(prefix="firmarbiter_scaffold_test_"))
        self.adapters_dir = self.tmp_dir / "adapters"
        self.adapters_dir.mkdir()
        shutil.copytree(
            ADAPTERS_DIR / "_template", self.adapters_dir / "_template",
            ignore=shutil.ignore_patterns("__pycache__"),
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_scaffolded_manifest_is_schema_valid(self) -> None:
        exit_code = self.scaffold_new_adapter("example-tool", self.adapters_dir)
        self.assertEqual(exit_code, 0)

        manifest_path = self.adapters_dir / "example-tool" / "adapter.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())

        schema = _load_json(MANIFEST_SCHEMA_PATH)
        validator = Draft202012Validator(schema)
        errors = list(validator.iter_errors(manifest))
        self.assertEqual(
            errors, [],
            "The manifest --new-adapter generates is not schema-valid: "
            + "; ".join(e.message for e in errors),
        )

    def test_scaffolded_dockerfile_installs_jsonschema_and_bakes_in_request_schema(self) -> None:
        self.scaffold_new_adapter("example-tool-2", self.adapters_dir)

        dockerfile = (self.adapters_dir / "example-tool-2" / "Dockerfile").read_text()
        self.assertIn(
            "pip install --no-cache-dir jsonschema", dockerfile,
            "The scaffolded Dockerfile doesn't install jsonschema, but "
            "lifecycle/events.py and lifecycle/request.py both import it "
            "at runtime — a fresh adapter would build fine and crash on "
            "its first real run.",
        )
        self.assertIn(
            "schemas/run-request-v1.schema.json", dockerfile,
            "The scaffolded Dockerfile doesn't bake in "
            "run-request-v1.schema.json, but lifecycle/request.py now "
            "validates incoming requests against it at runtime.",
        )


if __name__ == "__main__":
    unittest.main()
