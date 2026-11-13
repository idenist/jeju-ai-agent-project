"""Independent monetary examples and atomic scenario invariants (no API calls)."""
from copy import deepcopy
from decimal import Decimal
import unittest

from agents.juneok.comparison import compare_shipment_options
from agents.juneok.state import ScenarioStore, demo_scenario, empty_scenario


class ComparisonTests(unittest.TestCase):
    def compare(self, scenario):
        result = compare_shipment_options(scenario)
        self.assertEqual(result["status"], "OK", result)
        return result

    def test_team_example_matches_independent_arithmetic(self):
        result = self.compare(demo_scenario())
        option = result["options"][0]
        self.assertEqual(Decimal(result["current_proceeds_krw"]), Decimal("9000000"))
        self.assertEqual(Decimal(option["remaining_kg"]), Decimal("2940"))
        self.assertEqual(Decimal(option["storage_cost_krw"]), Decimal("140000"))
        self.assertEqual(Decimal(option["break_even_price_per_kg"]), Decimal(9140000) / Decimal(2940))
        self.assertEqual(option["minimum_whole_krw_per_kg"], "3109")
        self.assertIsNone(option["hypothetical_proceeds_krw"])
        self.assertFalse(result["is_future_forecast"])
        self.assertIsNone(result["recommended_option"])

    def test_gross_prices_with_asymmetric_fees_and_costs(self):
        scenario = demo_scenario()
        scenario.update(quantity_kg=100, current_price_per_kg=200, price_basis="gross",
                        current_fee_percent=10, cost_policy="itemized", included_costs=[],
                        current_costs=[{"category": "transport", "amount_krw": 500}])
        scenario["options"][0].update(delay_days=5, loss_percent=10, daily_storage_cost=20,
                                      fee_percent=20, future_price_per_kg=250,
                                      costs=[{"category": "transport", "amount_krw": 200}])
        result = self.compare(scenario)
        option = result["options"][0]
        # Immediate: 100*200*.9-500=17,500. Delayed: 90*250*.8-300=17,700.
        self.assertEqual(Decimal(result["current_proceeds_krw"]), Decimal(17500))
        self.assertEqual(Decimal(option["hypothetical_proceeds_krw"]), Decimal(17700))
        self.assertEqual(Decimal(option["difference_krw"]), Decimal(200))
        self.assertEqual(Decimal(option["break_even_price_per_kg"]), Decimal(17800) / Decimal(72))
        self.assertEqual(option["minimum_whole_krw_per_kg"], "248")

    def test_commission_net_does_not_require_or_subtract_fees(self):
        scenario = demo_scenario()
        scenario["current_fee_percent"] = None
        scenario["options"][0]["fee_percent"] = None
        result = self.compare(scenario)
        self.assertEqual(Decimal(result["current_proceeds_krw"]), Decimal(9000000))

    def test_nonzero_fees_on_commission_net_prices_are_rejected(self):
        for target in ("current", "option"):
            with self.subTest(target=target):
                scenario = demo_scenario()
                if target == "current":
                    scenario["current_fee_percent"] = 5
                else:
                    scenario["options"][0]["fee_percent"] = 5
                self.assertEqual(compare_shipment_options(scenario)["status"], "INVALID_INPUT")

    def test_missing_price_basis_requests_input(self):
        scenario = demo_scenario()
        scenario["price_basis"] = None
        result = compare_shipment_options(scenario)
        self.assertEqual(result["status"], "NEEDS_INPUT")
        self.assertIn("price_basis", result["missing_fields"])

    def test_unknown_fee_is_not_zero_for_gross_prices(self):
        scenario = demo_scenario()
        scenario.update(price_basis="gross", included_costs=[], current_fee_percent=None)
        scenario["options"][0]["fee_percent"] = None
        result = compare_shipment_options(scenario)
        self.assertEqual(result["status"], "NEEDS_INPUT")
        self.assertIn("current_fee_percent", result["missing_fields"])
        self.assertIn("options.seven_days.fee_percent", result["missing_fields"])

    def test_zero_loss_and_storage_cost_are_known_valid_values(self):
        scenario = demo_scenario()
        scenario["options"][0].update(loss_percent=0, daily_storage_cost=0)
        option = self.compare(scenario)["options"][0]
        self.assertEqual(Decimal(option["break_even_price_per_kg"]), Decimal(3000))

    def test_unknown_loss_and_storage_cost_request_input(self):
        for field in ("loss_percent", "daily_storage_cost"):
            with self.subTest(field=field):
                scenario = demo_scenario()
                scenario["options"][0][field] = None
                result = compare_shipment_options(scenario)
                self.assertEqual(result["status"], "NEEDS_INPUT")
                self.assertIn(f"options.seven_days.{field}", result["missing_fields"])

    def test_itemized_unknown_costs_differ_from_explicit_empty_costs(self):
        scenario = demo_scenario()
        scenario["cost_policy"] = "itemized"
        missing = compare_shipment_options(scenario)
        self.assertEqual(missing["status"], "NEEDS_INPUT")
        self.assertIn("current_costs", missing["missing_fields"])
        self.assertIn("options.seven_days.costs", missing["missing_fields"])
        scenario["current_costs"] = []
        scenario["options"][0]["costs"] = []
        self.compare(scenario)

    def test_equal_excluded_assumption_is_visible_and_conflicting_cost_rejected(self):
        result = self.compare(demo_scenario())
        self.assertTrue(result["assumptions"])
        scenario = demo_scenario()
        scenario["current_costs"] = [{"category": "transport", "amount_krw": 1}]
        self.assertEqual(compare_shipment_options(scenario)["status"], "INVALID_INPUT")

    def test_costs_already_in_price_cannot_be_deducted_again(self):
        scenario = demo_scenario()
        scenario.update(cost_policy="itemized", included_costs=["commission", "transport"],
                        current_costs=[{"category": "transport", "amount_krw": 100}])
        scenario["options"][0]["costs"] = []
        self.assertEqual(compare_shipment_options(scenario)["status"], "INVALID_INPUT")

    def test_storage_cost_cannot_be_charged_twice(self):
        scenario = demo_scenario()
        scenario.update(cost_policy="itemized", current_costs=[])
        scenario["options"][0]["costs"] = [{"category": "storage", "amount_krw": 10}]
        self.assertEqual(compare_shipment_options(scenario)["status"], "INVALID_INPUT")

    def test_explicit_zero_future_price_is_a_scenario_not_missing(self):
        scenario = demo_scenario()
        scenario["options"][0]["future_price_per_kg"] = 0
        option = self.compare(scenario)["options"][0]
        self.assertEqual(Decimal(option["hypothetical_proceeds_krw"]), Decimal(-140000))
        self.assertEqual(Decimal(option["difference_krw"]), Decimal(-9140000))

    def test_negative_immediate_proceeds_give_zero_minimum_future_price(self):
        scenario = demo_scenario()
        scenario.update(quantity_kg=1, current_price_per_kg=100, cost_policy="itemized",
                        current_costs=[{"category": "transport", "amount_krw": 1000}])
        scenario["options"][0].update(delay_days=1, loss_percent=0, daily_storage_cost=10,
                                      costs=[], future_price_per_kg=0)
        result = self.compare(scenario)
        self.assertEqual(Decimal(result["current_proceeds_krw"]), Decimal(-900))
        self.assertEqual(result["options"][0]["minimum_whole_krw_per_kg"], "0")
        self.assertEqual(Decimal(result["options"][0]["difference_krw"]), Decimal(890))

    def test_greater_cost_or_loss_requires_greater_break_even_price(self):
        base = self.compare(demo_scenario())["options"][0]
        for change in ({"loss_percent": 4}, {"daily_storage_cost": 40000}):
            with self.subTest(change=change):
                scenario = demo_scenario()
                scenario["options"][0].update(change)
                modified = self.compare(scenario)["options"][0]
                self.assertGreater(Decimal(modified["break_even_price_per_kg"]),
                                   Decimal(base["break_even_price_per_kg"]))

    def test_multiple_options_use_each_periods_own_loss(self):
        scenario = demo_scenario()
        scenario["options"].append({**deepcopy(scenario["options"][0]), "id": "three_days",
                                     "delay_days": 3, "loss_percent": 0})
        result = self.compare(scenario)
        self.assertEqual(Decimal(result["options"][0]["remaining_kg"]), Decimal(2940))
        self.assertEqual(Decimal(result["options"][1]["remaining_kg"]), Decimal(3000))
        self.assertEqual(Decimal(result["options"][1]["storage_cost_krw"]), Decimal(60000))

    def test_feasibility_is_separate_from_economic_calculation(self):
        scenario = demo_scenario()
        self.assertEqual(self.compare(scenario)["feasibility"]["status"], "UNCONFIRMED")
        scenario.update(storage_available=False, shipping_flexible=True)
        result = self.compare(scenario)
        self.assertEqual(result["feasibility"]["status"], "CONFLICT")
        self.assertIn("storage_available", result["feasibility"]["conflicts"])
        scenario["storage_available"] = True
        self.assertEqual(self.compare(scenario)["feasibility"]["status"], "CONFIRMED_BY_USER")

    def test_invalid_numeric_inputs_are_rejected(self):
        cases = [("quantity_kg", 0), ("quantity_kg", True), ("current_price_per_kg", "NaN"),
                 ("current_price_per_kg", "Infinity"), ("current_fee_percent", 100),
                 ("loss_percent", -1), ("loss_percent", 100), ("loss_percent", "NaN"),
                 ("fee_percent", 100), ("daily_storage_cost", True),
                 ("daily_storage_cost", -1), ("future_price_per_kg", "Infinity"),
                 ("delay_days", True), ("delay_days", 1.5), ("delay_days", 0)]
        top = {"quantity_kg", "current_price_per_kg", "current_fee_percent"}
        for field, value in cases:
            with self.subTest(field=field, value=value):
                scenario = demo_scenario()
                (scenario if field in top else scenario["options"][0])[field] = value
                self.assertEqual(compare_shipment_options(scenario)["status"], "INVALID_INPUT")

    def test_duplicate_option_ids_and_excess_options_are_rejected(self):
        scenario = demo_scenario()
        scenario["options"].append(deepcopy(scenario["options"][0]))
        self.assertEqual(compare_shipment_options(scenario)["status"], "INVALID_INPUT")
        scenario["options"] = [{**deepcopy(scenario["options"][0]), "id": str(i)} for i in range(4)]
        self.assertEqual(compare_shipment_options(scenario)["status"], "INVALID_INPUT")

    def test_input_and_result_snapshots_do_not_alias(self):
        scenario = demo_scenario()
        before = deepcopy(scenario)
        result = self.compare(scenario)
        self.assertEqual(scenario, before)
        scenario["options"][0]["loss_percent"] = 90
        self.assertEqual(result["input"]["options"][0]["loss_percent"], 2)
        result["input"]["quantity_kg"] = 1
        self.assertEqual(scenario["quantity_kg"], 3000)


