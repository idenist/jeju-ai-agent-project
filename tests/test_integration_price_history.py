"""과거 가격 도구의 원천 검증·도구 호출·원본 상태·출력 경로를 검수합니다."""
from __future__ import annotations

import json
import unittest
from copy import deepcopy
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

from agents.common.price_history_reporting import render_price_history
from agents.common.price_history_service import (
    get_integrated_price_history, price_history_model_receipt,
)
from agents.juneok.agent import AgentSession, SYSTEM_PROMPT
from agents.juneok.config import Settings
from agents.juneok.pdf_report import render_pdf_report
from agents.juneok.presentation import render_dialogue
from agents.juneok.reporting import render_report
from agents.juneok.state import ScenarioStore, demo_scenario
from agents.juneok.tools import TOOLS, execute_tool
from agents.juneok.workspace import SharedWorkspace


조건 = {
    "variety": "조생귤", "grade": "특", "size": "소", "package": "상자",
    "package_kg": 5, "interval_days": 7, "limit": 3,
}
검증성공 = {
    "status": "OK", "full_content_verified": True,
    "comparison_source_sha256": "a" * 64, "selected_market_sha256": "a" * 64,
    "observed_first_date": "2026-10-01", "observed_last_date": "2026-11-03",
    "verified_comparison_rows": 2299, "verified_availability_rows": 4776,
}
정상응답 = {
    "status": "OK", "conditions": {"variety": "조생귤", "grade": "특", "size": "소", "package": "상자", "package_kg": "5", "market_code": "110001", "trade_type": "경매"},
    "interval_days": 7, "base_date_filter": None,
    "clean_pairs": 1, "warning_pairs": 2, "unavailable_pairs": 3,
    "warning_counts": {"LOW_SAMPLE": 2}, "unavailable_reasons": [
        {"code": "NO_MATCHING_CONDITION", "message": "해당 거래조건 없음", "count": 3},
    ],
    "clean_change_range_pct": {"min": "-25.855820", "max": "-25.855820"},
    "examples": [{"base_date": "2026-10-02", "target_date": "2026-10-09",
                  "base_price_kg": "3101.12", "target_price_kg": "2299.30",
                  "price_change_pct": "-25.855820"}],
    "source": "data/processed/price_comparison_availability.csv",
    "cautions": ["과거 관측값일 뿐 미래 가격 예측이 아닙니다."],
    "is_future_forecast": False,
}


