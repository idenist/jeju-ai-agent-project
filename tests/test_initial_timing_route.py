"""Regression: generic 3-ton timing question collects a price, not a lecture."""
import unittest
from unittest.mock import patch

from agents.juneok.progressive_dialogue import first_timing_reply, current_comparison_guidance
from agents.juneok.agent import AgentSession
from agents.juneok.presentation import render_dialogue, report_for


class InitialTimingRouteTests(unittest.TestCase):
    def test_quantity_only_sets_state_and_asks_quote_without_model(self):
        class ModelMustNotRun:
            class Responses:
                def create(self, **_):
                    raise AssertionError("Initial quantity-only question must not call AI")
            responses = Responses()

        session = AgentSession(client=ModelMustNotRun())
        answer = session.run("감귤 3톤 언제 팔까요?")
        self.assertEqual(answer["status"], "OK")
        self.assertEqual(answer["metrics"]["model_requests"], 0)
        self.assertEqual(session.store.scenario["quantity_kg"], 3000)
        self.assertIsNone(session.store.scenario["current_price_per_kg"])
        self.assertIn("kg당 가격", answer["text"])
        self.assertNotIn("감귤 물량", answer["text"])
        self.assertNotIn("조건과 자료 이해하기", answer["text"])
        self.assertNotIn("최적 출하일", answer["text"])
        self.assertEqual(answer["visible_results"][0]["result"]["status"], "NEEDS_INPUT")

    def test_pristine_only_and_other_requests_not_swallowed(self):
        session = AgentSession()
        for prompt in ("감귤 3톤, 3,830원에 언제 팔까요?", "감귤 3톤 7일 뒤 팔까요?",
                       "감귤 3톤 가격 예측해 주세요", "감귤 3톤의 수수료율은 무엇인가요?"):
            self.assertIsNone(first_timing_reply(session, prompt, render_dialogue, report_for))
        session.store.update({"quantity_kg": 3000}, session.store.version)
        version = session.store.version
        repeated = first_timing_reply(session, "감귤 3톤 언제 팔까요?", render_dialogue, report_for)
        self.assertIsNotNone(repeated)
        self.assertEqual(session.store.version, version)
        self.assertIn("kg당 가격", repeated["text"])
        self.assertEqual(repeated["metrics"]["model_requests"], 0)

    def test_quantity_only_asks_just_missing_price(self):
        result = {"status": "NEEDS_INPUT", "input": {"quantity_kg": 3000,
                  "current_price_per_kg": None, "price_basis": None, "options": []},
                  "missing_fields": ["current_price_per_kg", "price_basis", "options"]}
        answer = current_comparison_guidance(result)
        self.assertIn("kg당 가격", answer)
        self.assertNotIn("감귤 물량", answer)

    def test_does_not_invent_price_or_earnings(self):
        session = AgentSession()
        answer = first_timing_reply(session, "감귤 3000kg 언제 출하할까요?", render_dialogue, report_for)
        self.assertIsNotNone(answer)
        self.assertNotIn("11,490,000", answer["text"])
        self.assertNotIn("최적", answer["text"])
        self.assertIsNone(session.store.scenario["price_basis"])