class ScenarioStoreTests(unittest.TestCase):
    def make_store(self):
        return ScenarioStore(scenario=demo_scenario())

    def test_empty_store_can_receive_partial_known_input(self):
        store = ScenarioStore()
        result = store.update({"quantity_kg": 3000}, expected_version=0)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["version"], 1)
        self.assertEqual(compare_shipment_options(store.scenario)["status"], "NEEDS_INPUT")

    def test_single_field_update_preserves_other_values_and_history(self):
        store = self.make_store()
        original = store.snapshot()
        result = store.update({"quantity_kg": 1000}, expected_version=0)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(store.version, 1)
        self.assertEqual(store.scenario["current_price_per_kg"], 3000)
        self.assertEqual(store.scenario["options"], original["scenario"]["options"])
        self.assertEqual(store.history, [original])
        self.assertEqual(result["changed_fields"], ["quantity_kg"])

    def test_stale_and_bool_versions_do_not_commit(self):
        for version in (-1, 1, True):
            with self.subTest(version=version):
                store = self.make_store()
                before = store.snapshot()
                self.assertEqual(store.update({"quantity_kg": 1}, version)["status"], "INVALID_INPUT")
                self.assertEqual(store.snapshot(), before)
                self.assertEqual(store.history, [])

    def test_invalid_patch_rolls_back_all_fields(self):
        store = self.make_store()
        before = store.snapshot()
        options = deepcopy(store.scenario["options"])
        options[0]["loss_percent"] = 100
        result = store.update({"quantity_kg": 1000, "options": options}, expected_version=0)
        self.assertEqual(result["status"], "INVALID_INPUT")
        self.assertEqual(store.snapshot(), before)
        self.assertEqual(store.history, [])

    def test_cross_field_contradiction_rolls_back(self):
        store = self.make_store()
        before = store.snapshot()
        result = store.update({"current_fee_percent": 5}, expected_version=0)
        self.assertEqual(result["status"], "INVALID_INPUT")
        self.assertEqual(store.snapshot(), before)
        self.assertEqual(store.history, [])

    def test_unknown_fields_do_not_commit(self):
        store = self.make_store()
        before = store.snapshot()
        result = store.update({"unknown_field": 1}, expected_version=0)
        self.assertEqual(result["status"], "INVALID_INPUT")
        self.assertEqual(store.snapshot(), before)

    def test_period_change_without_confirmation_invalidates_old_loss(self):
        store = self.make_store()
        options = deepcopy(store.scenario["options"])
        options[0].update(delay_days=3, loss_confirmed_for_period=False)
        result = store.update({"options": options}, expected_version=0)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["loss_reconfirmation_required"], ["seven_days"])
        self.assertIsNone(store.scenario["options"][0]["loss_percent"])
        missing = compare_shipment_options(store.scenario)
        self.assertEqual(missing["status"], "NEEDS_INPUT")
        self.assertIn("options.seven_days.loss_percent", missing["missing_fields"])

    def test_period_change_with_explicit_reconfirmation_keeps_loss(self):
        store = self.make_store()
        options = deepcopy(store.scenario["options"])
        options[0].update(delay_days=3, loss_percent=1, loss_confirmed_for_period=True)
        result = store.update({"options": options}, expected_version=0)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["loss_reconfirmation_required"], [])
        self.assertEqual(store.scenario["options"][0]["loss_percent"], 1)

    def test_patch_and_returned_snapshot_do_not_alias_stored_state(self):
        store = self.make_store()
        patch = {"options": deepcopy(store.scenario["options"])}
        patch["options"][0]["loss_percent"] = 3
        result = store.update(patch, expected_version=0)
        patch["options"][0]["loss_percent"] = 90
        result["scenario"]["options"][0]["loss_percent"] = 80
        self.assertEqual(store.scenario["options"][0]["loss_percent"], 3)
        self.assertEqual(store.history[0]["scenario"]["options"][0]["loss_percent"], 2)

    def test_constructor_does_not_alias_caller_scenario(self):
        initial = demo_scenario()
        store = ScenarioStore(scenario=initial)
        initial["options"][0]["loss_percent"] = 99
        initial["quantity_kg"] = 1
        self.assertEqual(store.scenario["quantity_kg"], 3000)
        self.assertEqual(store.scenario["options"][0]["loss_percent"], 2)


if __name__ == "__main__":
    unittest.main()
