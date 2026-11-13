"""통합 데이터·응답·계산 계약을 실제 함수 호출까지 연쇄 검증합니다."""
from __future__ import annotations

import csv
import hashlib
import json
import tempfile
import unittest
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from unittest.mock import patch

from agents.common.data_contract import verify_integrated_price_sources, verify_price_pair_sources
from agents.common.result_contract import normalize_tool_result
from scripts.build_price_comparisons import FIELDS, REQUIRED, run

ROOT = Path(__file__).resolve().parents[1]


def _observation(day: str, price: str) -> dict:
    """날짜·가격 외 거래조건이 동일한 합성 경매 행입니다."""
    return {
        "date": day, "market_code": "110001", "market": "서울가락",
        "variety_code": "01", "variety": "조생귤",
        "grade_code": "11", "grade": "특", "size_code": "184", "size": "소",
        "package": "상자", "package_kg": "5.000", "trade_type": "경매",
        "avg_price_kg": price, "total_kg": "10.000",
        "total_amount_krw": str(Decimal(price) * 10),
        "transaction_count": "10", "quality_flag": "OK",
    }


class FullSourceVerificationTests(unittest.TestCase):
    """기존 행 수 확인만으로 놓치는 결과 변조를 검출합니다."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for folder in ("data/processed", "data/reports", "data/metadata"):
            (self.root / folder).mkdir(parents=True)
        self.source = self.root / "data/processed/daily_prices.csv"
        with self.source.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=REQUIRED)
            writer.writeheader()
            writer.writerow(_observation("2026-10-01", "100.00"))
            writer.writerow(_observation("2026-10-08", "120.00"))
        run(self.source, self.root / "data/processed", self.root / "data/reports")
        self.manifest = self.root / "data/metadata/juneok_manifest.json"
        self.manifest.write_text(json.dumps({"files": [{
            "file": "data/processed/daily_prices.csv",
            "sha256": hashlib.sha256(self.source.read_bytes()).hexdigest(),
        }]}, ensure_ascii=False), encoding="utf-8")

    def _edit_pair(self, name, field, value):
        path = self.root / "data/processed" / name
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertTrue(rows)
        rows[0][field] = value
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)

    def test_original_pairs_and_report_are_consistent(self):
        result = verify_integrated_price_sources(self.root, market_root=self.root)
        self.assertEqual(result["status"], "OK", result)
        self.assertTrue(result["full_content_verified"])
        self.assertEqual(result["verified_comparison_rows"], 1)
        self.assertEqual(result["verified_availability_rows"], 6)

    def test_price_change_with_same_row_count_is_detected(self):
        self._edit_pair("price_comparisons.csv", "target_price_kg", "9999.00")
        # 경량 검사는 행 수만 확인하므로 이 변경을 놓칠 수 있습니다.
        self.assertEqual(verify_price_pair_sources(self.root, market_root=self.root)["status"], "OK")
        result = verify_integrated_price_sources(self.root, market_root=self.root)
        self.assertEqual(result["reason"], "PAIR_CONTENT_MISMATCH", result)

    def test_unavailable_reason_change_is_detected(self):
        self._edit_pair("price_comparison_availability.csv", "unavailable_reason", "임의 변경")
        result = verify_integrated_price_sources(self.root, market_root=self.root)
        self.assertEqual(result["reason"], "PAIR_CONTENT_MISMATCH", result)

    def test_report_summary_change_is_detected(self):
        path = self.root / "data/reports/price_comparison_report.json"
        report = json.loads(path.read_text(encoding="utf-8"))
        report["by_interval"]["7"]["matched_pairs"] = 123
        path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(verify_price_pair_sources(self.root, market_root=self.root)["status"], "OK")
        result = verify_integrated_price_sources(self.root, market_root=self.root)
        self.assertEqual(result["reason"], "REPORT_CONTENT_MISMATCH", result)

    def test_selected_market_snapshot_mismatch_is_rejected(self):
        alternate = self.root / "다른시장버전"
        path = alternate / "data/processed/daily_prices.csv"
        path.parent.mkdir(parents=True)
        path.write_bytes(self.source.read_bytes() + b"changed\r\n")
        result = verify_integrated_price_sources(self.root, market_root=alternate)
        self.assertEqual(result["reason"], "VERSION_MISMATCH", result)

    def test_validation_does_not_modify_original_files(self):
        paths = [self.source,
                 self.root / "data/processed/price_comparisons.csv",
                 self.root / "data/processed/price_comparison_availability.csv",
                 self.root / "data/reports/price_comparison_report.json"]
        originals = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        self.assertEqual(verify_integrated_price_sources(self.root, market_root=self.root)["status"], "OK")
        self.assertEqual(originals, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths})


class ActualRepositoryAcceptanceTests(unittest.TestCase):
    """실제 동결 자료를 이용한 도구 간 의미·상태 회귀 점검입니다."""

    def setUp(self):
        if not (ROOT / "data/processed/daily_prices.csv").is_file():
            self.skipTest("이 실행 환경에는 원본 가격 데이터가 없습니다.")

    def test_frozen_repository_passes_full_verification(self):
        result = verify_integrated_price_sources(ROOT, market_root=ROOT)
        self.assertEqual(result["status"], "OK", result)
        self.assertTrue(result["full_content_verified"])

    def test_latest_market_price_agrees_with_independent_baseline(self):
        from agents.baseline import load_price_observations
        from agents.juneok.evidence import get_market_evidence

        query = {"market_code": "110001", "variety": "조생귤", "grade": "특",
                 "size": "소", "package": "상자", "package_kg": 5}
        # 운영자가 별도 활성화한 스냅샷과 동결 자료 검사를 혼동하지 않습니다.
        with patch("agents.juneok.data_refresh.resolve_market_root", return_value=ROOT):
            result = get_market_evidence(query, ROOT)
        self.assertEqual(result["status"], "OK", result)
        baseline = load_price_observations(ROOT / "data/processed/daily_prices.csv",
                                           variety="조생귤", grade="특", size="소",
                                           package_kg=Decimal("5"))
        self.assertTrue(baseline)
        self.assertEqual(result["observed_date"], baseline[-1].date.isoformat())
        self.assertEqual(Decimal(result["price_per_kg"]), baseline[-1].price_per_kg)
        normalized = normalize_tool_result("get_market_evidence", result)
        self.assertEqual(normalized["status"], "OK")
        self.assertFalse(normalized["data"]["is_producer_net_price"])
        self.assertFalse(normalized["data"]["is_live_price"])

    def test_actual_historical_prices_keep_warning_and_source(self):
        from agents.jeonghyeon.price_history import get_price_history
        query = {"market_code": "110001", "variety": "조생귤", "grade": "특",
                 "size": "소", "package": "상자", "package_kg": 5,
                 "trade_type": "경매", "interval_days": 7, "limit": 3}
        raw = get_price_history(query)
        self.assertEqual(raw["status"], "OK", raw)
        self.assertGreater(raw["clean_pairs"], 0)
        self.assertIsNotNone(raw["source"])
        result = normalize_tool_result("get_price_history", raw)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["data"], raw)
        self.assertFalse(result["data"]["is_future_forecast"])

    def test_real_v0_and_extended_calculations_agree_for_simple_case(self):
        from agents.agent import compare_shipping
        from agents.juneok.comparison import compare_shipment_options
        from agents.juneok.state import demo_scenario

        basic = compare_shipping({
            "variety": "조생귤", "grade": "특", "size": "소", "package_kg": 5,
            "quantity_kg": 3000, "delay_days": 7, "daily_storage_cost": 20000,
            "loss_percent": 2, "current_price_per_kg": 3000,
            "future_price_per_kg": None,
        }, ROOT / "data/processed/daily_prices.csv")
        extended = compare_shipment_options(demo_scenario())
        self.assertEqual(extended["status"], "OK", extended)
        self.assertEqual(normalize_tool_result("compare_shipping", basic)["status"], "OK")
        self.assertEqual(Decimal(basic["current_gross_krw"].replace(",", "")), Decimal(extended["current_proceeds_krw"]))
        self.assertEqual(Decimal(basic["break_even_price_krw_per_kg"].replace(",", "")),
                         Decimal(extended["options"][0]["break_even_price_per_kg"]).quantize(
                             Decimal("0.01"), rounding=ROUND_HALF_UP))
        self.assertFalse(extended["is_future_forecast"])

    def test_missing_sale_price_is_not_guessed_from_market(self):
        from agents.juneok.comparison import compare_shipment_options
        from agents.juneok.state import empty_scenario
        result = normalize_tool_result("compare_shipment_options", compare_shipment_options(empty_scenario()))
        self.assertEqual(result["status"], "NEEDS_INPUT")
        self.assertIn("current_price_per_kg", result["missing_fields"])


if __name__ == "__main__":
    unittest.main()
