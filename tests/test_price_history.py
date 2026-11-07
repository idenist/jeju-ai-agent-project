import csv
from pathlib import Path
import tempfile
import unittest

from agents.jeonghyeon.price_history import execute_tool, get_price_history, DEFAULT_FILE
from test_price_comparisons import row
from build_price_comparisons import REQUIRED, run


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.file = self.root / "out/price_comparison_availability.csv"
        self.args = dict(market_code="110001", variety="조생귤", grade="특", size="소",
                         package="상자", package_kg=5, trade_type="경매", interval_days=7)

    def prepare(self, rows):
        source = self.root / "source.csv"
        with source.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=REQUIRED)
            writer.writeheader()
            writer.writerows(rows)
        run(source, self.root / "out", self.root / "reports")

    def query(self, **changes):
        return get_price_history({**self.args, **changes}, self.file)

    def test_exact_conditions_exclude_other_size(self):
        self.prepare([row(), row("2026-10-08", "110"),
                      row(size="중", size_code="183"), row("2026-10-08", "300", size="중", size_code="183")])
        result = self.query()
        self.assertEqual(result["clean_pairs"], 1)
        self.assertEqual(result["clean_change_range_pct"], {"min": "10.000000", "max": "10.000000"})
        self.assertFalse(result["is_future_forecast"])

    def test_missing_fields_request_clarification(self):
        self.prepare([row()])
        result = get_price_history({"variety": "조생귤"}, self.file)
        self.assertEqual(result["status"], "NEEDS_CLARIFICATION")
        self.assertIn("grade", result["missing_fields"])
        self.assertEqual(result["choices"]["grade"], ["특"])

    def test_warning_pairs_not_used_in_range(self):
        self.prepare([row(), row("2026-10-08", "300", transaction_count="1")])
        result = self.query()
        self.assertEqual(result["status"], "NO_CLEAN_COMPARISON")
        self.assertEqual(result["warning_pairs"], 1)
        self.assertIn("LOW_SAMPLE", result["warning_counts"])
        self.assertIsNone(result["clean_change_range_pct"])
        self.assertEqual(result["examples"], [])

    def test_missing_date_is_not_filled(self):
        self.prepare([row(), row("2026-10-09")])
        result = self.query(base_date="2026-10-01")
        self.assertEqual(result["unavailable_reasons"][0]["code"], "NO_MARKET_OBSERVATION_ON_TARGET_DATE")
        self.assertEqual(result["clean_pairs"], 0)

    def test_base_date_and_equivalent_weight(self):
        self.prepare([row(), row("2026-10-08")])
        result = self.query(base_date="2026-10-01", package_kg="5.000")
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(result["clean_pairs"], 1)
        self.assertEqual(self.query(base_date="2026-12-01")["status"], "NO_DATA")

    def test_display_limit_does_not_change_counts(self):
        self.prepare([row(), row("2026-10-08"), row("2026-10-15")])
        result = self.query(limit=1)
        self.assertEqual(result["clean_pairs"], 2)
        self.assertEqual(result["displayed_examples"], 1)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["examples"][0]["base_date"], "2026-10-08")

    def test_same_labels_different_codes_are_not_combined(self):
        self.prepare([row(), row("2026-10-08"), row(grade_code="12"), row("2026-10-08", grade_code="12")])
        self.assertEqual(self.query()["status"], "AMBIGUOUS_CODES")

    def test_invalid_args_and_unknown_tool(self):
        self.prepare([row()])
        for changes in [dict(interval_days=True), dict(interval_days=0), dict(limit=0),
                        dict(package_kg="NaN"), dict(base_date="2026-02-30"), dict(extra="x")]:
            with self.subTest(changes=changes):
                self.assertEqual(execute_tool("get_price_history", {**self.args, **changes}, self.file)["status"], "ERROR")
        self.assertEqual(execute_tool("delete", {}, self.file)["status"], "ERROR")

    def test_defaults_only_for_global_single_market_and_trade(self):
        self.prepare([row(), row("2026-10-08")])
        args = {k: v for k, v in self.args.items() if k not in {"market_code", "trade_type"}}
        result = get_price_history(args, self.file)
        self.assertEqual(result["clean_pairs"], 1)
        self.assertEqual(result["applied_defaults"], {"market_code": "110001", "trade_type": "경매"})
        self.assertNotIn("market_code", get_price_history({"variety": "조생귤"}, self.file)["missing_fields"])
        self.assertIn("package", get_price_history({"variety": "조생귤"}, self.file)["missing_fields"])

    def test_multiple_markets_still_need_confirmation(self):
        self.prepare([row(), row(market_code="120001", variety="온주", variety_code="13")])
        result = get_price_history({"variety": "조생귤"}, self.file)
        self.assertIn("market_code", result["missing_fields"])
        self.assertNotIn("market_code", result["applied_defaults"])

    def test_explicit_condition_is_not_replaced(self):
        self.prepare([row()])
        result = self.query(market_code="999999")
        self.assertEqual(result["status"], "NO_DATA")
        self.assertNotIn("market_code", result["applied_defaults"])

    def test_valid_unavailable_interval_and_source(self):
        self.prepare([row()])
        result = self.query(interval_days=10)
        self.assertEqual(result["status"], "INTERVAL_NOT_AVAILABLE")
        self.assertEqual(result["available_intervals"], [3, 7, 14])
        self.assertEqual(result["source"], self.file.name)
        self.assertFalse(result["is_future_forecast"])

    def test_duplicate_file_rows_are_rejected(self):
        self.prepare([row(), row("2026-10-08")])
        with self.file.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            fields, rows = reader.fieldnames, list(reader)
        with self.file.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows + [rows[0]])
        self.assertEqual(execute_tool("get_price_history", self.args, self.file)["status"], "ERROR")

    def test_real_condition_counts_against_csv(self):
        if not DEFAULT_FILE.exists():
            self.skipTest("비교 자료 없음")
        result = get_price_history(self.args)
        with DEFAULT_FILE.open(encoding="utf-8-sig", newline="") as f:
            matched = [r for r in csv.DictReader(f) if all(
                r[k] == str(v) for k, v in self.args.items() if k != "package_kg")
                and float(r["package_kg"]) == 5]
        self.assertEqual(result["clean_pairs"], sum(r["comparison_status"] == "COMPARABLE" for r in matched))
        self.assertEqual(result["attempts"], len(matched))


if __name__ == "__main__":
    unittest.main()
