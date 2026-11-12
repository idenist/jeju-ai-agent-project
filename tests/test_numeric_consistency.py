"""모델이 금액을 틀리게 적더라도 최종 출력은 Python 계산값을 사용하는지 확인한다."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from agents.agent import execute_tool, format_verified_result, run_turn


DATA_FILE = Path(__file__).resolve().parent.parent / "data/processed/daily_prices.csv"


def arguments(**overrides):
    """실제 조생귤 3톤 비교 사례의 도구 인수."""
    data = {
        "variety": "조생귤",
        "grade": "특",
        "size": "소",
        "package_kg": 5,
        "quantity_kg": 3000,
        "delay_days": 7,
        "daily_storage_cost": 20000,
        "loss_percent": 2,
        "current_price_per_kg": None,
        "future_price_per_kg": None,
    }
    data.update(overrides)
    return data


class FakeResponses:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0)


class FakeClient:
    def __init__(self, responses):
        self.responses = FakeResponses(responses)


def call_response(name, args, response_id="도구호출1"):
    return SimpleNamespace(
        id=response_id, status="completed", output_text="",
        output=[SimpleNamespace(
            type="function_call", name=name,
            call_id=f"call_{response_id}", arguments=json.dumps(args, ensure_ascii=False),
        )],
    )


def text_response(text, response_id="응답완료"):
    return SimpleNamespace(
        id=response_id, status="completed", output_text=text, output=[],
    )


class NumericConsistencyTests(unittest.TestCase):
    def test_report_uses_unrounded_python_total_not_llm_arithmetic(self):
        """LLM이 7,340,430원이라고 잘못 적어도 도구의 7,340,424원을 표시한다."""
        fake = FakeClient([
            call_response("compare_shipping", arguments()),
            text_response("경락가 2,446.81×3,000으로 7,340,430원입니다."),
        ])
        answer, response_id = run_turn(fake, "조생귤 3톤을 7일 보관하면?", prices_file=DATA_FILE)
        self.assertEqual(response_id, "응답완료")
        self.assertIn("7,340,424원", answer)
        self.assertNotIn("7,340,430원", answer)
        self.assertIn("2,544.36원/kg", answer)
        self.assertIn("2,545원/kg 이상", answer)
        self.assertIn("3.99%", answer)
        self.assertIn("2026-11-03", answer)
        self.assertIn("반올림 전 가격", answer)
        self.assertEqual(len(fake.responses.requests), 2)

    def test_price_only_does_not_use_hallucinated_llm_price(self):
        fake = FakeClient([
            call_response("get_market_price", {
                "variety": "조생귤", "grade": "특", "size": "소", "package_kg": 5,
            }),
            text_response("최근 경락가격은 9,999원/kg입니다."),
        ])
        answer, _ = run_turn(fake, "가격을 알려 줘", prices_file=DATA_FILE)
        self.assertIn("2,446.81원/kg", answer)
        self.assertNotIn("9,999원", answer)

    def test_future_hypothesis_uses_exact_tool_numbers(self):
        """사용자가 정한 가정가격은 예측으로 해석하지 않고 Python 결과만 표기한다."""
        result = execute_tool(
            "compare_shipping", arguments(future_price_per_kg=2800), DATA_FILE
        )
        answer = format_verified_result("compare_shipping", result)
        self.assertIn("2,800.00원/kg", answer)
        self.assertIn(result["future_hypothesis"]["difference_vs_now_krw"] + "원", answer)
        self.assertIn("예측 아님", answer)

    def test_user_input_price_not_labeled_market_price(self):
        result = execute_tool(
            "compare_shipping", arguments(current_price_per_kg=3000), DATA_FILE
        )
        answer = format_verified_result("compare_shipping", result)
        self.assertIn("사용자 입력가격 기준 즉시 출하 가정 매출: 9,000,000원", answer)
        self.assertNotIn("최근 관측가격 기준 즉시 출하 가정 매출", answer)

    def test_missing_information_preserves_model_followup(self):
        fake = FakeClient([text_response("보관기간과 감량률을 알려 주세요.")])
        answer, _ = run_turn(fake, "미뤄도 될까?", prices_file=DATA_FILE)
        self.assertEqual(answer, "보관기간과 감량률을 알려 주세요.")

    def test_tool_error_preserves_llm_error_message(self):
        fake = FakeClient([
            call_response("compare_shipping", arguments(loss_percent=100)),
            text_response("감량률은 100% 미만이어야 합니다."),
        ])
        answer, _ = run_turn(fake, "이상한 보관조건", prices_file=DATA_FILE)
        self.assertIn("100% 미만", answer)


if __name__ == "__main__":
    unittest.main()
