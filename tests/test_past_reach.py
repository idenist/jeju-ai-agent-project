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


class PastOutcomesTest(unittest.TestCase):
    """기준을 넘었을 때와 못 넘었을 때의 금액 크기도 표와 같은 계산으로 보여야 한다."""

    def test_outcomes_match_table_extremes_and_counts(self):
        result = run_pair(0)
        out, reach = result["past_reach"]["outcomes"], result["past_reach"]
        self.assertEqual(out["gain_pairs"] + out["loss_pairs"], reach["total"])
        self.assertEqual(out["gain_pairs"], reach["reached"])
        table = [Decimal(row["difference_vs_a_krw"]) for row in result["options_b"]]
        # 가장 크게 오른/내린 사례 금액은 표의 최고·최저 행과 원 단위까지 같아야 한다.
        self.assertEqual(Decimal(out["best_krw"]), max(table).quantize(Decimal("1")))
        self.assertEqual(Decimal(out["worst_krw"]), min(table).quantize(Decimal("1")))
        text = _render_future_pair(result)
        self.assertIn(f"넘은 {out['gain_pairs']}쌍", text)
        self.assertIn("절반보다 적었습니다", text)

    def test_summary_buckets_are_factual(self):
        from services.past_reach import judgment_summary
        cases = {(0, 20): "없었습니다", (4, 20): "드물었습니다", (7, 20): "절반보다 적었습니다",
                 (10, 20): "같았습니다", (12, 20): "절반보다 많았습니다", (15, 20): "대부분", (20, 20): "모든 사례"}
        for (reached, total), words in cases.items():
            self.assertIn(words, judgment_summary({"reached": reached, "total": total}))
        for (reached, total), _ in cases.items():
            # 권유하는 말(기다리세요/파세요)은 쓰지 않는다.
            self.assertNotRegex(judgment_summary({"reached": reached, "total": total}), "하세요|추천")

    def test_partial_cost_and_small_sample_notes(self):
        partial = _render_future_pair(run_pair(None))
        self.assertIn("확인한 비용만 반영한 참고값", partial)
        pair = FuturePairState()
        option = {"loss_percent": 2, "daily_storage_cost": 0, "other_cost_krw": 0, "fee_percent": 5}
        small = pair.apply("new", pair.version, {
            "quantity": 3000, "price_basis": "gross", "start_date": "2026-11-17", "price_a": 2500,
            "option_a": {**option, "delay_days": 3}, "option_b": {**option, "delay_days": 10},
            "price_mode": "history",
            "history_filters": {"variety": "하우스감귤", "grade": "특", "size": "60내", "package": "상자", "package_kg": "3"}})
        self.assertEqual(small["past_reach"]["total"], 3)
        self.assertIn("사례가 3쌍뿐이라", _render_future_pair(small))
