"""Cross-entry acceptance checks, sharing the real store with a fake model transport."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from agents.agent import AgentSession
from core.config import Settings
from ui.workspace import SharedWorkspace, card_values
from ui.ui_workflow import INPUT_CONTEXTS
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

    def test_plain_conversation_keeps_current_comparison_available_for_export(self):
        from services.casefiles import parse_case

        ws = self.workspace()
        ws.load_demo()
        before = parse_case(ws.export_case("출하 비교"))
        reply = ws.run("감귤과 관계없는 소설 써 줘")
        self.assertEqual(reply["metrics"]["dialogue_route"], "outside_service_scope")
        after = parse_case(ws.export_case("출하 비교"))
        self.assertEqual(after["archived_results"], before["archived_results"])
        self.assertEqual(after["snapshot"], before["snapshot"])
        self.assertEqual(after["archived_results"][0]["tool"], "compare_shipment_options")

        ws.store.update({"quantity_kg": 4200}, ws.store.version)
        ws.run("감귤과 관계없는 소설 써 줘")
        self.assertEqual(ws.current_entries(), [])
        self.assertEqual(parse_case(ws.export_case("출하 비교"))["archived_results"], [])

    def test_chat_comparison_report_expires_with_conditions_or_source(self):
        from ui.chat import _chat_report, _latest_chat_report

        with patch("ui.workspace.data_revision", return_value=("original",)):
            ws = self.workspace()
            ws.load_demo()
            report = _chat_report(ws, {"visible_results": ws.current_entries(), "state": ws.store.snapshot()})
        messages = [{"reply": {"chat_report": report}}]
        with patch("ui.chat.data_revision", return_value=("original",)):
            self.assertEqual(_latest_chat_report(messages, ws.store.version, None), (0, report))
            self.assertEqual(_latest_chat_report(messages, ws.store.version + 1, None), (None, None))
        with patch("ui.chat.data_revision", return_value=("updated",)):
            self.assertEqual(_latest_chat_report(messages, ws.store.version, None), (None, None))

    def test_current_comparison_can_be_reopened_after_a_tool_returns_another_result(self):
        from services.casefiles import parse_case
        from ui.chat import _current_comparison_report

        ws = self.workspace([response(call("explain_term", {"term": "loss_rate"}), text="")])
        ws.load_demo()
        before = _current_comparison_report(ws)
        reply = ws.run("감모율이 무슨 뜻인가요?")
        self.assertEqual(reply["visible_results"][0]["tool"], "explain_term")
        self.assertEqual(ws.current_entries()[0]["tool"], "explain_term")
        report = _current_comparison_report(ws)
        self.assertEqual(report["entries"], before["entries"])
        before_case = parse_case(before["json"])
        reopened_case = parse_case(report["json"])
        self.assertEqual(reopened_case["archived_results"], before_case["archived_results"])
        self.assertEqual(reopened_case["snapshot"], before_case["snapshot"])
        ws.store.update({"quantity_kg": 4200}, ws.store.version)
        self.assertIsNone(_current_comparison_report(ws))

    def test_report_request_keeps_economics_and_shipment_only_reports_distinct(self):
        from ui.chat import _REPORT_REQUEST, _chat_report, _current_comparison_report, _latest_chat_report
        from services.shipment_timing import analyze_shipment_timing

        ws = self.workspace()
        ws.load_demo()
        comparison = _current_comparison_report(ws)
        timing = analyze_shipment_timing({"shipment_variety": "노지온주", "option_a_date": "2026-11-23",
                                          "option_b_date": "2026-11-30"})
        shipment = _chat_report(ws, {"visible_results": [{"tool": "analyze_shipment_timing", "result": timing}]})
        messages = [{"reply": {"chat_report": item}} for item in (comparison, shipment)]
        args = (messages, ws.store.version, None, ws.session.future_pair.version)
        self.assertEqual(_latest_chat_report(*args, target="comparison"), (0, comparison))
        self.assertEqual(_latest_chat_report(*args, target="timing"), (1, shipment))
        self.assertEqual(_latest_chat_report(*args), (1, shipment))

        for phrase in ("방금 비교 PDF로 줘", "아까 출하 비교 보고서로 주세요", "비교 결과를 프린트할 수 있게 줘"):
            self.assertIn("비교", _REPORT_REQUEST.fullmatch(phrase)["target"])
        self.assertIn("출하량", _REPORT_REQUEST.fullmatch("과거 출하량 PDF로 줘")["target"])
        self.assertIsNone(_REPORT_REQUEST.fullmatch("11월 23일 과거 출하량 PDF로 줘"))

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
        from presenters.reporting import render_report
        report = render_report(ws.current_entries())
        self.assertIn("3,109원/kg", report)
        self.assertIn("요청 전체가 완료된 것은 아닙니다", report)
        from ui.chat import _current_comparison_report
        saved = _current_comparison_report(ws)
        self.assertEqual(saved["entries"], ws.current_entries())
        self.assertEqual(saved["entries"][-1]["result"]["reason"], "API_ERROR")

    def test_external_data_change_hides_export_and_preserves_conditions(self):
        with patch("ui.workspace.data_revision", return_value=("old",)):
            ws = self.workspace()
            ws.load_demo()
            before = ws.store.snapshot()
        with patch("ui.workspace.data_revision", return_value=("new",)):
            self.assertEqual(ws.current_entries(), [])
            self.assertTrue(ws.check_data_change())
            self.assertEqual(ws.store.snapshot(), before)
            self.assertEqual(ws.current_entries(), [])

    def test_real_observations_are_not_labelled_as_fictional_demo(self):
        ws = self.workspace()
        ws.load_demo()
        ws.history({"shipment_variety": "노지온주", "target_month": 11, "start_year": 2018, "end_year": 2024})
        from presenters.reporting import render_report
        report = render_report(ws.current_entries())
        self.assertIn("원출처 관측자료 조회", report)
        self.assertNotIn("가상 예시·사용자 가정", report)

    def test_no_shipment_data_suggests_a_lookup_without_inventing_an_amount(self):
        from presenters.presentation import render_dialogue

        ws = self.workspace()
        result = ws.history({"shipment_variety": "노지온주", "target_month": 11, "start_year": 2025, "end_year": 2025})
        self.assertEqual(result["status"], "NO_DATA")
        self.assertEqual(result["series"], [])
        answer = render_dialogue(ws.current_entries())
        self.assertIn("관측자료가 없습니다", answer)
        self.assertIn("노지온주의 다른 해 11월 출하량", answer)

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
        with patch("ui.workspace.data_revision", return_value=("new",)):
            ws._freeze([], "자료 조회", expected_data_revision=("old",))
            self.assertNotEqual(ws.result_data_revision, ("new",))
            self.assertEqual(ws.current_entries(), [])


if __name__ == "__main__":
    unittest.main()
