"""Deterministic regression tests. No Streamlit, LLM, network, or API key needed."""
import csv
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from services.shipment_timing import (
    analyze_shipment_timing, format_timing_report, resolve_pair_dates,
)


class ShipmentTimingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        target = self.root / "data/processed"
        target.mkdir(parents=True)
        self.path = target / "daily_shipments.csv"
        self.rows = []

    def save(self):
        with self.path.open("w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.DictWriter(stream, fieldnames=["date", "category", "variety", "shipment_tons", "data_warning"])
            writer.writeheader()
            writer.writerows(self.rows)

    def add_window(self, year, center, tons, missing=None):
        day = date.fromisoformat(f"{year}-{center}")
        for offset in range(-3, 4):
            current = day + timedelta(days=offset)
            if current.isoformat() == missing:
                continue
            self.rows.append({"date": current.isoformat(), "category": "감귤류", "variety": "노지온주",
                              "shipment_tons": str(tons), "data_warning": "ok"})

    def go(self, a="2026-11-20", b="2026-11-27", variety="노지온주"):
        return analyze_shipment_timing({"option_a_date": a, "option_b_date": b,
                                        "shipment_variety": variety}, self.root)

    def test_same_year_matched_windows_and_medians(self):
        for year in (2018, 2019, 2020, 2021):
            self.add_window(year, "11-20", 100)
            self.add_window(year, "11-27", 200 if year != 2018 else 50)
        self.save()
        result = self.go()
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["paired_year_count"], 4)
        self.assertEqual(result["summary"]["a_median_7day_tons"], "700.00")
        self.assertEqual(result["summary"]["b_median_7day_tons"], "1400.00")
        self.assertEqual(result["summary"]["b_higher_years"], 3)
        self.assertEqual(result["summary"]["paired_median_change_pct"], "100.00")
        self.assertIsNone(result["recommended_option"])
        self.assertFalse(result["price_effect_estimated"])

    def test_missing_days_never_zero_filled(self):
        for year in (2018, 2019, 2020):
            self.add_window(year, "11-20", 100, missing=f"{year}-11-20")
            self.add_window(year, "11-27", 90)
        self.save()
        result = self.go()
        self.assertEqual(result["status"], "INSUFFICIENT_DATA")
        self.assertEqual(result["paired_year_count"], 0)
        self.assertEqual(result["excluded_reference_years"][0]["observed_days_a"], 6)

    def test_cross_year_anchors_b_in_following_year(self):
        for year in (2018, 2019, 2020):
            self.add_window(year, "12-24", 40)
            self.add_window(year + 1, "01-05", 50)
        self.save()
        result = self.go("2026-12-24", "2027-01-05")
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["years"][0]["historical_b_date"], "2019-01-05")
        self.assertEqual(result["years"][0]["historical_a_date"], "2018-12-24")

    def test_only_complete_historical_years_exclude_partial_2025(self):
        for year in (2023, 2024, 2025):
            self.add_window(year, "11-20", 50)
            self.add_window(year, "11-27", 100)
        self.save()
        result = self.go()
        self.assertEqual(result["status"], "INSUFFICIENT_DATA")
        self.assertEqual(result["paired_year_count"], 2)

    def test_no_arbitrary_variety_mapping(self):
        r = self.go(variety="조생귤")
        self.assertEqual(r["status"], "INVALID_INPUT")
        self.assertIn("노지온주", r["available_varieties"])

    def test_mandatory_dates_and_variety(self):
        r = analyze_shipment_timing({"shipment_variety": None,"option_a_date":None,"option_b_date":None}, self.root)
        self.assertEqual(r["status"], "NEEDS_INPUT")
        r = analyze_shipment_timing({"shipment_variety":"노지온주","option_a_date":None,"option_b_date":None}, self.root)
        self.assertEqual(r["status"], "NEEDS_INPUT")

    def test_duplicate_daily_rows_are_data_error(self):
        self.add_window(2020, "11-20", 10)
        self.rows.append(dict(self.rows[0]))
        self.save()
        r = self.go()
        self.assertEqual(r["status"], "DATA_ERROR")

    def test_corrupt_report_is_data_error(self):
        self.add_window(2020, "11-20", 10)
        self.save()
        p = self.root / "data/reports/shipment_quality_report.json"
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps({"unit": "ton", "daily_rows": 999}), encoding="utf-8")
        r = self.go()
        self.assertEqual(r["status"], "DATA_ERROR")

    def test_feb29_seasons_skip_non_leap_years(self):
        for year in (2020, 2024):
            for md in ("02-29", "03-15"):
                self.add_window(year, md, 50)
        self.save()
        r = self.go("2028-02-29", "2028-03-15")
        self.assertEqual(r["status"], "INSUFFICIENT_DATA")
        self.assertEqual(r["paired_year_count"], 2)

    def test_future_pair_dates_only_after_confirming_inputs(self):
        o = {"start_date": "2026-11-16", "option_a": {"delay_days": 3}, "option_b": {"delay_days": 10}}
        self.assertEqual(resolve_pair_dates(o), ("2026-11-19", "2026-11-26"))
        o["option_b"]["delay_days"] = None
        self.assertEqual(resolve_pair_dates(o), (None, None))
        o["option_b"]["delay_days"] = 2
        self.assertEqual(resolve_pair_dates(o), (None, None))

    def test_report_keeps_source_and_limitations(self):
        for year in (2018, 2019, 2020):
            self.add_window(year, "11-20", 100)
            self.add_window(year, "11-27", 120)
        self.save()
        result = self.go()
        body = format_timing_report(result)
        self.assertIn("제주데이터허브", body)
        self.assertIn("미래", body)
        self.assertIn("840", body)
        self.assertIn("2018", body)

    def test_bad_direction_or_large_gap_is_invalid(self):
        self.save()
        self.assertEqual(self.go("2026-11-27", "2026-11-20")["status"], "INVALID_INPUT")
        self.assertEqual(self.go("2026-11-20", "2027-11-21")["status"], "INVALID_INPUT")


if __name__ == "__main__":
    unittest.main()
