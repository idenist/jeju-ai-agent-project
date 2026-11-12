import unittest

from agents.juneok.comparison import compare_shipment_options
from agents.juneok.reporting import render_result, render_report
from agents.juneok.state import demo_scenario
from agents.juneok.ui_workflow import INPUT_CONTEXTS, condition_rows, form_snapshot, manual_scenario


class UiWorkflowTests(unittest.TestCase):
    def test_zero_false_unknown_and_future_assumption_are_distinct(self):
        scenario = demo_scenario()
        scenario["options"][0]["daily_storage_cost"] = 0
        scenario["options"][0]["future_price_per_kg"] = 3300
        scenario["shipping_flexible"] = False
        rows = {r["조건"]: r for r in condition_rows(scenario)}
        self.assertEqual(rows["seven_days · 하루 보관비"]["값"], "0원")
        self.assertEqual(rows["저장 가능"]["값"], "미확인")
        self.assertEqual(rows["출하일 변경 가능"]["값"], "아니오")
        self.assertIn("예측 아님", rows["seven_days · 미래 단가"]["구분"])
        self.assertIn("미검증", rows["현재 단가"]["구분"])
        fake = condition_rows(scenario, INPUT_CONTEXTS[2])
        self.assertEqual(fake[0]["구분"], INPUT_CONTEXTS[2])

    def test_unconfirmed_values_are_not_coerced_or_marked_real(self):
        values = {"option_count": 1, "included": [], "included_confirmed": True,
                  "quantity": " 3000 ", "price": "", "storage_0": "0", "loss_0": "2", "period_0": False}
        scenario = manual_scenario(values)
        self.assertEqual(scenario["quantity_kg"], "3000")
        self.assertIsNone(scenario["current_price_per_kg"])
        self.assertEqual(scenario["included_costs"], [])
        self.assertEqual(scenario["options"][0]["daily_storage_cost"], "0")
        self.assertIsNone(scenario["options"][0]["loss_percent"])
        self.assertNotIn("input_context", scenario)  # UI metadata must not become a calculation field

    def test_saved_form_is_independent_of_later_edits(self):
        values = {"option_count": 1, "included": ["수수료"], "loss_0": "2"}
        before = form_snapshot(values)
        values["included"].append("운송비")
        values["loss_0"] = "4"
        self.assertEqual(before["included"], ["수수료"])
        self.assertEqual(before["loss_0"], "2")
        self.assertNotEqual(before, form_snapshot(values))

    def test_report_keeps_revision_context_and_next_actions_from_screen(self):
        result = compare_shipment_options(demo_scenario())
        result["ui_context"] = {"version": 2, "input_context": INPUT_CONTEXTS[2], "changed_fields": ["감모율"]}
        text = render_result(result)
        self.assertIn("조건 버전 2", text)
        self.assertIn("가상 예시", text)
        self.assertIn("3,109원/kg", text)
        self.assertIn("다음 확인과 행동", text)
        self.assertIn("관계자에게 확인", text)
        report = render_report([{"tool": "compare_shipment_options", "result": result}])
        for value in ["조건 버전 2", "가상 예시", "3,109원/kg", "다음 확인과 행동", "관계자에게 확인"]:
            self.assertIn(value, report)

    def test_conflict_actions_do_not_suggest_execution(self):
        scenario = demo_scenario()
        scenario["shipping_flexible"] = False
        result = compare_shipment_options(scenario)
        result["ui_context"] = {"version": 1, "input_context": INPUT_CONTEXTS[0]}
        self.assertIn("제약을 먼저 확인", render_result(result))


if __name__ == "__main__":
    unittest.main()
