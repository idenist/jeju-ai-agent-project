"""빠진 값이 여러 개일 때도 짧은 답("3000원")을 먼저 물은 항목에 코드가 직접 넣는다.

이전에는 빠진 값이 1개일 때만 코드가 받고, 나머지는 모델 해석에 맡겨 엉뚱한 답이 나올 수 있었다.
"""
import unittest

from agents.future_pair_state import FuturePairState


def fresh():
    pair = FuturePairState()
    pair.apply("new", 0, {"quantity": 3000, "start_date": "2026-11-16", "option_a": {"delay_days": 3},
                          "option_b": {"delay_days": 10}, "price_mode": "manual"})
    return pair


class ShortAnswerBindingTest(unittest.TestCase):
    def test_price_answer_binds_when_several_fields_are_missing(self):
        for answer in ("3000원", "3,000원/kg", "3000", "A안은 3000원"):
            with self.subTest(answer=answer):
                pair = fresh()
                self.assertGreater(len(pair.pending["missing_fields"]), 1)
                self.assertIsNotNone(pair.apply_short_answer(answer))
                self.assertEqual(pair.inputs["price_a"], "3000")

    def test_answer_for_another_missing_field_binds_only_when_unique(self):
        pair = fresh()
        self.assertIsNotNone(pair.apply_short_answer("수수료 차감 전이에요"))
        self.assertEqual(pair.inputs["price_basis"], "gross")
        self.assertIsNone(pair.inputs.get("price_a"))

    def test_ambiguous_or_tentative_answers_go_to_the_model(self):
        # A·B 감모율이 모두 빠져 있으면 "2%"가 어느 칸인지 알 수 없다.
        for answer in ("2%", "5%?", "3000원? 4000원?", "B안 3000원"):
            with self.subTest(answer=answer):
                pair = fresh()
                self.assertIsNone(pair.apply_short_answer(answer))
                self.assertIsNone(pair.inputs.get("price_a"))


if __name__ == "__main__":
    unittest.main()
