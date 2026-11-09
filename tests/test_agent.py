"""LLM 네트워크 호출 없이 Tool Calling과 기존 분석 함수 연결을 검증한다."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from agents.agent import execute_tool, run_turn


DATA_FILE = Path(__file__).resolve().parent.parent / "data/processed/daily_prices.csv"


class FakeResponses:
    """실제 API 응답 형식의 최소 속성만 흉내 내는 테스트 객체."""

    def __init__(self, queue):
        self.queue = list(queue)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.queue.pop(0)


class FakeClient:
    def __init__(self, queue):
        self.responses = FakeResponses(queue)


class ToolCallingTests(unittest.TestCase):
    def test_market_price_from_real_csv(self):
        """기존 가공 CSV로 11월 3일 조생귤 가격을 조회한다."""
        result = execute_tool(
            "get_market_price",
            {"variety": "조생귤", "grade": "특", "size": "소", "package_kg": 5},
            DATA_FILE,
        )
        self.assertTrue(result["has_price"])
        self.assertEqual(result["observed_date"], "2026-11-03")
        self.assertEqual(result["observed_price_krw_per_kg"], "2,446.81")
        self.assertFalse(result["is_future_forecast"])

    def test_break_even_reuses_baseline(self):
        """경락가격 계산은 재사용하고 LLM이 수식을 계산하지 않는다."""
        result = execute_tool(
            "compare_shipping",
            {
                "variety": "조생귤", "grade": "특", "size": "소", "package_kg": 5,
                "quantity_kg": 3000, "delay_days": 7, "daily_storage_cost": 20000,
                "loss_percent": 2, "current_price_per_kg": None,
                "future_price_per_kg": None,
            },
            DATA_FILE,
        )
        self.assertEqual(result["minimum_integer_price_krw_per_kg"], "2,545")
        self.assertEqual(result["remaining_kg"], "2,940.00")
        self.assertEqual(result["required_increase_percent"], "3.99")
        self.assertIn("past_price_change", result["observed_market_price"])
        self.assertNotIn("future_hypothesis", result)

    def test_invalid_input_returns_tool_error(self):
        """보관 감량률 100%는 0으로 나누지 않고 오류로 반환한다."""
        result = execute_tool(
            "compare_shipping",
            {
                "variety": "조생귤", "grade": "특", "size": "소", "package_kg": 5,
                "quantity_kg": 3000, "delay_days": 7, "daily_storage_cost": 20000,
                "loss_percent": 100, "current_price_per_kg": None,
                "future_price_per_kg": None,
            },
            DATA_FILE,
        )
        self.assertIn("error", result)

    def test_responses_tool_call_round_trip(self):
        """LLM의 함수 호출 → Python 실행 → 결과 반환 흐름을 검증한다."""
        arguments = {
            "variety": "조생귤", "grade": "특", "size": "소", "package_kg": 5,
            "quantity_kg": 3000, "delay_days": 7, "daily_storage_cost": 20000,
            "loss_percent": 2, "current_price_per_kg": None,
            "future_price_per_kg": None,
        }
        first = SimpleNamespace(
            id="resp1", status="completed", output_text="",
            output=[SimpleNamespace(
                type="function_call", name="compare_shipping",
                call_id="call_abc", arguments=json.dumps(arguments),
            )],
        )
        second = SimpleNamespace(
            id="resp2", status="completed", output_text="손익분기 상승률은 약 3.99%입니다.",
            output=[],
        )
        client = FakeClient([first, second])
        reply, response_id = run_turn(client, "조생귤 출하를 7일 미루면?", prices_file=DATA_FILE)
        self.assertIn("3.99%", reply)
        self.assertEqual(response_id, "resp2")
        self.assertEqual(len(client.responses.calls), 2)
        tool_input = client.responses.calls[1]["input"][0]
        self.assertEqual(tool_input["type"], "function_call_output")
        self.assertEqual(tool_input["call_id"], "call_abc")
        self.assertEqual(client.responses.calls[1]["previous_response_id"], "resp1")
        tool_data = json.loads(tool_input["output"])
        self.assertEqual(tool_data["minimum_integer_price_krw_per_kg"], "2,545")

    def test_missing_values_can_be_asked_without_calling_tools(self):
        """질문이 불완전하면 모델이 추가 정보를 묻는 흐름도 지원한다."""
        first = SimpleNamespace(
            id="resp1", status="completed", output_text="등급과 크기, 보관비를 알려 주세요.",
            output=[],
        )
        client = FakeClient([first])
        reply, _ = run_turn(client, "감귤을 미룰까?", prices_file=DATA_FILE)
        self.assertIn("등급", reply)
        self.assertEqual(len(client.responses.calls), 1)


if __name__ == "__main__":
    unittest.main()
