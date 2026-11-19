from copy import deepcopy
from datetime import date
import unittest
from unittest.mock import patch

from agents.agent import AgentSession
from agents.future_pair_state import LABELS
from core.config import Settings
from test_integration_future_pair import COMMON, OPTION_B
from test_juneok_harness import FakeClient


class MoscowDecisionDialogueTests(unittest.TestCase):
    def session_waiting_for_b_price(self, **b_changes):
        client = FakeClient([])
        session = AgentSession(Settings(model="mock-model"), client)
        pair = session.future_pair
        pair.apply("new", 0, {
            **COMMON, "start_date": "2026-11-20", "price_mode": "manual",
            "option_b": {**OPTION_B, **b_changes},
        })
        pair.pending = pair.calculate(preview_history=True, allow_reference=False)
        pair.opening_field = LABELS["price_b"]
        self.addCleanup(lambda: self.assertEqual(len(client.requests), 0))
        return session

    def threshold(self, reply, minimum):
        self.assertEqual(reply["status"], "OK")
        self.assertEqual([entry["tool"] for entry in reply["visible_results"]], ["compare_future_pair"])
        result = reply["visible_results"][0]["result"]
        self.assertEqual(result["status"], "OK")
        self.assertTrue(result["threshold_only"])
        self.assertEqual(result["minimum_whole_krw_per_kg"], str(minimum))
        self.assertEqual(result["historical_price_lookup"], "not_used")
        self.assertIsNone(result["options_b"][0]["price_per_kg"])
        self.assertIsNone(result["options_b"][0]["difference_vs_a_krw"])
        for displayed in (reply["text"], reply["report"]):
            self.assertIn(f"{minimum:,}원/kg", displayed)
        return result

    def test_unknown_price_reply_or_break_even_request_uses_saved_conditions(self):
        for question in ("몰라요.", "B안 단가는 아직 몰라요. 얼마면 손해가 안 나요?",
                         "손익분기 가격이 얼마예요?"):
            with self.subTest(question=question):
                session = self.session_waiting_for_b_price()
                before = deepcopy(session.future_pair.inputs)
                reply = session.run(question)
                result = self.threshold(reply, 2660)
                self.assertEqual(result["unknown_cost_fields"], [])
                self.assertNotIn("미확인 비용 차이를 0원", reply["text"])
                self.assertEqual(result["option_a"]["sale_date"], "2026-11-23")
                self.assertEqual(result["option_a"]["price_per_kg"], "2500")
                self.assertEqual(session.future_pair.inputs, before)

    def test_missing_b_storage_yields_only_a_conditional_reference(self):
        session = self.session_waiting_for_b_price(daily_storage_cost=None)
        before = deepcopy(session.future_pair.inputs)
        reply = session.run("나중 출하 단가는 아직 모르겠어요. 얼마나 받아야 손해가 없나요?")
        result = self.threshold(reply, 2623)
        self.assertEqual(result["unknown_cost_fields"], ["B안 하루 보관비"])
        self.assertIsNone(result["options_b"][0]["daily_storage_cost"])
        self.assertIn("참고", reply["text"])
        self.assertIn("B안 하루 보관비", reply["report"])
        self.assertEqual(session.future_pair.inputs, before)

    def test_missing_loss_or_fee_asks_one_question_and_does_not_repeat_it(self):
        for changes, topic, other in (
            ({"loss_percent": None, "fee_percent": None}, r"감모|줄어", r"수수료"),
            ({"fee_percent": None}, r"수수료", r"감모|줄어"),
        ):
            with self.subTest(changes=changes):
                session = self.session_waiting_for_b_price(**changes)
                pair = session.future_pair
                before = deepcopy(pair.inputs)

                clarification = session.run("손익분기 단가를 알려주세요.")
                self.assertEqual(clarification["status"], "OK")
                self.assertEqual(clarification["text"].count("?"), 1)
                self.assertRegex(clarification["text"], topic)
                self.assertNotRegex(clarification["text"], other)
                self.assertIsNone(pair.current_result())

                follow_up = session.run("잘 모르겠어요.")
                self.assertEqual(follow_up["status"], "OK")
                self.assertNotRegex(follow_up["text"], rf"(?:{topic})[^.!?\n]*\?")
                self.assertIn("과거 출하량", follow_up["text"])
                self.assertIsNone(pair.current_result())
                self.assertEqual(pair.inputs, before)

    @patch("services.data_refresh.kst_today", return_value=date(2026, 11, 19))
    def test_explicit_shipment_volume_is_a_separate_lookup(self, _today):
        session = self.session_waiting_for_b_price()
        before = deepcopy(session.future_pair.inputs)
        threshold = self.threshold(session.run("몰라요."), 2660)

        reply = session.run("노지온주 11월 23일과 30일 주변 과거 출하량을 봐줘.")
        self.assertEqual([entry["tool"] for entry in reply["visible_results"]], ["analyze_shipment_timing"])
        result = reply["visible_results"][0]["result"]
        self.assertEqual(result["input"], {
            "shipment_variety": "노지온주", "option_a_date": "2026-11-23", "option_b_date": "2026-11-30",
        })
        self.assertEqual(session.future_pair.inputs, before)
        self.assertEqual(session.future_pair.current_result(), threshold)

    def test_later_date_edit_keeps_quote_deferred_despite_saved_history(self):
        from test_integration_future_pair import FILTERS

        session = self.session_waiting_for_b_price()
        pair = session.future_pair
        pair.inputs["history_filters"] = deepcopy(FILTERS)
        before = self.threshold(session.run("몰라요."), 2660)

        edit = session.run("B안만 17일로 바꿔줘")
        self.assertIsNone(pair.current_result())
        self.assertEqual(edit["text"].count("?"), 1)
        self.assertIn("17일 보관", edit["text"])
        self.assertNotIn("거래규격", edit["text"])

        after = self.threshold(session.run("7%예요."), 2773)
        self.assertEqual(after["option_a"], before["option_a"])
        self.assertEqual(after["options_b"][0]["sale_date"], "2026-12-07")
        self.assertEqual(pair.inputs["history_filters"], FILTERS)