class 가격조회통합검수(unittest.TestCase):
    def test_등록된_도구_스키마와_공통_상태_계약(self):
        from agents.common.result_contract import JUNEOK_TOOLS
        names = {tool["name"] for tool in TOOLS}
        self.assertEqual(names, JUNEOK_TOOLS)
        registered = next(tool for tool in TOOLS if tool["name"] == "get_price_history")
        self.assertTrue(registered["strict"])
        self.assertEqual(set(registered["parameters"]["required"]), set(registered["parameters"]["properties"]))
        self.assertIn("과거", SYSTEM_PROMPT)
        self.assertIn("get_price_history", SYSTEM_PROMPT)

    def test_실제_원천을_검증한_과거_가격_조회(self):
        # 이 사례만 실제 프로젝트의 동결 데이터와 재계산 결과를 검증합니다.
        before = deepcopy(조건)
        result = get_integrated_price_history(조건)
        self.assertEqual(result["status"], "OK", result.get("reason"))
        self.assertEqual(result["source_status"], "OK")
        self.assertGreater(result["clean_pairs"], 0)
        self.assertEqual(result["interval_days"], 7)
        self.assertEqual(result["source_verification"]["status"], "OK")
        self.assertTrue(result["source_verification"]["source_sha256"])
        self.assertFalse(result["is_future_forecast"])
        self.assertEqual(조건, before)

    def test_원천_검증_실패시_조회_차단(self):
        with patch("agents.common.price_history_service.verify_integrated_price_sources", return_value={
            "status": "DATA_ERROR", "reason": "VERSION_MISMATCH",
        }), patch("agents.common.price_history_service.execute_legacy_tool") as query:
            result = get_integrated_price_history(조건)
        query.assert_not_called()
        self.assertEqual(result["status"], "DATA_ERROR")
        self.assertEqual(result["reason"], "VERSION_MISMATCH")
        self.assertNotIn("examples", result)

    def test_조회_도중_원천_변경시_결과_폐기(self):
        with patch("agents.common.price_history_service.verify_integrated_price_sources", side_effect=[
            deepcopy(검증성공), {"status": "DATA_ERROR", "reason": "BASELINE_CHANGED"},
        ]), patch("agents.common.price_history_service.execute_legacy_tool", return_value=정상응답) as query:
            result = get_integrated_price_history(조건)
        query.assert_called_once()
        self.assertEqual(result["status"], "DATA_ERROR")
        self.assertNotIn("clean_pairs", result)
        self.assertNotIn("examples", result)

    def test_검증이_완료되지_않으면_성공_처리하지_않음(self):
        with patch("agents.common.price_history_service.verify_integrated_price_sources", return_value={
            "status": "OK", "full_content_verified": False,
        }), patch("agents.common.price_history_service.execute_legacy_tool") as query:
            result = get_integrated_price_history(조건)
        query.assert_not_called()
        self.assertEqual(result["status"], "DATA_ERROR")

    def test_정상_조회는_원래_품질정보를_유지(self):
        original = deepcopy(정상응답)
        with patch("agents.common.price_history_service.verify_integrated_price_sources", return_value=검증성공), \
             patch("agents.common.price_history_service.execute_legacy_tool", return_value=original):
            result = get_integrated_price_history(조건)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["source_status"], "OK")
        self.assertEqual(result["examples"], original["examples"])
        self.assertEqual(result["warning_pairs"], 2)
        self.assertIn("2개", " ".join(result["warnings"]))
        self.assertEqual(original, 정상응답)
        self.assertNotIn("2299.30", json.dumps(price_history_model_receipt(result)))
        self.assertNotIn("examples", price_history_model_receipt(result))

    def test_비교쌍_없음과_불확실_거래는_성공으로_꾸미지_않음(self):
        raw = {**deepcopy(정상응답), "status": "NO_CLEAN_COMPARISON", "clean_pairs": 0,
               "clean_change_range_pct": None, "examples": []}
        with patch("agents.common.price_history_service.verify_integrated_price_sources", return_value=검증성공), \
             patch("agents.common.price_history_service.execute_legacy_tool", return_value=raw):
            result = get_integrated_price_history(조건)
        self.assertEqual(result["status"], "NO_DATA")
        self.assertEqual(result["source_status"], "NO_CLEAN_COMPARISON")
        self.assertEqual(result["warning_pairs"], 2)
        self.assertIn("경고 없는 비교쌍이 없어", render_price_history(result))

    def test_조건_누락시_과거_가격_전용_질문을_표시(self):
        raw = {"status": "NEEDS_CLARIFICATION", "missing_fields": ["grade", "interval_days"],
               "choices": {"grade": ["특", "상"], "interval_days": [3, 7, 14]},
               "source": "data/processed/price_comparison_availability.csv"}
        with patch("agents.common.price_history_service.verify_integrated_price_sources", return_value=검증성공), \
             patch("agents.common.price_history_service.execute_legacy_tool", return_value=raw):
            result = get_integrated_price_history({"variety": "조생귤"})
        self.assertEqual(result["status"], "NEEDS_INPUT")
        text = render_dialogue([{"tool": "get_price_history", "result": result}])
        self.assertIn("비교 간격", text)
        self.assertIn("특", text)
        self.assertNotIn("감모", text)

    def test_간격이_미지원이면_가능한_간격을_표시(self):
        raw = {"status": "INTERVAL_NOT_AVAILABLE", "available_intervals": [3, 7, 14],
               "message": "요청한 간격의 자료가 없습니다."}
        with patch("agents.common.price_history_service.verify_integrated_price_sources", return_value=검증성공), \
             patch("agents.common.price_history_service.execute_legacy_tool", return_value=raw):
            result = get_integrated_price_history({"interval_days": 30})
        self.assertEqual(result["status"], "NO_DATA")
        self.assertIn("14일", render_price_history(result))

    def test_현재_시나리오를_바꾸지_않음(self):
        store = ScenarioStore(demo_scenario())
        before = store.snapshot()
        with patch("agents.common.price_history_service.verify_integrated_price_sources", return_value=검증성공), \
             patch("agents.common.price_history_service.execute_legacy_tool", return_value=정상응답):
            result = execute_tool("get_price_history", deepcopy(조건), store)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(store.snapshot(), before)

    def test_채팅_모델은_가격_원본이_아닌_상태만_전달받음(self):
        class FakeClient:
            def __init__(self):
                self.responses = self
                self.requests = []
            def create(self, **kwargs):
                self.requests.append(deepcopy(kwargs))
                call = SimpleNamespace(type="function_call", name="get_price_history",
                    arguments=json.dumps({**조건, "finish_turn": True}), call_id="sample")
                return SimpleNamespace(output=[call], output_text="미래가격은 99999999원", status="completed")
        client = FakeClient()
        session = AgentSession(Settings(model="mock-model"), client)
        session.store = ScenarioStore(demo_scenario())
        before = session.store.snapshot()
        with patch("agents.common.price_history_service.verify_integrated_price_sources", return_value=검증성공), \
             patch("agents.common.price_history_service.execute_legacy_tool", return_value=정상응답):
            reply = session.run("조생귤 특 소 상자 5kg의 7일 과거 가격을 비교해 주세요")
        self.assertEqual(reply["status"], "OK")
        self.assertEqual(len(client.requests), 1)
        self.assertIn("2,299.30", reply["text"])
        self.assertNotIn("99999999", reply["text"])
        self.assertEqual(session.store.snapshot(), before)
        receipt = next(item for item in session.messages if isinstance(item, dict) and item.get("type") == "function_call_output")
        self.assertNotIn("2299.30", receipt["output"])
        self.assertNotIn("3101.12", receipt["output"])

    def test_화면과_pdf에서_동일한_과거_가격을_표시(self):
        from pypdf import PdfReader
        with patch("agents.common.price_history_service.verify_integrated_price_sources", return_value=검증성공), \
             patch("agents.common.price_history_service.execute_legacy_tool", return_value=정상응답):
            result = get_integrated_price_history(조건)
        ws = SharedWorkspace(AgentSession())
        ws._freeze([{"tool": "get_price_history", "result": result}], "대화와 공유 조건")
        entries = ws.current_entries()
        self.assertEqual(entries[0]["result"]["ui_context"]["kind"], "evidence")
        report = render_report(entries)
        text = "".join(page.extract_text() for page in PdfReader(BytesIO(render_pdf_report(entries))).pages)
        self.assertIn("2,299.30", report)
        self.assertIn("2,299.30", text)
        self.assertIn("과거", report)
        self.assertNotIn("미래 가격을 예측했습니다", report)


if __name__ == "__main__":
    unittest.main()
