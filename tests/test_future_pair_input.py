import unittest

from agents.jeonghyeon.comparison_input import build_future_pair_input

# agents/juneok/comparison.py의 대안 필드 (2026-11-11 기준)
TEAM_OPTION_FIELDS = {"id", "delay_days", "loss_percent", "daily_storage_cost", "fee_percent",
                      "future_price_per_kg", "costs", "loss_confirmed_for_period"}
ARGS = {"variety": "조생귤", "grade": "특", "size": "소", "package": "상자", "package_kg": 5,
        "option_a_sale_date": "2026-11-13", "option_b_sale_date": "2026-11-20",
        "option_a_price_per_kg": 2500}
A = {"delay_days": 3, "loss_percent": 1, "daily_storage_cost": 20000}
B = {"delay_days": 10, "loss_percent": 2, "daily_storage_cost": 20000}


def build(args=ARGS, option_a=A, option_b=B):
    return build_future_pair_input(args, option_a=option_a, option_b=option_b)


class FuturePairInputTest(unittest.TestCase):
    def test_one_a_and_three_b_options_in_team_format(self):
        result = build()
        data = result["comparison_input"]
        self.assertEqual(result["mode"], "future_pair")
        self.assertEqual(data["option_a"], {"id": "option_a", "delay_days": 3, "loss_percent": "1",
                                            "daily_storage_cost": "20000", "future_price_per_kg": "2500"})
        self.assertEqual([o["id"] for o in data["options_b"]], ["past_min_7d", "past_median_7d", "past_max_7d"])
        # 팀 함수는 대안 최대 3개라 B 묶음만으로 한 번 호출할 수 있어야 한다.
        self.assertEqual(len(data["options_b"]), 3)
        self.assertTrue(all(set(o) <= TEAM_OPTION_FIELDS for o in [data["option_a"], *data["options_b"]]))

    def test_a_price_is_internal_reference_and_today_fields_are_hidden(self):
        result = build()
        self.assertEqual(result["comparison_input"]["current_price_per_kg"], "2500")
        self.assertIn("difference_krw", result["hidden_engine_fields"])
        self.assertIn("break_even_price_per_kg", result["hidden_engine_fields"])
        self.assertIn("내부 기준값", result["message"])

    def test_b_prices_and_storage_days_are_kept_apart(self):
        result = build()
        median = result["comparison_input"]["options_b"][1]
        self.assertEqual((median["future_price_per_kg"], median["delay_days"]), ("2409.37", 10))
        self.assertEqual(result["evidence"]["past_median_7d"]["price_interval_days"], 7)
        self.assertEqual(result["evidence"]["option_a"]["delay_days"], 3)

    def test_evidence_per_id(self):
        evidence = build()["evidence"]
        self.assertEqual(set(evidence), {"option_a", "past_min_7d", "past_median_7d", "past_max_7d"})
        self.assertEqual(evidence["option_a"]["summary"], "A안 2500원/kg · 사용자 입력 가정")
        self.assertTrue(all(v["price_type"] == "assumption" for v in evidence.values()))

    def test_missing_a_price_and_unsupported_gap_give_no_input(self):
        missing = build({k: v for k, v in ARGS.items() if k != "option_a_price_per_kg"})
        self.assertIsNone(missing["comparison_input"])
        self.assertEqual(missing["missing_fields"], ["option_a_price_per_kg"])
        gap = build({**ARGS, "option_b_sale_date": "2026-11-23"})
        self.assertIsNone(gap["comparison_input"])
        self.assertTrue(gap["manual_price_required"])

    def test_bad_storage_inputs(self):
        self.assertIn("A안 보관일", build(option_a={**A, "delay_days": 0})["error"])
        self.assertIn("A안보다 길어야", build(option_b={**B, "delay_days": 3})["error"])
        self.assertIn("감량률", build(option_b={**B, "loss_percent": 100})["error"])
        self.assertIn("받을 수 있습니다", build(option_a={**A, "price": 1})["error"])

    def test_unknown_costs_stay_none(self):
        data = build(option_a={"delay_days": 3})["comparison_input"]
        self.assertIsNone(data["option_a"]["loss_percent"])
        self.assertIsNone(data["option_a"]["daily_storage_cost"])
