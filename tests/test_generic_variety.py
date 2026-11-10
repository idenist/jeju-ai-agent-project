import json
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

from agents.jeonghyeon.agent import PROPERTIES, SCENARIO_PROPERTIES, answer_scenario, run_turn, TOOL_NAME
from agents.jeonghyeon.price_history import DEFAULT_FILE, normalize_conditions, read_rows


class GenericVarietyTest(unittest.TestCase):
    def test_generic_name_is_not_used_as_variety(self):
        normalized, changes = normalize_conditions({"variety": "감귤"}, read_rows(DEFAULT_FILE))
        self.assertIsNone(normalized["variety"])
        self.assertEqual(changes["variety"], {"from": "감귤", "to": None})
        # 자료에 실제로 있는 품종명은 그대로 둔다.
        self.assertEqual(normalize_conditions({"variety": "감귤(일반)"}, read_rows(DEFAULT_FILE))[0]["variety"],
                         "감귤(일반)")

    def test_profit_question_with_generic_name_asks_variety(self):
        args = dict.fromkeys(SCENARIO_PROPERTIES)
        args.update(variety="감귤", days_after_option_a=7, asks_profit=True)
        reply = answer_scenario(args, today=date(2026, 11, 10))
        self.assertIn("품종 감귤 → 조건 확인 필요", reply)
        self.assertIn("품종 (예:", reply)

    def test_auto_applied_line_says_not_user_choice(self):
        args = dict.fromkeys(PROPERTIES)
        args.update(intent="history", variety="조생귤", grade="특", size="소", package="상자",
                    package_kg=5, interval_days=7)
        call = SimpleNamespace(type="function_call", name=TOOL_NAME, arguments=json.dumps(args, ensure_ascii=False))
        client = Mock()
        client.responses.create.return_value = SimpleNamespace(status="completed", output=[call])
        reply = run_turn(client, [], model="test")
        self.assertIn("자동 적용 기준(사용자 지정 아님", reply)
