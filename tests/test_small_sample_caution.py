import unittest

from services.price_scenario import CAUTIONS, MIN_CLEAN_PAIRS, execute_scenario_tool

BASE = {"option_a_sale_date": "2026-11-20", "option_b_sale_date": "2026-11-27",
        "option_a_price_per_kg": 2500}
SMALL = {"variety": "하우스감귤", "grade": "특", "size": "60내", "package": "상자", "package_kg": "3"}
LARGE = {"variety": "조생귤", "grade": "특", "size": "소", "package": "상자", "package_kg": "5"}


class SmallSampleCautionTest(unittest.TestCase):
    def test_threshold_is_team_agreed_value(self):
        self.assertEqual(MIN_CLEAN_PAIRS, 10)

    def test_few_pairs_get_caution_but_keep_prices(self):
        result = execute_scenario_tool({**SMALL, **BASE})
        self.assertEqual(result["status"], "OK")
        self.assertLess(result["clean_pairs"], MIN_CLEAN_PAIRS)
        self.assertTrue(result["small_sample"])
        self.assertIn(f"{result['clean_pairs']}쌍뿐", result["cautions"][0])
        # 안내만 붙이고 가정 단가는 그대로 만든다.
        self.assertTrue(all(s["option_b_price_per_kg"] for s in result["scenarios"]))

    def test_enough_pairs_keep_default_cautions(self):
        result = execute_scenario_tool({**LARGE, **BASE})
        self.assertEqual(result["clean_pairs"], 20)
        self.assertNotIn("small_sample", result)
        self.assertEqual(result["cautions"], CAUTIONS)

    def test_shared_cautions_list_is_not_mutated(self):
        before = list(CAUTIONS)
        execute_scenario_tool({**SMALL, **BASE})
        self.assertEqual(CAUTIONS, before)


if __name__ == "__main__":
    unittest.main()
