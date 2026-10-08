import json
import re
import unittest
from pathlib import Path

from firmarbiter_core.run_identity import MAX_RUN_ID_LENGTH, build_run_id

RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# The exact case that was rejected for fact-extractor in corpus-50-v8 (134 chars).
LONG_CASE = (
    "openwrt-25.12.2-ath79-mikrotik-mikrotik_routerboard-911-lite-"
    "squashfs-sysupgrade-v7-11cb686b76f5"
)


class RunIdentityTests(unittest.TestCase):
    def test_ids_that_fit_are_unchanged(self):
        self.assertEqual(
            build_run_id("corpus-50-v8", "dfl-m510_gpl.tar-8da65c641e9a", "emba", 1),
            "corpus-50-v8.dfl-m510_gpl.tar-8da65c641e9a.emba.attempt-1",
        )

    def test_the_corpus_50_v8_failure_now_fits_the_schema(self):
        old = f"corpus-50-v8.{LONG_CASE}.fact-extractor.attempt-1"
        self.assertGreater(len(old), MAX_RUN_ID_LENGTH)
        new = build_run_id("corpus-50-v8", LONG_CASE, "fact-extractor", 1)
        self.assertLessEqual(len(new), MAX_RUN_ID_LENGTH)
        self.assertRegex(new, RUN_ID_PATTERN)
        self.assertTrue(new.endswith(".fact-extractor.attempt-1"))

    def test_shortened_ids_are_deterministic_and_unique(self):
        a = build_run_id("corpus-50-v8", LONG_CASE, "fact-extractor", 1)
        self.assertEqual(a, build_run_id("corpus-50-v8", LONG_CASE, "fact-extractor", 1))
        other_case = LONG_CASE[:-12] + "ffffffffffff"
        self.assertNotEqual(a, build_run_id("corpus-50-v8", other_case, "fact-extractor", 1))
        self.assertNotEqual(a, build_run_id("corpus-50-v8", LONG_CASE, "fact-extractor", 2))
        self.assertNotEqual(a, build_run_id("corpus-50-v8", LONG_CASE, "firmadyne", 1))

    def test_cases_sharing_a_long_prefix_stay_distinct(self):
        prefix = "x" * 150
        ids = {build_run_id("exp", f"{prefix}-{i}", "emba", 1) for i in range(50)}
        self.assertEqual(len(ids), 50)
        for run_id in ids:
            self.assertLessEqual(len(run_id), MAX_RUN_ID_LENGTH)
            self.assertRegex(run_id, RUN_ID_PATTERN)

    def test_an_experiment_id_that_leaves_no_room_is_rejected_clearly(self):
        with self.assertRaises(ValueError):
            build_run_id("e" * 120, LONG_CASE, "fact-extractor", 1)

    def test_limit_matches_the_schemas(self):
        root = Path(__file__).resolve().parents[1] / "schemas"
        for name in ("run-request-v1.schema.json", "adapter-event-v1.schema.json"):
            text = (root / name).read_text(encoding="utf-8")
            self.assertIn("{0,%d}" % (MAX_RUN_ID_LENGTH - 1), text, name)


if __name__ == "__main__":
    unittest.main()
