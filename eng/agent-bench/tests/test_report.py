import copy
import csv
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location("bench_report", Path(__file__).parents[1] / "report.py")
report = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(report)


def native_usage():
    return {
        "totalNanoAiu": 5_740_762_500,
        "tokenDetails": {key: {"tokenCount": value} for key, value in (
            ("input", 147), ("cache_read", 2_756_880), ("cache_write", 113_325), ("output", 31_317))},
        "modelMetrics": {"model": {"usage": {
            "inputTokens": 2_870_352, "outputTokens": 31_317,
            "cacheReadTokens": 2_756_880, "cacheWriteTokens": 113_325, "reasoningTokens": 21_478}}},
        "agentMetrics": {"main": {"totalNanoAiu": 999_999_999_999}},
    }


class UsageTests(unittest.TestCase):
    def test_native_identity_non_cached_definition_and_exact_credit_conversion(self):
        columns = report.usage_columns(native_usage())
        self.assertEqual(columns, {
            "cache_read_tokens": 2_756_880, "cache_write_tokens": 113_325,
            "uncached_input_tokens": 147, "output_tokens": 31_317,
            "non_cached_input_output_tokens": 31_464, "recorded_input_tokens": 2_870_352,
            "recorded_input_output_tokens": 2_901_669, "ai_credits": 5.7407625,
            "input_bucket_identity_verified": True})

    def test_absent_buckets_are_unknown_not_zero(self):
        self.assertTrue(all(value is None for value in report.usage_columns({}).values()))
        usage = native_usage()
        del usage["tokenDetails"]["cache_write"]
        columns = report.usage_columns(usage)
        self.assertIsNone(columns["cache_write_tokens"])
        self.assertIsNone(columns["input_bucket_identity_verified"])
        self.assertEqual(columns["non_cached_input_output_tokens"], 31_464)

    def test_zero_is_known(self):
        usage = {"totalNanoAiu": 0,
                 "tokenDetails": {key: {"tokenCount": 0} for key in ("input", "cache_read", "cache_write", "output")},
                 "modelMetrics": {"model": {"usage": {
                     "inputTokens": 0, "outputTokens": 0, "cacheReadTokens": 0, "cacheWriteTokens": 0}}}}
        columns = report.usage_columns(usage)
        self.assertTrue(columns["input_bucket_identity_verified"])
        self.assertEqual(columns["recorded_input_output_tokens"], 0)
        self.assertEqual(columns["ai_credits"], 0)

    def test_distinct_models_sum_once_not_duplicate_agent_metrics(self):
        usage = native_usage()
        usage["modelMetrics"]["second"] = copy.deepcopy(usage["modelMetrics"]["model"])
        for bucket in usage["tokenDetails"].values():
            bucket["tokenCount"] *= 2
        columns = report.usage_columns(usage)
        self.assertEqual(columns["recorded_input_output_tokens"], 2 * 2_901_669)
        self.assertEqual(columns["ai_credits"], 5.7407625)

    def test_contradictory_native_counts_rejected(self):
        for key in ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens"):
            with self.subTest(key=key):
                usage = native_usage()
                usage["modelMetrics"]["model"]["usage"][key] += 1
                with self.assertRaisesRegex(ValueError, "Native"):
                    report.usage_columns(usage)

    def test_invalid_counts_rejected(self):
        for value in (-1, True, 1.5, "12"):
            with self.subTest(value=value):
                usage = native_usage()
                usage["tokenDetails"]["input"]["tokenCount"] = value
                with self.assertRaisesRegex(ValueError, "nonnegative integer"):
                    report.usage_columns(usage)

    def test_table_reports_scope_format_and_benign_edit_qualifications(self):
        base = {"model": "gpt-6.1-sol", "variant": "typescript", "agent_wall_ms": 1000,
                "strict_repair_oracle4": False, "strict_preservation_oracle4": False,
                "diagnosis_oracle5_supplemental": True,
                "runtime_workflows_success": True, **report.usage_columns(native_usage())}
        preservation = "\n".join(report.table([base], primary=True))
        self.assertIn("runtime pass; strict-only rejection (see qualifications)", preservation)
        unsafe = "\n".join(report.table([
            {**base, "agent_scope_violation": True, "runtime_workflows_success": None,
             "diagnosis_format_valid": False}], primary=True))
        self.assertIn("unsafe scope violation; retained failure", unsafe)
        self.assertIn("malformed JSON, not a wrong-root conclusion", unsafe)
        self.assertIn("| unknown |", unsafe)
        partial = "\n".join(report.table([
            {**base, "diagnosis_oracle5_supplemental": False,
             "diagnosis_oracle5_passed_roots": 3, "diagnosis_oracle5_total_roots": 4}], primary=True))
        self.assertIn("3/4 (fail)", partial)


