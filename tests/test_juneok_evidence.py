"""Real-data checks and adversarial source fixtures for evidence tools."""
from __future__ import annotations

import csv
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from agents.juneok.config import PROJECT_ROOT
from agents.juneok.evidence import get_historical_shipment_context, get_market_evidence


MARKET = {"market_code": "110001", "variety": "조생귤", "grade": "특", "size": "소",
          "package": "상자", "package_kg": 5, "as_of": None}
SHIPMENT = {"shipment_variety": "노지온주", "target_month": 11, "start_year": None, "end_year": None}


def write_csv(root, relative, rows):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.market_rows = [
            {"date": "2026-10-01", "market_code": "110001", "variety": "조생귤", "variety_code": "01",
             "grade": "특", "grade_code": "11", "size": "소", "size_code": "185", "package": "상자",
             "package_kg": "5.000", "trade_type": "경매", "avg_price_kg": "33.33", "total_kg": "3.000",
             "total_amount_krw": "100.00", "transaction_count": "1", "quality_flag": "LOW_SAMPLE"},
            {"date": "2026-10-03", "market_code": "110001", "variety": "조생귤", "variety_code": "01",
             "grade": "특", "grade_code": "11", "size": "소", "size_code": "185", "package": "상자",
             "package_kg": "5.000", "trade_type": "경매", "avg_price_kg": "100.14", "total_kg": "7.000",
             "total_amount_krw": "701.00", "transaction_count": "2", "quality_flag": "ok"},
        ]
        write_csv(self.root, "data/processed/daily_prices.csv", self.market_rows)
        write_csv(self.root, "data/processed/monthly_shipments.csv", [
            {"year_month": "2024-11", "category": "감귤류", "variety": "노지온주", "shipment_tons": "11.20",
             "observed_days": "30", "data_warning": "ok"},
            {"year_month": "2025-09", "category": "감귤류", "variety": "노지온주", "shipment_tons": "4.50",
             "observed_days": "17", "data_warning": "ok"},
        ])
        write_csv(self.root, "data/processed/daily_shipments.csv", [{"date": "2024-11-01"}, {"date": "2025-09-17"}])

    def test_actual_november_values_and_summary(self):
        r = get_historical_shipment_context(SHIPMENT)
        self.assertEqual(r["status"], "OK")
        self.assertEqual(r["years_covered"], list(range(2018, 2025)))
        self.assertEqual(r["series"][-1]["shipment_tons"], "43781.08")
        self.assertEqual(r["summary"]["min"], "43781.08")
        self.assertEqual(r["summary"]["median"], "53120.77")
        self.assertEqual(r["source"]["source_last_date"], "2025-09-17")

    def test_missing_year_is_not_zero(self):
        r = get_historical_shipment_context({**SHIPMENT, "start_year": 2025, "end_year": 2025})
        self.assertEqual(r["status"], "NO_DATA")
        self.assertEqual(r["series"], [])

    def test_no_automatic_market_to_shipment_mapping(self):
        self.assertEqual(get_historical_shipment_context({**SHIPMENT, "shipment_variety": "조생귤"})["status"], "NO_DATA")

    def test_invalid_shipment_inputs(self):
        for patch in ({"target_month": True}, {"target_month": 13}, {"target_month": 1.5},
                      {"start_year": True}, {"start_year": 2025, "end_year": 2024}, {"unknown": 1}):
            with self.subTest(patch=patch):
                self.assertEqual(get_historical_shipment_context({**SHIPMENT, **patch}, self.root)["status"], "INVALID_INPUT")

    def test_custom_source_dates_are_derived(self):
        r = get_historical_shipment_context(SHIPMENT, self.root)
        self.assertEqual(r["source"]["source_first_date"], "2024-11-01")
        self.assertEqual(r["series"][0]["shipment_tons"], "11.20")

    def test_partial_month_excluded_from_summary(self):
        r = get_historical_shipment_context({**SHIPMENT, "target_month": 9}, self.root)
        self.assertEqual(r["status"], "OK")
        self.assertIn("PARTIAL_MONTH", r["series"][0]["warning"])
        self.assertIsNone(r["summary"]["median"])
        self.assertEqual(r["summary"]["years_count"], 0)

    def test_report_fallback_and_missing_date_warning(self):
        (self.root / "data/processed/daily_shipments.csv").unlink()
        folder = self.root / "data/reports"
        folder.mkdir()
        (folder / "shipment_quality_report.json").write_text(json.dumps({"source_date_min": "2024-11-01",
            "source_date_max": "2025-09-17", "missing_date_rows_by_variety": {"노지온주": 2}}), encoding="utf-8")
        r = get_historical_shipment_context(SHIPMENT, self.root)
        self.assertEqual(r["source"]["source_last_date"], "2025-09-17")
        self.assertIn("MISSING_DATE_ROWS_EXCLUDED", r["series"][0]["warning"])

    def test_missing_source_dates_not_invented(self):
        (self.root / "data/processed/daily_shipments.csv").unlink()
        r = get_historical_shipment_context(SHIPMENT, self.root)
        self.assertIsNone(r["source"]["source_last_date"])
        self.assertEqual(r["source"]["source_last_month"], "2025-09")

    def test_missing_and_corrupt_shipment_files(self):
        p = self.root / "data/processed/monthly_shipments.csv"
        p.write_text("year_month\ninvalid\n", encoding="utf-8")
        self.assertEqual(get_historical_shipment_context(SHIPMENT, self.root)["status"], "DATA_ERROR")
        p.unlink()
        self.assertEqual(get_historical_shipment_context(SHIPMENT, self.root)["status"], "DATA_ERROR")

    def test_price_recomputed_from_totals_not_rounded_average(self):
        r = get_market_evidence({**MARKET, "as_of": "2026-10-01"}, self.root)
        self.assertEqual(r["status"], "OK")
        self.assertEqual(Decimal(r["price_per_kg"]), Decimal(100) / Decimal(3))
        self.assertNotEqual(Decimal(r["price_per_kg"]), Decimal("33.33"))
        self.assertEqual(r["quality_flags"], ["LOW_SAMPLE"])

    def test_latest_and_equivalent_package_weight(self):
        r = get_market_evidence({**MARKET, "package_kg": "5.0"}, self.root)
        self.assertEqual(r["observed_date"], "2026-10-03")
        self.assertEqual(Decimal(r["price_per_kg"]), Decimal(701) / Decimal(7))

    def test_as_of_outside_source_never_backfills(self):
        for day in ("2026-09-30", "2026-10-04"):
            self.assertEqual(get_market_evidence({**MARKET, "as_of": day}, self.root)["status"], "NO_DATA")

    def test_inside_range_prior_observation_is_explicit(self):
        r = get_market_evidence({**MARKET, "as_of": "2026-10-02"}, self.root)
        self.assertEqual(r["observed_date"], "2026-10-01")
        self.assertTrue(any("앞선" in w for w in r["warnings"]))

    def test_missing_condition_needs_input(self):
        r = get_market_evidence({**MARKET, "grade": None}, self.root)
        self.assertEqual(r["status"], "NEEDS_INPUT")
        self.assertEqual(r["missing_fields"], ["grade"])

    def test_invalid_market_inputs(self):
        for patch in ({"package_kg": "NaN"}, {"package_kg": True}, {"package_kg": 0},
                      {"as_of": "2026-02-30"}, {"market_code": 110001}, {"unknown": 1}):
            with self.subTest(patch=patch):
                self.assertEqual(get_market_evidence({**MARKET, **patch}, self.root)["status"], "INVALID_INPUT")

    def test_different_condition_not_substituted(self):
        self.assertEqual(get_market_evidence({**MARKET, "grade": "상"}, self.root)["status"], "NO_DATA")

    def test_ambiguous_labels_rejected(self):
        self.market_rows.append({**self.market_rows[0], "variety_code": "99"})
        write_csv(self.root, "data/processed/daily_prices.csv", self.market_rows)
        r = get_market_evidence(MARKET, self.root)
        self.assertEqual(r["status"], "INVALID_INPUT")
        self.assertEqual(r["reason"], "AMBIGUOUS_CODES")

    def test_duplicate_and_nonpositive_market_rows_rejected(self):
        write_csv(self.root, "data/processed/daily_prices.csv", self.market_rows + [self.market_rows[0]])
        self.assertEqual(get_market_evidence(MARKET, self.root)["status"], "DATA_ERROR")
        write_csv(self.root, "data/processed/daily_prices.csv", [{**self.market_rows[0], "total_kg": "0"}])
        self.assertEqual(get_market_evidence(MARKET, self.root)["status"], "DATA_ERROR")

    def test_actual_market_price_has_provenance(self):
        # Pin the historical observation; a legitimate newer CSV must not invalidate this oracle.
        r = get_market_evidence({**MARKET, "as_of": "2026-11-03"})
        self.assertEqual(r["status"], "OK")
        self.assertEqual(r["observed_date"], "2026-11-03")
        self.assertFalse(r["is_producer_net_price"])
        self.assertEqual(Decimal(r["price_per_kg"]), Decimal(r["total_amount_krw"]) / Decimal(r["total_kg"]))


if __name__ == "__main__":
    unittest.main()
