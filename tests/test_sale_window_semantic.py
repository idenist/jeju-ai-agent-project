"""Structural acceptance for a future-only shipment eligibility window.

Fake semantic outputs validate routing/state contracts, not live LLM accuracy.
"""
import unittest
from datetime import datetime, timedelta, timezone
from test_semantic_dialogue_engine import SemanticClient, extraction
from agents.juneok.agent import AgentSession
from agents.juneok.dialogue_engine import route_turn


class SaleWindowSemanticTests(unittest.TestCase):
    def test_future_only_constraint_and_followup_quote_keep_current_store_empty(self):
        day = (datetime.now(timezone(timedelta(hours=9))).date() + timedelta(days=7)).isoformat()
        text = f"{day} 이후에만 감귤을 팔 수 있어요. 언제 팔까요?"
        outputs = [
            extraction(task="sale_window", earliest_sale_date=day,
                       quotes={"earliest_sale_date": day}),
            extraction(task="sale_window", quantity_kg=3000, quoted_price_per_kg=3520,
                       quotes={"quantity_kg": "3000kg", "quoted_price_per_kg": "3520원"}),
            extraction(task="sale_window", intent="clarify"),
        ]
        agent = AgentSession(client=SemanticClient(outputs))
        first = route_turn(agent, text)
        second = route_turn(agent, "3000kg를 kg당 3520원에 팔건데요")
        third = route_turn(agent, "제 말이 이해가 안 되세요?")
        self.assertIn(day, first["text"])
        self.assertIn("3,000kg", second["text"])
        self.assertIn("3,520원", second["text"])
        self.assertIn("현재 출하할 수 없다면", third["text"])
        self.assertEqual(agent._sale_window_state.earliest_sale_date, day)
        self.assertIsNone(agent.store.scenario["current_price_per_kg"])
        self.assertIsNone(agent.store.scenario["quantity_kg"])
        self.assertEqual(agent.store.version, 0)

    def test_ambiguous_followup_remains_in_active_window(self):
        day = (datetime.now(timezone(timedelta(hours=9))).date() + timedelta(days=7)).isoformat()
        agent = AgentSession(client=SemanticClient([
            extraction(task="sale_window", earliest_sale_date=day, quotes={"earliest_sale_date": day}),
            extraction(task="other", intent="other"),
        ]))
        route_turn(agent, f"{day}부터만 출하 가능해요")
        result = route_turn(agent, "제 말이 이해가 안되나요?")
        self.assertEqual(result["status"], "OK")
        self.assertIn(day, result["text"])
        self.assertEqual(agent.store.version, 0)

    def test_explicit_specialist_task_still_delegates(self):
        day = (datetime.now(timezone(timedelta(hours=9))).date() + timedelta(days=7)).isoformat()
        agent = AgentSession(client=SemanticClient([
            extraction(task="sale_window", earliest_sale_date=day, quotes={"earliest_sale_date": day}),
            extraction(task="settlement", intent="provide"),
        ]))
        route_turn(agent, f"{day}부터 출하 가능")
        self.assertIsNone(route_turn(agent, "지난 정산 금액 검산해 주세요"))

    def test_invalid_window_is_atomic(self):
        day = datetime.now(timezone(timedelta(hours=9))).date()
        a = (day + timedelta(days=5)).isoformat()
        b = (day + timedelta(days=2)).isoformat()
        agent = AgentSession(client=SemanticClient([
            extraction(task="sale_window", earliest_sale_date=a, quotes={"earliest_sale_date": a}),
            extraction(task="sale_window", latest_sale_date=b, quantity_kg=3000,
                       quotes={"latest_sale_date": b, "quantity_kg": "3000kg"}),
        ]))
        route_turn(agent, a + " 이후에만 출하할 수 있어요")
        bad = route_turn(agent, b + "까지만, 물량은 3000kg")
        self.assertEqual(bad["status"], "INVALID_INPUT")
        self.assertIsNone(agent._sale_window_state.quantity_kg)
        self.assertIsNone(agent._sale_window_state.latest_sale_date)

    def test_model_failure_is_explicit_not_legacy_fallback(self):
        agent = AgentSession(client=SemanticClient([extraction(quantity_kg=3000, quotes={"quantity_kg": "3톤"})]))
        reply = route_turn(agent, "언제 감귤을 출하하면 될까요?")
        self.assertEqual(reply["status"], "INVALID_INPUT")
        self.assertEqual(agent.store.version, 0)

if __name__ == "__main__":
    unittest.main()