class HandoffTests(unittest.TestCase):
    def setup_handoff(self, root):
        source = root / "previous"
        source.mkdir()
        rows, index = [], []
        usage = native_usage()
        for arm in ("raw", "typescript"):
            usage_dir = root / arm
            usage_dir.mkdir()
            report.write_json(usage_dir / "usage.json", usage)
            row = {"model": "model", "variant": arm, "native_nano_aiu": usage["totalNanoAiu"],
                   "agent_wall_ms": 1000, "runtime_workflows_success": None,
                   "strict_repair_oracle4": False, "diagnosis_oracle5_supplemental": False,
                   "agent_scope_violation": arm == "raw",
                   **{key: value["tokenCount"] for key, value in usage["tokenDetails"].items()}}
            rows.append(row)
            index.append({**row, "usage_path": str(usage_dir / "usage.json")})
        pilot = root / "pilot"
        pilot.mkdir()
        report.write_json(pilot / "usage.json", usage)
        costs = {"pilots": [{"model": "model", "variant": "raw", "path": str(pilot),
                            "native_nano_aiu": usage["totalNanoAiu"], "primary_eligibility": False}],
                 "primary_total_native_nano_aiu": 2 * usage["totalNanoAiu"],
                 "pilot_total_native_nano_aiu": usage["totalNanoAiu"]}
        for name, value in (
            ("aggregate.json", rows), ("artifact-index.json", index),
            ("diagnosis-reasons.json", rows), ("costs.json", costs),
            ("paired-results.json", [{"model": "model", **{row["variant"]: row for row in rows}}]),
            ("frozen-artifact-hashes.json", {}), ("measurements-oracle4.json", {"frozen": True}),
        ):
            report.write_json(source / name, value)
        (source / "report.md").write_text("# Smoke\n\n| Old table |\n| --- |\n| old |\n\nOriginal qualifications.\n")
        return source

    def test_revision_preserves_inputs_and_adds_explicit_csv_and_separate_pilot_cost(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.setup_handoff(root)
            hashes = {path: report.sha256(path) for path in root.rglob("*") if path.is_file()}
            output = root / "new"
            result = report.revise_handoff(source, output, "reporting-commit")
            self.assertEqual(result["primary_attempts"], 2)
            self.assertEqual(result["primary_ai_credits"], 11.481525)
            self.assertEqual(result["pilot_ai_credits"], 5.7407625)
            self.assertTrue(all(report.sha256(path) == sha for path, sha in hashes.items()))
            self.assertEqual((output / "measurements-oracle4.json").read_bytes(),
                             (source / "measurements-oracle4.json").read_bytes())
            with (output / "aggregate.csv").open() as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["cache_write_tokens"], "113325")
            self.assertEqual(rows[0]["non_cached_input_output_tokens"], "31464")
            self.assertEqual(rows[0]["recorded_input_output_tokens"], "2901669")
            self.assertEqual(rows[0]["ai_credits"], "5.7407625")
            self.assertEqual(rows[0]["agent_scope_violation"], "True")
            with (output / "excluded-pilots.csv").open() as stream:
                self.assertEqual(len(list(csv.DictReader(stream))), 1)
            provenance = report.read_json(output / "reporting-provenance.json")
            self.assertEqual(provenance["reporter_commit"], "reporting-commit")
            self.assertEqual(provenance["reporter_sha256"],
                             hashlib.sha256(Path(report.__file__).read_bytes()).hexdigest())
            self.assertFalse(provenance["original_scoring_timing_cost_modified"])
            self.assertIn("Original qualifications.", (output / "report.md").read_text())

    def test_existing_or_overlapping_outputs_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.setup_handoff(Path(directory))
            for output in (source, source / "nested", source.parent):
                with self.subTest(output=output), self.assertRaises(ValueError):
                    report.revise_handoff(source, output)

    def test_changed_usage_frozen_scores_or_missing_index_rejected_before_writing(self):
        for defect in ("usage", "frozen", "index"):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = self.setup_handoff(root)
                if defect == "usage":
                    usage = native_usage()
                    usage["totalNanoAiu"] += 1
                    report.write_json(root / "raw/usage.json", usage)
                elif defect == "frozen":
                    report.write_json(source / "frozen-artifact-hashes.json",
                                      {str(source / "measurements-oracle4.json"): "wrong-hash"})
                else:
                    report.write_json(source / "artifact-index.json", [])
                with self.assertRaises(ValueError):
                    report.revise_handoff(source, root / "new")
                self.assertFalse((root / "new").exists())


if __name__ == "__main__":
    unittest.main()
