from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from travel_eval.metrics import calculate_metrics, is_expected_subset
from travel_eval.runner import DEFAULT_FIXTURES, DEFAULT_POLICY, load_json, run_suite


ROOT = Path(__file__).resolve().parents[1]


class GoldenReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results, cls.expected = run_suite(DEFAULT_FIXTURES, DEFAULT_POLICY)

    def test_every_scenario_matches_curated_goldens(self):
        expected_by_id = {item["scenario_id"]: item for item in self.expected}
        for result in self.results:
            with self.subTest(scenario=result["scenario_id"]):
                self.assertTrue(is_expected_subset(result, expected_by_id[result["scenario_id"]]))

    def test_replay_is_deterministic(self):
        second_results, _ = run_suite(DEFAULT_FIXTURES, DEFAULT_POLICY)
        self.assertEqual(self.results, second_results)

    def test_notification_authority_invariant(self):
        for result in self.results:
            approved = {
                decision["decision_id"]: decision
                for decision in result["decisions"]
                if decision["verdict"] != "SUPPRESS"
            }
            for notification in result["notifications"]:
                with self.subTest(notification=notification["notification_id"]):
                    self.assertIn(notification["decision_id"], approved)
                    decision = approved[notification["decision_id"]]
                    self.assertEqual(
                        notification["search_requested"],
                        decision["verdict"] == "NOTIFY_AND_SEARCH",
                    )

    def test_automated_metrics_meet_release_shape(self):
        metrics = calculate_metrics(self.results, self.expected)
        self.assertEqual(metrics["scenario_pass_rate"], 1.0)
        self.assertEqual(metrics["unauthorized_notification_count"], 0)
        self.assertEqual(metrics["duplicate_notification_rate"], 0.0)


class ContractAndFixtureTests(unittest.TestCase):
    def test_all_json_files_parse(self):
        for path in ROOT.rglob("*.json"):
            with self.subTest(path=path.relative_to(ROOT)):
                load_json(path)

    def test_json_schemas_declare_draft_and_version(self):
        for path in (ROOT / "travel_eval" / "schemas").glob("*.schema.json"):
            schema = load_json(path)
            with self.subTest(schema=path.name):
                self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
                self.assertIn("$id", schema)

    def test_document_manifest_hashes(self):
        manifest = load_json(ROOT / "travel_eval" / "fixtures" / "documents" / "manifest.json")
        self.assertEqual(len(manifest["fixtures"]), 3)
        for fixture in manifest["fixtures"]:
            pdf_path = ROOT / fixture["path"]
            expected_path = ROOT / fixture["expected"]
            with self.subTest(fixture=fixture["fixture_id"]):
                self.assertTrue(pdf_path.is_file())
                self.assertTrue(expected_path.is_file())
                digest = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
                self.assertEqual(digest, fixture["sha256"])


if __name__ == "__main__":
    unittest.main()
