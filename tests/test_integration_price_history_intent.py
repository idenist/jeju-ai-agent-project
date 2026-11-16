"""INT-04 실패의 과거 가격 조회 우선순위와 통칭 품종 처리 계약을 검증합니다.

문구 검증은 실제 모델의 도구 선택 성공률을 대신하지 않습니다.
"""
from __future__ import annotations

import unittest

from services.price_history_service import get_integrated_price_history


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

if __name__ == "__main__":
    unittest.main()
