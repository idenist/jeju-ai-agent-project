"""기본 손익 계산과 확장 손익 계산의 공통 조건·비용 계약을 검증합니다."""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, ROUND_CEILING
import unittest

from agents.baseline import calculate_break_even
from agents.juneok.comparison import compare_shipment_options
from agents.juneok.state import ScenarioStore, demo_scenario


def number(value: object) -> Decimal:
    """테스트 기준값을 이진 부동소수 연산 없이 표현합니다."""
    return Decimal(str(value))


def common_scenario(quantity, price, days, cost, loss, future=None) -> dict:
    """두 엔진에 모두 존재하는 비용 없는 총액 기준의 동일 조건을 구성합니다."""
    scenario = demo_scenario()
    scenario.update(
        quantity_kg=str(quantity), current_price_per_kg=str(price),
        price_basis="gross", current_fee_percent=0,
        cost_policy="equal_excluded", included_costs=[], current_costs=[],
    )
    scenario["options"][0].update(
        delay_days=days, loss_percent=str(loss), daily_storage_cost=str(cost),
        fee_percent=0, future_price_per_kg=None if future is None else str(future),
        costs=[], loss_confirmed_for_period=True,
    )
    return scenario


class SharedCalculationTests(unittest.TestCase):
    """같은 경제적 가정에서만 두 엔진의 수치를 직접 대조합니다."""

    def check_case(self, quantity, price, days, cost, loss, future=None) -> None:
        q, p, c, r = map(number, (quantity, price, cost, loss))
        f = None if future is None else number(future)
        basic = calculate_break_even(
            current_price_per_kg=p, quantity_kg=q, delay_days=days,
            daily_storage_cost=c, loss_percent=r, future_price_per_kg=f,
        )
        extended = compare_shipment_options(common_scenario(quantity, price, days, cost, loss, future))
        self.assertEqual(extended["status"], "OK", extended)
        option = extended["options"][0]

        # 독립 기준식은 테스트 안에만 두며 운영용 계산 엔진을 추가하지 않습니다.
        reference_current = q * p
        reference_remaining = q * (number(100) - r) / number(100)
        reference_cost = number(days) * c
        reference_threshold = (reference_current + reference_cost) / reference_remaining
        expected = {
            "current_proceeds_krw": reference_current,
            "remaining_kg": reference_remaining,
            "storage_cost_krw": reference_cost,
            "break_even_price_per_kg": reference_threshold,
        }
        self.assertEqual(basic.current_gross, expected["current_proceeds_krw"])
        self.assertEqual(basic.remaining_kg, expected["remaining_kg"])
        self.assertEqual(basic.extra_storage_cost, expected["storage_cost_krw"])
        self.assertEqual(basic.break_even_price_per_kg, expected["break_even_price_per_kg"])
        self.assertEqual(number(extended["current_proceeds_krw"]), expected["current_proceeds_krw"])
        for key in ("remaining_kg", "storage_cost_krw", "break_even_price_per_kg"):
            self.assertEqual(number(option[key]), expected[key], key)
        self.assertEqual(
            number(option["minimum_whole_krw_per_kg"]),
            reference_threshold.to_integral_value(rounding=ROUND_CEILING),
        )
        if f is None:
            self.assertIsNone(basic.future_net_if_given)
            self.assertIsNone(basic.difference_if_given)
            self.assertIsNone(option["hypothetical_proceeds_krw"])
            self.assertIsNone(option["difference_krw"])
        else:
            expected_future = reference_remaining * f - reference_cost
            expected_difference = expected_future - reference_current
            self.assertEqual(basic.future_net_if_given, expected_future)
            self.assertEqual(basic.difference_if_given, expected_difference)
            self.assertEqual(number(option["hypothetical_proceeds_krw"]), expected_future)
            self.assertEqual(number(option["difference_krw"]), expected_difference)
        self.assertIs(extended["is_future_forecast"], False)
        self.assertIsNone(extended["recommended_option"])

    def test_team_example_matches_both_engines(self):
        self.check_case(3000, 3000, 7, 20000, 2)

    def test_future_price_is_only_a_user_assumption(self):
        self.check_case(3000, 3000, 7, 20000, 2, 3090)

    def test_explicit_zero_future_price_is_not_missing(self):
        self.check_case(100, 1000, 3, 20, 2, 0)

    def test_zero_loss_and_zero_storage_are_valid(self):
        self.check_case(100, 1000, 3, 0, 0)

    def test_decimal_quantity_and_cost_match(self):
        self.check_case("850.5", "2446.81", 14, "123.45", "2.5", "2500.25")

    def test_varied_conditions_match_without_fees(self):
        for days in (1, 3, 7, 14, 30, 365):
            for loss in (0, 1, 2, 10):
                with self.subTest(days=days, loss=loss):
                    self.check_case(1200, 2400, days, 1500, loss, 2600)

    def test_shared_invalid_inputs_are_rejected(self):
        cases = [
            (0, 1000, 7, 1000, 2), (100, 0, 7, 1000, 2),
            (100, 1000, 0, 1000, 2), (100, 1000, 7, -1, 2),
            (100, 1000, 7, 1000, 100), (100, 1000, 7, 1000, -1),
        ]
        for quantity, price, days, cost, loss in cases:
            with self.subTest(case=(quantity, price, days, cost, loss)):
                with self.assertRaises(ValueError):
                    calculate_break_even(
                        current_price_per_kg=number(price), quantity_kg=number(quantity),
                        delay_days=days, daily_storage_cost=number(cost), loss_percent=number(loss),
                    )
                result = compare_shipment_options(common_scenario(quantity, price, days, cost, loss))
                self.assertEqual(result["status"], "INVALID_INPUT", result)

    def test_extended_period_limit_is_not_silently_equated(self):
        scenario = common_scenario(100, 1000, 366, 100, 2)
        self.assertEqual(compare_shipment_options(scenario)["status"], "INVALID_INPUT")
        # 기본 엔진의 허용 범위가 더 넓어도 통합 UI에서는 1~365일을 사용합니다.
        self.assertGreater(
            calculate_break_even(
                current_price_per_kg=number(1000), quantity_kg=number(100),
                delay_days=366, daily_storage_cost=number(100), loss_percent=number(2),
            ).break_even_price_per_kg, 0,
        )


