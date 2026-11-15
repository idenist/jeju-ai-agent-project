"""STEP 1 structural acceptance: varied utterances -> the same safe state transition.

The fake model returns *semantic values*, not a model-generated reply.
This tests the Python contract, not real language-understanding accuracy.
"""
import json
import unittest
from types import SimpleNamespace

from agents.juneok.agent import AgentSession
from agents.juneok.dialogue_engine import route_turn, _FIELDS


def extraction(task="current_comparison", intent="provide", quotes=None, unknown_fields=None, **facts):
    value = {k: None for k in _FIELDS}
    value.update(facts)
    value.update(task=task, intent=intent, unknown_fields=unknown_fields or [],
                 evidence=[{"field": k, "quote": text} for k, text in (quotes or {}).items()])
    return value


class SemanticClient:
    def __init__(self, actions):
        self.actions = list(actions)
        self.calls = []
        self.responses = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.actions:
            raise AssertionError("Unexpected semantic model call")
        data = self.actions.pop(0)
        return SimpleNamespace(
            output=[SimpleNamespace(type="function_call", name="interpret_current_shipment_turn",
                                    arguments=json.dumps(data, ensure_ascii=False))],
            usage=SimpleNamespace(input_tokens=50, output_tokens=30))


class SemanticWorkflowTests(unittest.TestCase):
    def session(self, *actions):
        client = SemanticClient(actions)
        return AgentSession(client=client), client

    def test_different_utterances_have_same_state_transition_not_phrase_matching(self):
        examples = ["감귤 7일 후 출하가 좋을까요?", "일주일 묵혔다 파는 게 이득일지", "지금 출하할까, 딱 한 주 기다릴까?"]
        for phrase in examples:
            with self.subTest(text=phrase):
                # The semantic contract guarantees the Python behavior; real LLM
                # recognition of paraphrases must be verified separately.
                key = "7일" if "7일" in phrase else "일주일" if "일주일" in phrase else "한 주"
                agent, client = self.session(extraction(delay_days=7, quotes={"delay_days": key}))
                reply = route_turn(agent, phrase)
                self.assertEqual(agent.store.scenario["options"][0]["delay_days"], 7)
                self.assertIsNone(agent.store.scenario["price_basis"])
                self.assertIn("물량", reply["text"])
                self.assertEqual(reply["metrics"]["model_requests"], 1)
                self.assertEqual(client.calls[0]["tool_choice"]["name"], "interpret_current_shipment_turn")

    def test_multi_turn_unknown_clarify_and_shift_to_next_field(self):
        actions = [
            extraction(quantity_kg=3000, quotes={"quantity_kg": "3톤"}),
            extraction(current_price_per_kg=2275, quotes={"current_price_per_kg": "2275원"}),
            extraction(intent="unknown", unknown_fields=["price_basis"]),
            extraction(intent="other"),
            extraction(intent="clarify"),
            extraction(delay_days=5, quotes={"delay_days": "5일"}),
        ]
        agent, client = self.session(*actions)
        utterances = ["감귤 3톤 팔 때를 고민 중", "현재 2275원이요",
                      "수수료를 차감한 뒤인지 잘 모르겠어요", "오늘이요", "네?", "5일 더 보관할게요"]
        replies = [route_turn(agent, q) for q in utterances]
        self.assertEqual(len(client.calls), 6)
        self.assertEqual(agent.store.scenario["quantity_kg"], 3000)
        self.assertEqual(agent.store.scenario["current_price_per_kg"], 2275)
        self.assertIsNone(agent.store.scenario["price_basis"])
        self.assertEqual(agent.store.scenario["options"][0]["delay_days"], 5)
        self.assertIn("price_basis", agent._deferred_current_fields)
        self.assertTrue(all("과거 출하량부터" not in r["text"] for r in replies))
        self.assertIn("미확인", replies[2]["text"])
        self.assertIn("보관", replies[-1]["text"])

    def test_hallucinated_value_without_literal_user_evidence_cannot_change_state(self):
        agent, _ = self.session(extraction(quantity_kg=3000, quotes={"quantity_kg": "3톤"}))
        self.assertEqual(route_turn(agent, "감귤 언제 출하할까요?")["status"], "INVALID_INPUT")
        self.assertIsNone(agent.store.scenario["quantity_kg"])
        self.assertEqual(agent.store.version, 0)

    def test_change_of_storage_period_drops_old_loss(self):
        agent, _ = self.session(extraction(delay_days=7, quotes={"delay_days": "7일"}),
                                extraction(delay_days=14, quotes={"delay_days": "14일"}))
        route_turn(agent, "7일 뒤 출하할까?")
        stored = agent.store.scenario["options"][0]
        changed = agent.store.update({"options": [{**stored, "loss_percent": 3}]}, agent.store.version)
        self.assertEqual(changed["status"], "OK")
        route_turn(agent, "아니 14일 뒤로 정정")
        self.assertIsNone(agent.store.scenario["options"][0]["loss_percent"])
        self.assertEqual(agent.store.scenario["options"][0]["delay_days"], 14)

    def test_model_does_not_decide_calculated_amounts(self):
        agent, _ = self.session(extraction(quantity_kg=3000, current_price_per_kg=3830, price_basis="gross",
            quotes={"quantity_kg": "3톤", "current_price_per_kg": "3830원", "price_basis": "수수료 빼기 전"}))
        reply = route_turn(agent, "감귤 3톤, 3830원/kg 수수료 빼기 전인데 기다릴까요?")
        self.assertIn("11,490,000원", reply["text"])
        self.assertIn("차감 전", reply["text"])
        self.assertNotIn("순이익", reply["text"])
        self.assertEqual(agent.store.scenario["quantity_kg"], 3000)

    def test_unknown_basis_cannot_be_overwritten_by_conflicting_model_guess(self):
        agent, _ = self.session(
            extraction(intent="unknown", price_basis="commission_net",
                       unknown_fields=["price_basis"],
                       quotes={"price_basis": "수수료 뺀 뒤"}))
        agent.store.update({"quantity_kg": 3000, "current_price_per_kg": 2275}, agent.store.version)
        reply = route_turn(agent, "수수료 뺀 뒤인지 모르겠어요")
        self.assertIsNotNone(reply)
        self.assertIsNone(agent.store.scenario["price_basis"])
        self.assertIn("price_basis", agent._deferred_current_fields)

    def test_unrelated_workflow_is_not_intercepted(self):
        agent, _ = self.session(extraction(task="other", intent="other"))
        self.assertIn("확실히 분류하지 못했습니다", route_turn(agent, "정산 기록 검산해주세요")["text"])
        self.assertEqual(agent.store.version, 0)

if __name__ == "__main__":
    unittest.main()
