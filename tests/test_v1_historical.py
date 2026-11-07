"""V1-F01/F02: 과거 가격쌍, 가상 손익, 근거 품질, 실제 Tool Calling 연결 검사.

독립적으로 고정한 V1 개발용 골드값을 검증한다. 최종 비교용 holdout은 읽지 않는다.
"""
from __future__ import annotations

import csv
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from agents.v1_agent import execute_tool, run_turn
from agents.historical_analysis import analyze_historical_delays, format_historical_report

PRICE_FILE = Path(__file__).resolve().parents[1] / "data/processed/daily_prices.csv"
REFERENCE_DATE = date(2026, 11, 7)


def inputs(**changes):
    """조생귤·특·소·5kg, 3톤·7일·2만원·2% 기본 시연 조건."""
    data = {
        "variety": "조생귤", "grade": "특", "size": "소", "package_kg": 5,
        "quantity_kg": 3000, "delay_days": 7,
        "daily_storage_cost_krw": 20000, "loss_percent": 2,
        "as_of_date": None, "min_transactions_per_day": 5,
    }
    data.update(changes)
    return data


class FakeResponses:
    def __init__(self, values):
        self.values = list(values)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.values.pop(0)


class FakeClient:
    def __init__(self, values):
        self.responses = FakeResponses(values)


