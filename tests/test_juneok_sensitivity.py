from copy import deepcopy
from decimal import Decimal
import unittest
from agents.juneok.state import ScenarioStore, demo_scenario
from agents.juneok.sensitivity import compare_sensitivity
from agents.juneok.tools import execute_tool
from agents.juneok.reporting import render_result


class SensitivityTests(unittest.TestCase):
    def test_independent_money_examples_and_original_state_unchanged(self):
        scenario = demo_scenario()
        original = deepcopy(scenario)
        result = compare_sensitivity(scenario, "seven_days", [1, 2, 4], [3000, 3300])
        self.assertEqual(result["status"], "OK")
        self.assertEqual(len(result["rows"]), 6)
        self.assertEqual([r["minimum_whole_krw_per_kg"] for r in result["rows"]], ["3078", "3078", "3109", "3109", "3174", "3174"])
        self.assertEqual([Decimal(r["difference_krw"]) for r in result["rows"]], [-230000, 661000, -320000, 562000, -500000, 364000])
        self.assertEqual(scenario, original)
        self.assertFalse(result["is_future_forecast"])

    def test_missing_future_prices_only_calculates_thresholds(self):
        result = compare_sensitivity(demo_scenario(), "seven_days", [2, 4], None)
        self.assertEqual([r["minimum_whole_krw_per_kg"] for r in result["rows"]], ["3109", "3174"])
        self.assertTrue(all(r["difference_krw"] is None for r in result["rows"]))

    def test_missing_base_loss_is_not_silently_assumed_or_saved(self):
        scenario = demo_scenario()
        scenario["options"][0]["loss_percent"] = None
        self.assertEqual(compare_sensitivity(scenario, "seven_days", [2], None)["status"], "OK")
        self.assertIsNone(scenario["options"][0]["loss_percent"])
        self.assertEqual(compare_sensitivity(scenario, "seven_days", None, None)["status"], "NEEDS_INPUT")

    def test_invalid_and_excessive_hypotheses_are_rejected(self):
        for losses, prices in [([], None), ([2, 2], None), ([100], None), ([-1], None), ([True], None),
                               (["NaN"], None), (list(range(8)), None), ([2], []), ([2], [-1]), ([2], ["Infinity"])]:
            with self.subTest(losses=losses, prices=prices):
                self.assertEqual(compare_sensitivity(demo_scenario(), "seven_days", losses, prices)["status"], "INVALID_INPUT")

    def test_differential_fees_and_costs_are_retained(self):
        scenario = demo_scenario()
        scenario.update(quantity_kg=1000, current_price_per_kg=2000, price_basis="gross", included_costs=[],
                        current_fee_percent=5, cost_policy="itemized", current_costs=[{"category": "transport", "amount_krw": 10000}])
        scenario["options"][0].update(delay_days=3, daily_storage_cost=1000, fee_percent=4,
                                     costs=[{"category": "transport", "amount_krw": 20000}])
        result = compare_sensitivity(scenario, "seven_days", [1], [2100])
        self.assertEqual(result["rows"][0]["minimum_whole_krw_per_kg"], "2013")
        self.assertEqual(Decimal(result["rows"][0]["difference_krw"]), 82840)

    def test_version_and_missing_constraints_are_preserved(self):
        store = ScenarioStore(demo_scenario())
        result = execute_tool("compare_sensitivity", {"expected_version": 0, "option_id": "seven_days", "loss_values": [2], "price_values": None}, store)
        self.assertEqual(result["scenario_version"], 0)
        self.assertEqual(result["feasibility"]["status"], "UNCONFIRMED")
        self.assertEqual(store.version, 0)
        self.assertEqual(execute_tool("compare_sensitivity", {"expected_version": 1}, store)["status"], "INVALID_INPUT")
        text = render_result(result)
        self.assertIn("3,109원/kg", text)
        self.assertIn("사용자가 정한 가정", text)


if __name__ == "__main__":
    unittest.main()
