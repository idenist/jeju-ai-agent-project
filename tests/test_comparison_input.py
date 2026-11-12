import unittest

from agents.jeonghyeon.comparison_input import build_comparison_input

# integration/agents의 agents/juneok/comparison.py에 정의된 대안 필드 (2026-11-10 기준)
TEAM_OPTION_FIELDS = {"id", "delay_days", "loss_percent", "daily_storage_cost", "fee_percent",
                      "future_price_per_kg", "costs", "loss_confirmed_for_period"}
ARGS = {"variety": "조생귤", "grade": "특등급", "size": "소과", "package": "상자", "package_kg": 5,
        "option_a_sale_date": "2026-11-13", "option_b_sale_date": "2026-11-20",
        "option_a_price_per_kg": 2500}
COSTS = {"delay_days": 5, "loss_percent": 2, "daily_storage_cost": 20000}


def build(args=ARGS, **costs):
    return build_comparison_input(args, **{**COSTS, **costs})


def option(result, key):
    return next(o for o in result["comparison_input"]["options"] if o["id"] == key)


class ComparisonInputTest(unittest.TestCase):
    def test_options_match_team_contract(self):
        result = build()
        options = result["comparison_input"]["options"]
        self.assertEqual([o["id"] for o in options], ["past_min_7d", "past_median_7d", "past_max_7d"])
        self.assertTrue(all(set(o) <= TEAM_OPTION_FIELDS for o in options))
        self.assertEqual(result["comparison_input"]["current_price_per_kg"], "2500")
        self.assertEqual(option(result, "past_median_7d")["future_price_per_kg"], "2409.37")

    def test_price_interval_and_storage_days_are_separate(self):
        result = build()
        median = option(result, "past_median_7d")
        self.assertEqual(median["delay_days"], 5)
        self.assertEqual(result["evidence"]["past_median_7d"]["price_interval_days"], 7)

    def test_evidence_is_kept_apart_from_calculation_input(self):
        result = build()
        evidence = result["evidence"]["past_median_7d"]
        self.assertNotIn("price_type", option(result, "past_median_7d"))
        self.assertEqual((evidence["price_type"], evidence["clean_pairs"]), ("assumption", 20))
        self.assertEqual(evidence["summary"], "B안 2,409.37원/kg · 동일 규격 7일 과거 20쌍의 중앙값 적용 · 가정")
        self.assertEqual(evidence["source"]["comparison_file"], "data/processed/price_comparison_availability.csv")
        self.assertEqual(result["evidence"]["current"]["price_type"], "assumption")

    def test_changing_only_b_date_recalculates_price(self):
        first = build()
        second = build({**ARGS, "option_b_sale_date": "2026-11-27"})
        self.assertEqual(second["comparison_input"]["current_price_per_kg"], "2500")
        self.assertEqual(second["evidence"]["past_median_14d"]["conditions"],
                         first["evidence"]["past_median_7d"]["conditions"])
        self.assertEqual(second["evidence"]["past_median_14d"]["clean_pairs"], 15)
        self.assertEqual(option(second, "past_median_14d")["future_price_per_kg"], "2621.76")

    def test_unsupported_gap_does_not_reuse_previous_price(self):
        build()
        result = build({**ARGS, "option_b_sale_date": "2026-11-23"})
        self.assertEqual(result["status"], "INTERVAL_NOT_AVAILABLE")
        self.assertIsNone(result["comparison_input"])
        self.assertTrue(result["manual_price_required"])
        self.assertIn("직접 입력", result["message"])

    def test_missing_a_price_asks_instead_of_building_options(self):
        result = build({k: v for k, v in ARGS.items() if k != "option_a_price_per_kg"})
        self.assertEqual(result["status"], "NEEDS_CLARIFICATION")
        self.assertEqual(result["missing_fields"], ["option_a_price_per_kg"])
        self.assertIsNone(result["comparison_input"])

    def test_unknown_costs_stay_empty(self):
        # 모르는 감량률·보관비를 0으로 채우지 않아야 팀 함수가 다시 묻는다.
        median = option(build(loss_percent=None, daily_storage_cost=None), "past_median_7d")
        self.assertIsNone(median["loss_percent"])
        self.assertIsNone(median["daily_storage_cost"])

    def test_bad_storage_days_are_input_errors(self):
        for days in (0, 366, True, 7.0):
            with self.subTest(days=days):
                self.assertEqual(build(delay_days=days)["error_kind"], "INVALID_INPUT")
        self.assertEqual(build(loss_percent=100)["error_kind"], "INVALID_INPUT")


if __name__ == "__main__":
    unittest.main()
