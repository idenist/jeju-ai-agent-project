"""Cross-entry acceptance checks, sharing the real store with a fake model transport."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from agents.juneok.agent import AgentSession
from agents.juneok.config import Settings
from agents.juneok.workspace import SharedWorkspace, card_values
from agents.juneok.ui_workflow import INPUT_CONTEXTS
from test_juneok_harness import FakeClient, call, response


class WorkspaceTests(unittest.TestCase):
    def workspace(self, responses=None):
        return SharedWorkspace(AgentSession(Settings(model="mock"), FakeClient(responses or [])))

    def draft(self, ws):
        values, ids = card_values(ws.store.snapshot())
        values["input_context"] = ws.input_context
        return values, ids

    def test_chat_card_chat_uses_same_store_and_preserves_arbitrary_option_ids(self):
        ws = self.workspace()
        ws.load_demo()
        option = deepcopy(ws.store.scenario["options"][0])
        option["id"] = "농가 직접 정한 보관안"
        ws.store.update({"options": [option]}, ws.store.version)
        values, ids = self.draft(ws)
        self.assertEqual(ids[0], "농가 직접 정한 보관안")
        self.assertTrue(values["period_0"])
        values["loss_0"] = "4"
        result = ws.apply_card(values, ws.store.version, ids)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(ws.current_entries()[0]["result"]["options"][0]["minimum_whole_krw_per_kg"], "3174")
        self.assertEqual(ws.store.scenario["options"][0]["id"], ids[0])
        ws.session.client = FakeClient([response(call("compare_shipment_options", {"expected_version": ws.store.version, "finish_turn": True}))])
        reply = ws.run("방금 카드에서 바꾼 조건으로 다시 확인해 줘")
        self.assertEqual(reply["status"], "OK")
        self.assertEqual(reply["visible_results"][0]["result"]["input"]["quantity_kg"], 3000)
        self.assertEqual(reply["visible_results"][0]["result"]["options"][0]["minimum_whole_krw_per_kg"], "3174")

    def test_old_card_version_is_rejected_and_unknown_is_explicitly_cleared(self):
        ws = self.workspace()
        ws.load_demo()
        values, ids = self.draft(ws)
        before = ws.store.version
        ws.store.update({"quantity_kg": 6000}, before)
        self.assertEqual(ws.apply_card(values, before, ids)["status"], "INVALID_INPUT")
        self.assertEqual(ws.store.scenario["quantity_kg"], 6000)
        values, ids = self.draft(ws)
        values["price"] = ""
        values["shipping_flexible"] = "아니오"
        self.assertEqual(ws.apply_card(values, ws.store.version, ids)["status"], "OK")
        self.assertIsNone(ws.store.scenario["current_price_per_kg"])
        self.assertIs(ws.store.scenario["shipping_flexible"], False)
        self.assertEqual(ws.current_entries()[0]["result"]["status"], "NEEDS_INPUT")

    def test_card_origin_is_direct_and_invalid_transaction_rolls_back(self):
        ws = self.workspace()
        ws.load_demo()
        ws.store.origin_turn_id = "previous-conversation"
        values, ids = self.draft(ws)
        values["loss_0"] = "4"
        self.assertEqual(ws.apply_card(values, ws.store.version, ids)["status"], "OK")
        self.assertEqual(ws.store.provenance["options.seven_days.loss_percent"]["source"], "direct_input")
        before = ws.store.snapshot()
        values, ids = self.draft(ws)
        values["current_fee"] = "5"  # Contradicts a commission-net quote.
        self.assertEqual(ws.apply_card(values, ws.store.version, ids)["status"], "INVALID_INPUT")
        self.assertEqual(ws.store.snapshot(), before)

    def test_period_change_loses_old_loss_until_new_period_is_asserted(self):
        ws = self.workspace()
        ws.load_demo()
        values, ids = self.draft(ws)
        values.update(days_0=3, period_0=False)
        self.assertEqual(ws.apply_card(values, ws.store.version, ids)["status"], "OK")
        self.assertIsNone(ws.store.scenario["options"][0]["loss_percent"])
        self.assertEqual(ws.current_entries()[0]["result"]["status"], "NEEDS_INPUT")
        values, ids = self.draft(ws)
        values.update(loss_0="2", period_0=True)
        self.assertEqual(ws.apply_card(values, ws.store.version, ids)["status"], "OK")
        self.assertEqual(ws.current_entries()[0]["result"]["status"], "OK")

    def test_grid_is_nonmutating_and_old_result_cannot_be_current(self):
        ws = self.workspace()
        ws.load_demo()
        before = ws.store.snapshot()
        result = ws.sensitivity("seven_days", [1, 2, 4], [3000, 3300])
        self.assertEqual(len(result["rows"]), 6)
        self.assertEqual(ws.store.snapshot(), before)
        ws.store.update({"quantity_kg": 4000}, ws.store.version)
        self.assertEqual(ws.current_entries(), [])

    def test_data_refresh_chat_clear_preserves_conditions_and_discards_exports(self):
        ws = self.workspace()
        ws.load_demo()
        before = ws.store.snapshot()
        ws.session.messages = ["old-price-evidence"]
        ws.clear_conversation(preserve_conditions=True)
        self.assertEqual(ws.store.snapshot(), before)
        self.assertEqual(ws.session.messages, [])
        self.assertEqual(ws.current_entries(), [])

    def test_api_failure_keeps_partial_results_with_explicit_failure_in_export(self):
        ws = self.workspace()
        ws.load_demo()
        result = ws.calculate()
        # Independent of provider error formatting: preserve verified output and failure together.
        ws._freeze([{"tool": "compare_shipment_options", "result": result}], "대화", execution_status="API_ERROR")
        from agents.juneok.reporting import render_report
        report = render_report(ws.current_entries())
        self.assertIn("3,109원/kg", report)
        self.assertIn("요청 전체가 완료된 것은 아닙니다", report)

    def test_external_data_change_hides_export_and_preserves_conditions(self):
        with patch("agents.juneok.workspace.data_revision", return_value=("old",)):
            ws = self.workspace()
            ws.load_demo()
            before = ws.store.snapshot()
        with patch("agents.juneok.workspace.data_revision", return_value=("new",)):
            self.assertEqual(ws.current_entries(), [])
            self.assertTrue(ws.check_data_change())
            self.assertEqual(ws.store.snapshot(), before)
            self.assertEqual(ws.current_entries(), [])

    def test_real_observations_are_not_labelled_as_fictional_demo(self):
        ws = self.workspace()
        ws.load_demo()
        ws.history({"shipment_variety": "노지온주", "target_month": 11, "start_year": 2018, "end_year": 2024})
        from agents.juneok.reporting import render_report
        report = render_report(ws.current_entries())
        self.assertIn("원출처 관측자료 조회", report)
        self.assertNotIn("가상 예시·사용자 가정", report)

    def test_unchanged_numeric_card_values_keep_types_and_provenance(self):
        ws = self.workspace()
        ws.load_demo()
        before = ws.store.snapshot()
        values, ids = self.draft(ws)
        values["loss_0"] = "4"
        ws.apply_card(values, ws.store.version, ids)
        self.assertEqual(ws.store.scenario["quantity_kg"], before["scenario"]["quantity_kg"])
        self.assertEqual(ws.store.provenance["quantity_kg"], before["provenance"]["quantity_kg"])
        self.assertEqual(ws.current_entries()[0]["result"]["ui_context"]["changed_fields"], ["대안 1 기간 전체 감모율(%)"])

    def test_equivalent_itemized_cost_order_keeps_provenance_but_real_edit_changes_it(self):
        ws = self.workspace()
        ws.load_demo()
        option = deepcopy(ws.store.scenario["options"][0])
        costs = [{"category": "transport", "amount_krw": 100}, {"category": "packaging", "amount_krw": 50}]
        option["costs"] = deepcopy(costs)
        ws.store.origin_turn_id = "conversation-X"
        ws.store.update({"cost_policy": "itemized", "current_costs": costs, "options": [option]}, ws.store.version)
        before = ws.store.snapshot()
        values, ids = self.draft(ws)
        values["loss_0"] = "4"
        ws.apply_card(values, ws.store.version, ids)
        self.assertEqual(ws.store.scenario["current_costs"], before["scenario"]["current_costs"])
        self.assertEqual(ws.store.provenance["current_costs"], before["provenance"]["current_costs"])
        self.assertEqual(ws.current_entries()[0]["result"]["ui_context"]["changed_fields"], ["대안 1 기간 전체 감모율(%)"])
        values, ids = self.draft(ws)
        values["current_packaging"] = "75"
        ws.apply_card(values, ws.store.version, ids)
        self.assertEqual(ws.store.provenance["current_costs"]["source"], "direct_input")

    def test_result_cannot_be_exported_if_data_changed_during_operation(self):
        ws = self.workspace()
        with patch("agents.juneok.workspace.data_revision", return_value=("new",)):
            ws._freeze([], "자료 조회", expected_data_revision=("old",))
            self.assertNotEqual(ws.result_data_revision, ("new",))
            self.assertEqual(ws.current_entries(), [])


if __name__ == "__main__":
    unittest.main()
