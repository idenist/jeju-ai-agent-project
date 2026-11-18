"""검수 의견 §1-1: 비용을 일부만 알아도 참고 손익분기가 계산돼야 한다."""
import unittest

from services.future_pair import compare_future_pair_threshold_reference

BASE = {"quantity": 3000, "price_basis": "gross", "start_date": "2026-11-17", "price_a": 3450}
A = {"delay_days": 3, "loss_percent": 5, "fee_percent": 10, "daily_storage_cost": None, "other_cost_krw": None}
B = {"delay_days": 10, "loss_percent": 7, "fee_percent": 30, "daily_storage_cost": None, "other_cost_krw": None}
SPEC = {"variety": "조생귤", "grade": "특", "size": "소", "package": "상자", "package_kg": "5"}


class PartialCostThresholdTest(unittest.TestCase):
    def reference(self, **a):
        return compare_future_pair_threshold_reference(option_a={**A, **a}, option_b=B, **BASE)

    def test_all_costs_unknown(self):
        self.assertEqual(self.reference()["minimum_whole_krw_per_kg"], "4532")

    def test_one_known_cost_no_longer_crashes(self):
        result = self.reference(daily_storage_cost=10000)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["minimum_whole_krw_per_kg"], "4516")
        self.assertEqual(result["option_a"]["daily_storage_cost"], "10000")
        self.assertNotIn("A안 하루 보관비", result["unknown_cost_fields"])

    def test_explicit_zero_differs_from_unknown(self):
        result = self.reference(daily_storage_cost=0)
        self.assertEqual(result["minimum_whole_krw_per_kg"], "4532")
        # 명시한 0원은 확인된 값이라 미확인 목록에서 빠지고, 미확인(None)은 목록에 남는다.
        self.assertEqual(result["option_a"]["daily_storage_cost"], "0")
        self.assertNotIn("A안 하루 보관비", result["unknown_cost_fields"])
        self.assertIn("A안 하루 보관비", self.reference()["unknown_cost_fields"])

    def test_decimal_cost_is_kept(self):
        self.assertEqual(self.reference(daily_storage_cost=10000.5)["option_a"]["daily_storage_cost"], "10000.5")

    def test_chat_state_with_and_without_past_prices(self):
        from agents.future_pair_state import FuturePairState

        for filters in (None, SPEC):
            for cost, expected in ((10000, "4516"), (0, "4532")):
                pair = FuturePairState()
                inputs = {**BASE, "option_a": {**A, "daily_storage_cost": cost}, "option_b": B}
                if filters:
                    inputs.update(price_mode="history", history_filters=filters)
                result = pair.apply("new", pair.version, inputs)
                self.assertEqual((result["status"], result["minimum_whole_krw_per_kg"]), ("OK", expected))


if __name__ == "__main__":
    unittest.main()
