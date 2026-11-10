"""INT-04 실패의 과거 가격 조회 우선순위와 통칭 품종 처리 계약을 검증합니다.

문구 검증은 실제 모델의 도구 선택 성공률을 대신하지 않습니다.
"""
from __future__ import annotations

import unittest

from agents.common.price_history_service import get_integrated_price_history
from agents.juneok.agent import SYSTEM_PROMPT
from agents.juneok.tools import TOOLS


규격 = {"grade": "특", "size": "소", "package": "상자",
        "package_kg": 5, "interval_days": 7}


class 과거가격조회의도검수(unittest.TestCase):
    def test_통칭_감귤은_자료없음이_아닌_세부품종_확인(self):
        result = get_integrated_price_history({**규격, "variety": "감귤"})
        self.assertEqual(result["status"], "NEEDS_INPUT")
        self.assertEqual(result["source_status"], "NEEDS_CLARIFICATION")
        self.assertIn("variety", result["missing_fields"])
        self.assertFalse(result["is_future_forecast"])
        self.assertNotIn("examples", result)

    def test_명시적_과거조회와_품종후보_탐색의_우선순위(self):
        self.assertIn("가격쌍 조회를 명시적으로 요청하면", SYSTEM_PROMPT)
        self.assertIn("get_price_history가 우선", SYSTEM_PROMPT)
        self.assertIn("explore_data로 대신 조회해 자료가 없다고 단정하지 마세요", SYSTEM_PROMPT)
        self.assertIn("세부 품종을 확인하도록", SYSTEM_PROMPT)

    def test_가격조회_도구설명에도_누락조건_재확인_명시(self):
        description = next(tool["description"] for tool in TOOLS if tool["name"] == "get_price_history")
        self.assertIn("품종이 통칭이거나 일부 조건을 몰라도", description)
        self.assertIn("누락 조건을 확인", description)
        self.assertIn("NO_DATA라고 단정하지 않음", description)


if __name__ == "__main__":
    unittest.main()
