import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from agents.jeonghyeon.agent import PROPERTIES, TOOL_NAME, run_turn


class AgentTests(unittest.TestCase):
    conditions = {
        "intent": "history", "variety": "조생귤", "grade": "특", "size": "소",
        "package": "상자", "package_kg": 5, "interval_days": 7,
    }

    def turn(self, status="completed", **fields):
        args = dict.fromkeys(PROPERTIES)
        args.update(fields)
        call = SimpleNamespace(type="function_call", name=TOOL_NAME, arguments=json.dumps(args))
        client = Mock()
        client.responses.create.return_value = SimpleNamespace(status=status, output=[call])
        return run_turn(client, [{"role": "user", "content": "감귤 가격을 비교해 줘"}], model="test")

    def test_missing_conditions_and_readable_weight_choices(self):
        reply = self.turn(intent="history", variety="조생귤", interval_days=7)
        self.assertIn("다음 조건", reply)
        self.assertIn("포장 중량(kg) (예: 5, ", reply)
        self.assertNotIn("5.000", reply)

    def test_known_history_uses_verified_numbers(self):
        reply = self.turn(**self.conditions)
        self.assertIn("경고 없음 20쌍", reply)
        self.assertIn("-33.54% ~ +89.30%", reply)

    def test_unsupported_intent_does_not_show_prices(self):
        reply = self.turn(**{**self.conditions, "intent": "unsupported"})
        self.assertNotIn("20쌍", reply)
        self.assertIn("과거 감귤 거래 가격", reply)

    def test_unavailable_interval_shows_available_intervals(self):
        reply = self.turn(**{**self.conditions, "interval_days": 10})
        self.assertIn("3일, 7일, 14일", reply)

    def test_invalid_date_shows_input_error(self):
        self.assertIn("YYYY-MM-DD", self.turn(**self.conditions, base_date="11월 3일"))

    def test_incomplete_response_does_not_run_query(self):
        reply = self.turn(status="incomplete", **self.conditions)
        self.assertIn("다시 질문", reply)
        self.assertNotIn("20쌍", reply)
