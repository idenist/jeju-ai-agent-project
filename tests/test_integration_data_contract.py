"""통합 시장가격·과거 가격 비교의 원천 버전 계약 테스트입니다."""
from __future__ import annotations

import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from agents.common.data_contract import market_key, verify_price_pair_sources


FIELDS = [
    "base_date", "target_date", "interval_days", "market_code", "variety_code",
    "grade_code", "size_code", "package", "package_kg", "trade_type",
    "comparison_status", "unavailable_reason",
]
IDENTITY = {
    "market_code": "110001", "variety_code": "01", "grade_code": "11",
    "size_code": "184", "package": "상자", "package_kg": "5.000",
    "trade_type": "경매",
}


class MarketIdentityTests(unittest.TestCase):
    def test_preserve_codes_and_match_numeric_package(self):
        a = market_key(IDENTITY)
        b = market_key({**IDENTITY, "package_kg": 5})
        self.assertEqual(a, b)
        self.assertEqual(a[1], "01")
        self.assertEqual(a[5], "5")

    def test_different_source_codes_are_not_merged(self):
        self.assertNotEqual(market_key(IDENTITY), market_key({**IDENTITY, "size_code": "183"}))

    def test_reject_unknown_and_invalid_package(self):
        for value in (None, True, 0, -2, "NaN", "Infinity", "미상"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                market_key({**IDENTITY, "package_kg": value})
        with self.assertRaises(ValueError):
            market_key({**IDENTITY, "variety_code": 1})
        with self.assertRaises(ValueError):
            market_key({k: v for k, v in IDENTITY.items() if k != "trade_type"})


class PricePairSourceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for path in ("data/processed", "data/reports", "data/metadata"):
            (self.root / path).mkdir(parents=True, exist_ok=True)
        self.market_file = self.root / "data/processed/daily_prices.csv"
        self.market_file.write_bytes(b"date,market_code,package_kg\r\n2026-10-01,110001,5.000\r\n")
        self.sha = hashlib.sha256(self.market_file.read_bytes()).hexdigest()
        self.report_path = self.root / "data/reports/price_comparison_report.json"
        self.manifest_path = self.root / "data/metadata/juneok_manifest.json"
        self.report = {
            "input_file": "daily_prices.csv", "input_sha256": self.sha,
            "calendar_basis": "calendar_days_exact_date",
            "pair_orientation": "target_date = base_date + interval_days",
            "interval_days": [3, 7, 14],
            "units": {"price": "KRW/kg", "weight": "kg", "price_change_pct": "percent, not fraction"},
            "observed_date_min": "2026-10-01", "observed_date_max": "2026-10-01",
            "rows": 1, "availability_rows": 2,
        }
        self.manifest = {"files": [{"file": "data/processed/daily_prices.csv", "sha256": self.sha}]}
        self.write_meta()
        pair = dict(zip(FIELDS, ["2026-10-01", "2026-10-08", 7, "110001", "01", "11", "184",
                                      "상자", 5, "경매", "COMPARABLE", ""]))
        absent = {**pair, "interval_days": 3, "target_date": "2026-10-04",
                  "comparison_status": "UNAVAILABLE", "unavailable_reason": "NO_MATCHING_CONDITION"}
        self.write_pairs("price_comparisons.csv", [pair])
        self.write_pairs("price_comparison_availability.csv", [pair, absent])

    def write_meta(self):
        self.report_path.write_text(json.dumps(self.report, ensure_ascii=False), encoding="utf-8")
        self.manifest_path.write_text(json.dumps(self.manifest, ensure_ascii=False), encoding="utf-8")

    def write_pairs(self, name, rows):
        with (self.root / "data/processed" / name).open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)

    def test_matching_sources_are_accepted(self):
        result = verify_price_pair_sources(self.root)
        self.assertEqual(result["status"], "OK", result)
        self.assertEqual(result["price_pair_rows"], 1)
        self.assertEqual(result["availability_rows"], 2)

    def test_updated_market_snapshot_is_rejected(self):
        newer = self.root / "new-market"
        (newer / "data/processed").mkdir(parents=True)
        (newer / "data/processed/daily_prices.csv").write_bytes(self.market_file.read_bytes() + b"different\r\n")
        result = verify_price_pair_sources(self.root, market_root=newer)
        self.assertEqual(result["status"], "DATA_ERROR")
        self.assertEqual(result["reason"], "VERSION_MISMATCH")

    def test_shared_input_change_is_rejected(self):
        self.market_file.write_bytes(self.market_file.read_bytes() + b"different\r\n")
        result = verify_price_pair_sources(self.root)
        self.assertEqual(result["reason"], "BASELINE_CHANGED")

    def test_report_manifest_mismatch_is_rejected(self):
        self.manifest["files"][0]["sha256"] = "0" * 64
        self.write_meta()
        self.assertEqual(verify_price_pair_sources(self.root)["reason"], "CONTRACT_MISMATCH")

    def test_missing_or_corrupted_csv_is_rejected(self):
        file = self.root / "data/processed/price_comparisons.csv"
        file.unlink()
        self.assertEqual(verify_price_pair_sources(self.root)["reason"], "SOURCE_READ_FAILED")
        self.write_pairs("price_comparisons.csv", [])
        self.assertEqual(verify_price_pair_sources(self.root)["reason"], "REPORT_COUNT_MISMATCH")

    def test_invalid_units_and_interval_are_rejected(self):
        self.report["units"]["price"] = "KRW/box"
        self.write_meta()
        self.assertEqual(verify_price_pair_sources(self.root)["reason"], "CONTRACT_MISMATCH")
        self.report["units"]["price"] = "KRW/kg"
        self.report["interval_days"] = [True]
        self.write_meta()
        self.assertEqual(verify_price_pair_sources(self.root)["reason"], "CONTRACT_MISMATCH")


class CurrentRepositoryTests(unittest.TestCase):
    def test_frozen_source_files_have_consistent_versions(self):
        # 프로젝트 단위 테스트는 개인이 선택한 새 스냅샷이 아닌 동결 원천을 검증합니다.
        root = Path(__file__).resolve().parents[1]
        if not (root / "data/reports/price_comparison_report.json").exists():
            self.skipTest("과거 가격 비교 보고서가 없습니다.")
        result = verify_price_pair_sources(root, market_root=root)
        self.assertEqual(result["status"], "OK", result)


if __name__ == "__main__":
    unittest.main()
