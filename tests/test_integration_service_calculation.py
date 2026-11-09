"""통합 서비스가 단일 계산 엔진을 사용하고 기존 응답을 보존하는지 확인합니다."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from unittest import TestCase
from unittest.mock import patch

from agents.common.calculation_service import compare_shipment_options as service_calculate
from agents.juneok.comparison import compare_shipment_options as original_calculate
from agents.juneok.state import ScenarioStore, demo_scenario


class ServiceCalculationTests(TestCase):
    def test_same_example_matches_original_result(self):
        scenario = demo_scenario()
        self.assertEqual(service_calculate(scenario), original_calculate(scenario))
        result = service_calculate(scenario)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["options"][0]["minimum_whole_krw_per_kg"], "3109")

    def test_single_engine_call_for_one_analysis(self):
        with patch("agents.common.calculation_service._primary_calculate", wraps=original_calculate) as run:
            result = service_calculate(demo_scenario())
        self.assertEqual(run.call_count, 1)
        self.assertEqual(result["status"], "OK")

    def test_service_does_not_call_legacy_engine(self):
        with patch("agents.baseline.calculate_break_even", side_effect=AssertionError("중복 계산 금지")):
            self.assertEqual(service_calculate(demo_scenario())["status"], "OK")

    def test_service_keeps_original_scenario_unchanged(self):
        scenario = demo_scenario()
        previous = deepcopy(scenario)
        service_calculate(scenario)
        self.assertEqual(scenario, previous)

    def test_incomplete_values_keep_missing_fields(self):
        scenario = demo_scenario()
        scenario["current_price_per_kg"] = None
        result = service_calculate(scenario)
        self.assertEqual(result["status"], "NEEDS_INPUT")
        self.assertIn("current_price_per_kg", result["missing_fields"])

    def test_unknown_status_fails_closed(self):
        with patch("agents.common.calculation_service._primary_calculate", return_value={"status": "UNKNOWN"}):
            result = service_calculate(demo_scenario())
        self.assertEqual(result["status"], "DATA_ERROR")
        self.assertNotIn("options", result)

    def test_invalid_input_is_not_fabricated_as_success(self):
        scenario = demo_scenario()
        scenario["options"][0]["loss_percent"] = 100
        self.assertEqual(service_calculate(scenario)["status"], "INVALID_INPUT")

    def test_registered_service_uses_common_entrypoint(self):
        from agents.juneok.tools import execute_tool
        store = ScenarioStore(scenario=demo_scenario())
        with patch("agents.common.calculation_service._primary_calculate", wraps=original_calculate) as run:
            result = execute_tool("compare_shipment_options", {"expected_version": 0}, store)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["scenario_version"], 0)
        self.assertEqual(result["options"][0]["minimum_whole_krw_per_kg"], "3109")

    def test_related_calculation_modules_share_entrypoint(self):
        from agents.juneok import tools, briefing, insights, sensitivity, limits
        for module in (tools, briefing, insights, sensitivity, limits):
            with self.subTest(module=module.__name__):
                self.assertIs(module.compare_shipment_options, service_calculate)

    def test_baseline_stays_independent_for_verification(self):
        from agents.baseline import calculate_break_even
        scenario = demo_scenario()
        old = calculate_break_even(
            current_price_per_kg=Decimal("3000"), quantity_kg=Decimal("3000"),
            delay_days=7, daily_storage_cost=Decimal("20000"), loss_percent=Decimal("2"),
        )
        new = service_calculate(scenario)
        self.assertEqual(old.break_even_price_per_kg, Decimal(new["options"][0]["break_even_price_per_kg"]))
