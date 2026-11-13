"""Regression tests for efficient completion, compound requests and canonical exports."""
from copy import deepcopy
import unittest

from test_juneok_harness import FakeClient, call, response
from agents.juneok.agent import AgentSession
from agents.juneok.config import Settings
from agents.juneok.state import ScenarioStore, demo_scenario
from agents.juneok.presentation import connection_label
from agents.juneok.quality import assess_turn


class WorkflowTests(unittest.TestCase):
    def session(self, responses):
        return AgentSession(Settings(model="mock"), FakeClient(responses))

    def test_complete_update_needs_one_request_and_deterministic_comparison(self):
        session = self.session([response(call("update_scenario", {"expected_version": 0,
            "patch": demo_scenario(), "finish_turn": True}))])
        turn = session.run("사용자 조건으로 비교")
        self.assertEqual(turn["status"], "OK")
        self.assertEqual(turn["metrics"]["model_requests"], 1)
        self.assertIn("3,109원/kg", turn["text"])
        self.assertNotIn("99999999", turn["report"])
        self.assertTrue(assess_turn(turn)["passed"])

    def test_incomplete_update_finishes_with_short_questions_and_no_invented_cost(self):
        turn = self.session([response(call("update_scenario", {"expected_version": 0,
            "patch": {"quantity_kg": 3000}, "finish_turn": True}))]).run("3톤 비교")
        self.assertIn("먼저 확인", turn["text"])
        self.assertLessEqual(turn["text"].count("?"), 3)
        self.assertIsNone(turn["state"]["scenario"]["current_price_per_kg"])
        self.assertNotIn("3,109", turn["text"])
        self.assertEqual(turn["metrics"]["model_requests"], 1)

    def test_invalid_finish_does_not_prevent_recovery(self):
        session = self.session([
            response(call("update_scenario", {"expected_version": 0, "patch": {"quantity_kg": -1}, "finish_turn": True})),
            response(call("update_scenario", {"expected_version": 0, "patch": demo_scenario(), "finish_turn": True}, 2))])
        turn = session.run("비교")
        self.assertEqual(turn["status"], "OK")
        self.assertEqual(turn["metrics"]["model_requests"], 2)
        self.assertIn("3,109", turn["text"])

    def test_compound_lookup_then_update_keeps_both_outcomes(self):
        session = self.session([
            response(call("get_historical_shipment_context", {"shipment_variety": "노지온주", "target_month": 11,
                "start_year": 2018, "end_year": 2024, "finish_turn": False})),
            response(call("update_scenario", {"expected_version": 0, "patch": demo_scenario(), "finish_turn": True}, 2))])
        turn = session.run("과거 출하자료를 조회하고 내 조건도 비교")
        self.assertEqual([x["tool"] for x in turn["visible_results"]], ["get_historical_shipment_context", "compare_shipment_options"])
        self.assertIn("43,781.08".replace(",", ""), turn["report"])
        self.assertIn("3,109", turn["report"])

    def test_export_excludes_stale_money_but_debug_trace_retains_it(self):
        session = self.session([
            response(call("compare_shipment_options", {"expected_version": 0})),
            response(call("update_scenario", {"expected_version": 0, "patch": {"current_price_per_kg": 4000}, "finish_turn": True}, 2))])
        session.store = ScenarioStore(demo_scenario())
        turn = session.run("가격 수정")
        for rendered in (turn["text"], turn["report"]):
            self.assertIn("12,000,000", rendered)
            self.assertNotIn("9,000,000", rendered)
        self.assertEqual(turn["tool_results"][0]["result"]["current_proceeds_krw"], "9000000")

    def test_quality_checker_rejects_report_mismatch_and_stale_comparison(self):
        session = self.session([response(call("update_scenario", {"expected_version": 0,
            "patch": demo_scenario(), "finish_turn": True}))])
        turn = session.run("비교")
        broken = deepcopy(turn)
        broken["report"] = "임의의 보고서"
        self.assertFalse(assess_turn(broken)["checks"]["report_matches_visible_results"])
        broken = deepcopy(turn)
        broken["state"]["version"] += 1
        self.assertFalse(assess_turn(broken)["checks"]["no_stale_comparison"])

    def test_provenance_preserves_unmodified_inputs_and_tracks_retraction(self):
        store = ScenarioStore()
        store.origin_turn_id = "first"
        store.update(demo_scenario(), 0)
        store.origin_turn_id = "second"
        options = deepcopy(store.scenario["options"])
        options[0]["delay_days"] = 3
        store.update({"options": options}, 1)
        self.assertEqual(store.provenance["quantity_kg"]["turn_id"], "first")
        self.assertEqual(store.provenance["options.seven_days.delay_days"]["turn_id"], "second")
        self.assertEqual(store.provenance["options.seven_days.loss_percent"]["source"], "period_change_requires_confirmation")
        snapshot = store.snapshot()
        snapshot["provenance"]["quantity_kg"]["turn_id"] = "tampered"
        self.assertEqual(store.provenance["quantity_kg"]["turn_id"], "first")

    def test_connection_status_requires_real_success_and_clears_on_key_absence(self):
        self.assertIn("실제 호출 전", connection_label(True, None))
        self.assertIn("호출 성공", connection_label(True, {"status": "OK", "metrics": {"model_requests": 1}}))
        self.assertIn("호출 실패", connection_label(True, {"status": "API_ERROR"}))
        self.assertIn("키 미설정", connection_label(False, {"status": "OK", "metrics": {"model_requests": 1}}))


if __name__ == "__main__":
    unittest.main()
