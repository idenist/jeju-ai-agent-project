"""Regression: unknown commission basis never triggers unrelated data or a question loop."""
import unittest
from unittest.mock import patch

from agents.juneok.agent import AgentSession
from agents.juneok.presentation import render_dialogue, report_for
from agents.juneok.progressive_dialogue import current_input_followup, current_comparison_guidance
from agents.common.calculation_service import compare_shipment_options


class ModelMustNotRun:
    class Responses:
        def create(self, **_kwargs):
            raise AssertionError("An explicit uncertainty follow-up must not invoke the model")
    responses = Responses()


class CurrentUncertaintyTests(unittest.TestCase):
    def setUp(self):
        self.agent = AgentSession(client=ModelMustNotRun())
        self.agent.store.update({"quantity_kg": 3000, "current_price_per_kg": 2275}, 0)

    def test_unknown_basis_preserves_price_and_moves_to_date_question(self):
        response = self.agent.run("수수료를 뺀 뒤의 단가인지 잘 모르겠어요.")
        self.assertEqual(response["metrics"]["model_requests"], 0)
        self.assertIn("미확인으로 유지", response["text"])
        self.assertIn("며칠 더 보관", response["text"])
        self.assertNotIn("과거 출하량", response["text"])
        state = self.agent.store.scenario
        self.assertEqual(state["quantity_kg"], 3000)
        self.assertEqual(state["current_price_per_kg"], 2275)
        self.assertIsNone(state["price_basis"])
        self.assertEqual(state["options"], [])

    def test_today_and_confused_responses_do_not_repeat_basis_question(self):
        self.agent.run("수수료를 뺀 뒤의 단가인지 잘 모르겠어요.")
        for user in ("오늘이요", "네?"):
            reply = self.agent.run(user)
            self.assertIn("보관기간", reply["text"])
            self.assertNotIn("그 가격은", reply["text"])
            self.assertEqual(reply["metrics"]["model_requests"], 0)
            self.assertEqual(self.agent.store.version, 1)

    def test_bare_confusion_before_defer_explains_basis(self):
        response = self.agent.run("네?")
        self.assertIn("수수료", response["text"])
        self.assertIn("미확인", response["text"])
        self.assertEqual(response["metrics"]["model_requests"], 0)

    def test_period_answer_stored_without_guessing_fee_or_loss(self):
        self.agent.run("수수료를 뺀 뒤의 단가인지 잘 모르겠어요.")
        response = self.agent.run("7일")
        self.assertEqual(self.agent.store.scenario["options"][0]["delay_days"], 7)
        self.assertIsNone(self.agent.store.scenario["options"][0]["loss_percent"])
        self.assertIsNone(self.agent.store.scenario["price_basis"])
        self.assertNotIn("그 가격은", response["text"])
        self.assertIn("7일", response["text"])
        self.assertEqual(response["metrics"]["model_requests"], 0)
        self.assertEqual(response["visible_results"][0]["result"]["status"], "NEEDS_INPUT")

    def test_unknown_cost_does_not_switch_to_history_view(self):
        self.agent.store.update({"price_basis": "commission_net", "options": [{"id": "week", "delay_days": 7}]}, 1)
        response = self.agent.run("7일 동안 감모율을 모르겠어요")
        self.assertEqual(response["metrics"]["model_requests"], 0)
        self.assertNotIn("과거 출하량", response["text"])
        self.assertEqual(self.agent.store.scenario["options"][0].get("loss_percent"), None)
        self.assertIn("미확인", response["text"])

    def test_clearing_local_followups_keeps_numbers_unmodified(self):
        self.agent.run("수수료를 뺀 뒤의 단가인지 잘 모르겠어요.")
        self.assertIsNone(self.agent.store.scenario["price_basis"])
        result = compare_shipment_options(self.agent.store.scenario)
        self.assertIn("price_basis", result["missing_fields"])
        self.assertNotIn("수수료를 빼기 전 판매금액", self.agent.run("네?")["text"])

    def test_no_false_match_when_the_user_supplies_a_clear_basis(self):
        self.assertIsNone(current_input_followup(self.agent, "판매수수료를 뺀 뒤의 단가입니다", render_dialogue, report_for))
        self.assertIsNone(current_input_followup(self.agent, "과거 경락가격을 조회해 주세요", render_dialogue, report_for))


if __name__ == "__main__":
    unittest.main()