class HistoricalV1Tests(unittest.TestCase):
    def test_historical_gold_counts_and_median(self):
        """골드 EVAL-001: 후보 21→제외 1→유효 20, 이익7·손실13."""
        result = analyze_historical_delays(inputs(), PRICE_FILE, analysis_date=REFERENCE_DATE)
        n = result["numeric"]
        self.assertEqual(n["candidate_pair_count"], 21)
        self.assertEqual(n["excluded_low_sample_pair_count"], 1)
        self.assertEqual(n["valid_pair_count"], 20)
        self.assertEqual((n["profit_pair_count"], n["loss_pair_count"], n["tie_pair_count"]), (7, 13, 0))
        self.assertEqual((n["min_difference_krw"], n["median_difference_krw"], n["max_difference_krw"]),
                         ("-3590888", "-556205", "3730699"))
        self.assertEqual(n["last_observed_date"], "2026-11-03")
        self.assertEqual(n["days_since_last_observation"], 4)
        self.assertEqual(result["status"], "descriptive_only")
        self.assertFalse(result["is_forecast"])
        self.assertIn("not_forecast", result["warnings"])

    def test_price_up_but_money_loss(self):
        """골드 EVAL-004: 가격 1.54% 상승해도 보관비·감량을 반영하면 손실."""
        result = analyze_historical_delays(inputs(), PRICE_FILE, analysis_date=REFERENCE_DATE)
        pair = next(p for p in result["pairs"] if p["start_date"] == "2026-10-07")
        self.assertEqual(pair["end_date"], "2026-10-14")
        self.assertEqual(pair["price_change_percent"], "1.54")
        self.assertEqual(pair["difference_krw"], "-173791")
        self.assertTrue(pair["included"])

    def test_low_sample_exclusion_is_auditable(self):
        result = analyze_historical_delays(inputs(), PRICE_FILE, analysis_date=REFERENCE_DATE)
        excluded = [p for p in result["pairs"] if not p["included"]]
        self.assertEqual(len(excluded), 1)
        self.assertEqual(excluded[0]["start_date"], "2026-10-01")
        self.assertEqual(excluded[0]["start_transactions"], 3)
        self.assertEqual(excluded[0]["exclusion_reason"], "low_sample")
        self.assertIn("low_sample_pairs_excluded", result["warnings"])

    def test_as_of_never_uses_newer_prices(self):
        """골드 EVAL-006: 10/23 상한 이후 날짜는 시작·끝 모두 사용 금지."""
        result = analyze_historical_delays(
            inputs(as_of_date="2026-10-23"), PRICE_FILE, analysis_date=REFERENCE_DATE
        )
        self.assertEqual(result["numeric"]["candidate_pair_count"], 12)
        self.assertEqual(result["numeric"]["valid_pair_count"], 11)
        self.assertEqual(result["numeric"]["profit_pair_count"], 2)
        self.assertEqual(result["numeric"]["last_observed_date"], "2026-10-23")
        self.assertEqual(result["numeric"]["days_since_last_observation"], 15)
        self.assertTrue(all(p["end_date"] <= "2026-10-23" for p in result["pairs"]))

    def test_no_price_pair_is_unavailable(self):
        result = analyze_historical_delays(
            inputs(as_of_date="2026-10-03"), PRICE_FILE, analysis_date=REFERENCE_DATE
        )
        self.assertEqual(result["numeric"]["valid_pair_count"], 0)
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["numeric"]["median_difference_krw"])
        self.assertIn("insufficient_valid_pairs", result["warnings"])
        self.assertIn("분석 가능한 과거 구간이 없습니다", format_historical_report(result))

    def test_not_number_of_trading_days(self):
        """10월 1일과 10월 9일만 있으면 정확히 7일 차이의 날짜쌍은 없다."""
        columns = ["date", "market_code", "market", "variety", "grade", "size",
                   "package", "package_kg", "trade_type", "total_kg",
                   "total_amount_krw", "transaction_count", "quality_flag"]
        with tempfile.TemporaryDirectory() as d:
            csv_file = Path(d) / "prices.csv"
            with csv_file.open("w", encoding="utf-8-sig", newline="") as file:
                w = csv.writer(file)
                w.writerow(columns)
                for day, amount in (("2026-10-01", 20000), ("2026-10-09", 30000)):
                    w.writerow((day, "110001", "서울가락", "조생귤", "특", "소",
                                "상자", "5.000", "경매", 10, amount, 5, "OK"))
            result = analyze_historical_delays(inputs(), csv_file, analysis_date=REFERENCE_DATE)
            self.assertEqual(result["numeric"]["candidate_pair_count"], 0)

    def test_quality_stale_and_not_statistical_confidence(self):
        out = analyze_historical_delays(inputs(), PRICE_FILE, analysis_date=REFERENCE_DATE)
        self.assertIn("stale_price", out["warnings"])
        self.assertIn("overlapping_windows_possible", out["warnings"])
        self.assertIsNone(out["quality"]["statistical_confidence_level"])
        self.assertFalse(out["quality"]["is_future_probability"])
        fresh = analyze_historical_delays(inputs(), PRICE_FILE, analysis_date=date(2026, 11, 5))
        self.assertNotIn("stale_price", fresh["warnings"])

    def test_wrong_sample_size_invalidates_claim(self):
        out = analyze_historical_delays(
            inputs(min_transactions_per_day=50000), PRICE_FILE, analysis_date=REFERENCE_DATE
        )
        self.assertEqual(out["numeric"]["excluded_low_sample_pair_count"], 21)
        self.assertEqual(out["numeric"]["valid_pair_count"], 0)
        self.assertEqual(out["status"], "unavailable")

    def test_invalid_storage_assumptions_are_rejected(self):
        for update in ({"quantity_kg": 0}, {"loss_percent": 100},
                       {"daily_storage_cost_krw": -1}, {"delay_days": 0},
                       {"package_kg": 0}, {"min_transactions_per_day": 0},
                       {"grade": ""}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                analyze_historical_delays(inputs(**update), PRICE_FILE, analysis_date=REFERENCE_DATE)

    def test_only_jeju_auction_of_same_product_is_queried(self):
        out = analyze_historical_delays(
            inputs(variety="온주", size="중", quantity_kg=1500,
                   daily_storage_cost_krw=7000, loss_percent=1),
            PRICE_FILE, analysis_date=REFERENCE_DATE
        )
        self.assertEqual(out["numeric"]["valid_pair_count"], 19)
        self.assertEqual(out["numeric"]["profit_pair_count"], 6)

    def test_quality_tool_emits_numbers_but_no_pairs(self):
        result = execute_tool("assess_evidence_quality", inputs(), PRICE_FILE,
                              analysis_date=REFERENCE_DATE)
        self.assertEqual(result["status"], "descriptive_only")
        self.assertEqual(result["numeric"]["valid_pair_count"], 20)
        self.assertNotIn("pairs", result)
        from agents.v1_agent import format_verified_result
        self.assertIn("근거 품질 점검", format_verified_result("assess_evidence_quality", result))

    def test_tool_call_round_trip_and_numbers_not_from_llm(self):
        call = SimpleNamespace(
            id="resp-v1-1", status="completed", output_text="",
            output=[SimpleNamespace(type="function_call", name="analyze_historical_delays",
                                    call_id="id-v1", arguments=json.dumps(inputs(), ensure_ascii=False))],
        )
        fake_answer = SimpleNamespace(
            id="resp-v1-2", status="completed",
            output_text="미래에 이득일 확률은 35%이며, 20개 중 15개가 이득입니다.", output=[],
        )
        client = FakeClient([call, fake_answer])
        answer, rid = run_turn(client, "과거 7일 보관하면 어땠을까?", prices_file=PRICE_FILE,
                               analysis_date=REFERENCE_DATE)
        self.assertEqual(rid, "resp-v1-2")
        self.assertIn("가상 이익: 7구간", answer)
        self.assertNotIn("15개가 이득", answer)
        self.assertIn("미래 이익 발생 확률", answer)  # 확률이 아니라는 경고 문구
        self.assertEqual(len(client.responses.requests), 2)
        self.assertEqual(client.responses.requests[1]["input"][0]["call_id"], "id-v1")

    def test_missing_information_still_asks_without_tools(self):
        client = FakeClient([SimpleNamespace(
            id="resp", status="completed", output_text="등급과 크기를 알려주세요.", output=[]
        )])
        text, _ = run_turn(client, "등급은 모르겠어", prices_file=PRICE_FILE)
        self.assertIn("등급", text)
        self.assertEqual(len(client.responses.requests), 1)


if __name__ == "__main__":
    unittest.main()
