import csv
import unittest
from decimal import Decimal
from pathlib import Path

from agents.future_pair_state import FuturePairState
from presenters.reporting import _render_future_pair
from services.past_reach import past_pairs_reaching, reach_sentence

ROOT = Path(__file__).resolve().parents[1]
LARGE = {"variety": "조생귤", "grade": "특", "size": "소", "package": "상자", "package_kg": "5"}


def run_pair(daily_storage_cost):
    pair = FuturePairState()
    option = {"loss_percent": 2, "daily_storage_cost": daily_storage_cost, "other_cost_krw": daily_storage_cost,
              "fee_percent": 5}
    return pair.apply("new", pair.version, {
        "quantity": 3000, "price_basis": "gross", "start_date": "2026-11-17", "price_a": 2500,
        "option_a": {**option, "delay_days": 3}, "option_b": {**option, "loss_percent": 4, "delay_days": 10},
        "price_mode": "history", "history_filters": LARGE})


class PastPairsReachingTest(unittest.TestCase):
    def test_counts_pairs_at_or_above_threshold(self):
        # A 1000원, 손익분기 1100원이면 +10% 이상인 쌍만 센다(경계값 포함).
        reach = past_pairs_reaching(["-5", "9.99", "10", "25"], 1000, "1100", 7)
        self.assertEqual((reach["reached"], reach["total"]), (2, 4))
        # 확률처럼 읽히는 비율 값은 결과에 넣지 않는다.
        self.assertNotIn("share_pct", reach)
        self.assertEqual(reach["needed_change_pct"], "10.00")

    def test_missing_or_bad_inputs_return_none(self):
        self.assertIsNone(past_pairs_reaching(None, 1000, "1100", 7))
        self.assertIsNone(past_pairs_reaching([], 1000, "1100", 7))
        self.assertIsNone(past_pairs_reaching(["1"], 0, "1100", 7))
        self.assertIsNone(reach_sentence(None))

    def test_sentence_uses_counts_and_overlap_note(self):
        reach = past_pairs_reaching(["5", "-3", "12"], 1000, "1020", 7)
        full = reach_sentence(reach)
        partial = reach_sentence(reach, partial_costs=True)
        self.assertIn("3쌍 중 **2쌍**이 이 기준을 충족했습니다", full)
        self.assertIn("날짜가 겹치는 과거 자료", full)
        self.assertNotIn("%", full.replace("7일", ""))
        self.assertIn("2쌍**이 이 참고 기준(확인한 비용만 반영)을 충족했습니다", partial)


class PastReachInAnswerTest(unittest.TestCase):
    """예측 모델 없이도 답변에 손익분기 도달 빈도가 Python 계산값으로 나와야 한다."""

    def expected(self, threshold):
        with open(ROOT / "data/processed/price_comparison_availability.csv", encoding="utf-8-sig") as f:
            rows = [r for r in csv.DictReader(f)
                    if r["comparison_status"] == "COMPARABLE" and r["interval_days"] == "7"
                    and (r["variety"], r["grade"], r["size"]) == ("조생귤", "특", "소")
                    and Decimal(r["package_kg"]) == 5]
        return sum(2500 * (1 + Decimal(r["price_change_pct"]) / 100) >= Decimal(threshold) for r in rows), len(rows)

    def test_full_comparison_shows_reach(self):
        result = run_pair(0)
        reach = result["past_reach"]
        self.assertEqual((reach["reached"], reach["total"]), self.expected(result["break_even_vs_a_krw_per_kg"]))
        text = _render_future_pair(result)
        self.assertIn(f"20쌍 중 **{reach['reached']}쌍**이 이 기준을 충족했습니다", text)
        self.assertNotIn("확인한 비용만 반영", text)

    def test_reference_with_unknown_costs_shows_reach(self):
        result = run_pair(None)
        self.assertTrue(result["threshold_only"])
        reach = result["past_reach"]
        self.assertEqual((reach["reached"], reach["total"]), self.expected(result["break_even_vs_a_krw_per_kg"]))
        self.assertIn("이 참고 기준(확인한 비용만 반영)을 충족했습니다", _render_future_pair(result))

    def test_manual_b_price_has_no_reach(self):
        pair = FuturePairState()
        option = {"loss_percent": 2, "daily_storage_cost": 0, "other_cost_krw": 0, "fee_percent": 5}
        result = pair.apply("new", pair.version, {
            "quantity": 3000, "price_basis": "gross", "start_date": "2026-11-17", "price_a": 2500, "price_b": 2700,
            "option_a": {**option, "delay_days": 3}, "option_b": {**option, "delay_days": 10},
            "price_mode": "manual"})
        self.assertEqual(result["status"], "OK")
        self.assertIsNone(result.get("past_reach"))
        self.assertNotIn("가격쌍", _render_future_pair(result).split("\n")[2])


if __name__ == "__main__":
    unittest.main()