class ExtendedCalculationTests(unittest.TestCase):
    """기본 엔진에 없는 수수료·항목별 비용·입력 상태를 독립적으로 검증합니다."""

    def test_different_fees_and_costs_use_separate_formula(self):
        scenario = common_scenario(100, 200, 5, 20, 10, 250)
        scenario.update(price_basis="gross", current_fee_percent=10,
                        cost_policy="itemized", current_costs=[
                            {"category": "transport", "amount_krw": 500},
                        ])
        scenario["options"][0].update(fee_percent=20, costs=[
            {"category": "transport", "amount_krw": 200},
        ])
        result = compare_shipment_options(scenario)
        self.assertEqual(result["status"], "OK", result)
        option = result["options"][0]
        # 현재: 100×200×0.9-500 / 지연: 90×250×0.8-(100+200).
        current = number(100) * number(200) * number("0.9") - number(500)
        remaining = number(100) * number("0.9")
        costs = number(5) * number(20) + number(200)
        future = remaining * number(250) * number("0.8") - costs
        threshold = (current + costs) / (remaining * number("0.8"))
        self.assertEqual(number(result["current_proceeds_krw"]), current)
        self.assertEqual(number(option["hypothetical_proceeds_krw"]), future)
        self.assertEqual(number(option["difference_krw"]), future - current)
        self.assertEqual(number(option["break_even_price_per_kg"]), threshold)
        # 수수료·항목별 비용이 빠진 기본 엔진과 억지로 일치시키면 안 됩니다.
        basic = calculate_break_even(
            current_price_per_kg=number(200), quantity_kg=number(100),
            delay_days=5, daily_storage_cost=number(20), loss_percent=number(10),
            future_price_per_kg=number(250),
        )
        self.assertNotEqual(option["break_even_price_per_kg"], str(basic.break_even_price_per_kg))

    def test_commission_net_never_subtracts_fee_twice(self):
        scenario = demo_scenario()
        scenario["current_fee_percent"] = None
        scenario["options"][0]["fee_percent"] = None
        self.assertEqual(compare_shipment_options(scenario)["status"], "OK")
        for field in ("current_fee_percent", "fee_percent"):
            changed = deepcopy(scenario)
            if field == "current_fee_percent":
                changed[field] = 5
            else:
                changed["options"][0][field] = 5
            self.assertEqual(compare_shipment_options(changed)["status"], "INVALID_INPUT")

    def test_unknown_fee_is_not_assumed_zero(self):
        scenario = common_scenario(100, 1000, 7, 100, 2)
        scenario["current_fee_percent"] = None
        result = compare_shipment_options(scenario)
        self.assertEqual(result["status"], "NEEDS_INPUT")
        self.assertIn("current_fee_percent", result["missing_fields"])
        scenario["current_fee_percent"] = 0
        self.assertEqual(compare_shipment_options(scenario)["status"], "OK")

    def test_unknown_loss_and_storage_are_not_assumed_zero(self):
        for field in ("loss_percent", "daily_storage_cost"):
            with self.subTest(field=field):
                scenario = common_scenario(100, 1000, 7, 100, 2)
                scenario["options"][0][field] = None
                result = compare_shipment_options(scenario)
                self.assertEqual(result["status"], "NEEDS_INPUT")
                self.assertTrue(any(x.endswith(field) for x in result["missing_fields"]))
                scenario["options"][0][field] = 0
                self.assertEqual(compare_shipment_options(scenario)["status"], "OK")

    def test_itemized_costs_require_explicit_confirmation(self):
        scenario = common_scenario(100, 1000, 7, 100, 2)
        scenario["cost_policy"] = "itemized"
        scenario["current_costs"] = None
        scenario["options"][0]["costs"] = None
        result = compare_shipment_options(scenario)
        self.assertEqual(result["status"], "NEEDS_INPUT")
        self.assertIn("current_costs", result["missing_fields"])
        self.assertTrue(any(x.endswith(".costs") for x in result["missing_fields"]))
        scenario["current_costs"] = []
        scenario["options"][0]["costs"] = []
        self.assertEqual(compare_shipment_options(scenario)["status"], "OK")

    def test_cost_already_in_price_is_not_deducted_twice(self):
        scenario = common_scenario(100, 1000, 7, 100, 2)
        scenario.update(cost_policy="itemized", included_costs=["transport"],
                        current_costs=[{"category": "transport", "amount_krw": 100}])
        scenario["options"][0]["costs"] = []
        self.assertEqual(compare_shipment_options(scenario)["status"], "INVALID_INPUT")

    def test_storage_cost_cannot_be_listed_twice(self):
        scenario = common_scenario(100, 1000, 7, 100, 2)
        scenario.update(cost_policy="itemized", current_costs=[])
        scenario["options"][0]["costs"] = [{"category": "storage", "amount_krw": 100}]
        self.assertEqual(compare_shipment_options(scenario)["status"], "INVALID_INPUT")

    def test_period_change_requires_loss_reconfirmation(self):
        store = ScenarioStore(demo_scenario())
        original = store.scenario["options"][0]
        result = store.update({"options": [{**original, "delay_days": 14}]}, expected_version=0)
        self.assertEqual(result["status"], "OK", result)
        self.assertEqual(result["loss_reconfirmation_required"], ["seven_days"])
        self.assertIsNone(store.scenario["options"][0]["loss_percent"])
        needed = compare_shipment_options(store.scenario)
        self.assertEqual(needed["status"], "NEEDS_INPUT")
        self.assertIn("options.seven_days.loss_percent", needed["missing_fields"])

    def test_economic_result_does_not_override_infeasible_shipment(self):
        scenario = demo_scenario()
        scenario["storage_available"] = False
        scenario["shipping_flexible"] = False
        result = compare_shipment_options(scenario)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["feasibility"]["status"], "CONFLICT")
        self.assertIsNone(result["recommended_option"])
        self.assertIs(result["is_future_forecast"], False)

    def test_each_option_has_its_own_period_loss(self):
        scenario = demo_scenario()
        second = {**scenario["options"][0], "id": "fourteen_days", "delay_days": 14,
                  "loss_percent": 5, "daily_storage_cost": 20000}
        scenario["options"].append(second)
        result = compare_shipment_options(scenario)
        self.assertEqual(result["status"], "OK")
        first, other = result["options"]
        self.assertEqual(number(first["remaining_kg"]), number(3000) * number("0.98"))
        self.assertEqual(number(other["remaining_kg"]), number(3000) * number("0.95"))
        self.assertGreater(number(other["break_even_price_per_kg"]), number(first["break_even_price_per_kg"]))


if __name__ == "__main__":
    unittest.main()
