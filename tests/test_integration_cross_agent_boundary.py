"""정현님 과거 경락가격과 준억님 손익 계산의 통합 경계를 검증합니다.

실제 동결 자료와 모의 Responses 도구 호출을 사용합니다. 유료 API를 호출하지 않습니다.
"""
from __future__ import annotations

import json
import unittest
from copy import deepcopy
from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

from agents.common.data_contract import verify_integrated_price_sources
from agents.common.price_history_service import get_integrated_price_history
from agents.common.result_contract import JUNEOK_TOOLS
from agents.jeonghyeon.comparison_input import build_comparison_input
from agents.jeonghyeon.price_history import execute_tool as 기존_조회
from agents.jeonghyeon.price_scenario import content_sha256, get_price_scenarios
from agents.juneok.agent import AgentSession
from agents.juneok.config import PROJECT_ROOT, Settings
from agents.juneok.pdf_report import render_pdf_report
from agents.juneok.reporting import render_report
from agents.juneok.state import ScenarioStore, demo_scenario
from agents.juneok.tools import TOOLS, execute_tool
from agents.juneok.workspace import SharedWorkspace


조회조건 = {"variety": "조생귤", "grade": "특", "size": "소", "package": "상자",
          "package_kg": 5, "interval_days": 7, "limit": 3}
판매가정 = {"variety": "조생귤", "grade": "특", "size": "소", "package": "상자",
          "package_kg": 5, "option_a_sale_date": "2026-11-13",
          "option_b_sale_date": "2026-11-20", "option_a_price_per_kg": 2500}


def 도구_호출(name, args, number):
    return SimpleNamespace(type="function_call", name=name,
                           arguments=json.dumps({**args, "finish_turn": True}, ensure_ascii=False),
                           call_id=f"통합검수_{number}")


class 모의응답클라이언트:
    def __init__(self, calls):
        self.responses = self
        self.items = iter([SimpleNamespace(output=[도구_호출(name, args, i)],
                                           status="completed", output_text="미래가격 99999999원")
                           for i, (name, args) in enumerate(calls, 1)])
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(deepcopy(kwargs))
        return next(self.items)


class 통합경계검수(unittest.TestCase):
    def test_정현_최신_가정도구는_기존_에이전트에_자동등록되지_않음(self):
        names = {item["name"] for item in TOOLS}
        self.assertEqual(names, JUNEOK_TOOLS)
        self.assertIn("get_price_history", names)
        self.assertNotIn("get_price_scenarios", names)
        store = ScenarioStore(demo_scenario())
        before = store.snapshot()
        blocked = execute_tool("get_price_scenarios", {}, store)
        self.assertEqual(blocked["status"], "INVALID_INPUT")
        self.assertEqual(store.snapshot(), before)

    def test_신규_가격비교_보고서해시와_공통검증이_함께_통과(self):
        report = json.loads((PROJECT_ROOT / "data/reports/price_comparison_report.json").read_text(encoding="utf-8"))
        expected = report["output_sha256"]["price_comparison_availability.csv"]
        observed = content_sha256((PROJECT_ROOT / "data/processed/price_comparison_availability.csv").read_bytes())
        self.assertEqual(observed, expected)
        status = verify_integrated_price_sources(PROJECT_ROOT)
        self.assertEqual(status["status"], "OK", status.get("reason"))
        self.assertTrue(status["full_content_verified"])
        self.assertEqual(status["verified_availability_rows"], 4776)

    def test_7일과_14일_실제_경락가격쌍만_조회(self):
        seven = get_integrated_price_history(조회조건)
        fourteen = get_integrated_price_history({**조회조건, "interval_days": 14})
        self.assertEqual((seven["status"], fourteen["status"]), ("OK", "OK"))
        self.assertEqual((seven["clean_pairs"], fourteen["clean_pairs"]), (20, 15))
        self.assertEqual(seven["source_verification"]["status"], "OK")
        self.assertEqual(fourteen["source_verification"]["status"], "OK")
        self.assertFalse(seven["is_future_forecast"])
        self.assertFalse(fourteen["is_future_forecast"])
        self.assertNotIn("future_price_per_kg", seven)

    def test_통칭_품종은_정확한_품종을_다시_질문(self):
        result = get_integrated_price_history({**조회조건, "variety": "감귤"})
        self.assertEqual(result["status"], "NEEDS_INPUT")
        self.assertIn("variety", result["missing_fields"])
        self.assertEqual(result["normalized_conditions"]["variety"]["to"], None)

    def test_포장문구와_중량_충돌은_자동선택하지_않음(self):
        result = 기존_조회("get_price_history", {**조회조건, "package": "10kg 박스"})
        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(result["error_kind"], "INVALID_INPUT")
        self.assertIn("10kg", result["error"])
        self.assertIn("5kg", result["error"])

    def test_과거조회_7일에서_14일로_변경후_별도_손익계산(self):
        ws = SharedWorkspace(AgentSession(Settings(model="mock-model")))
        ws.load_demo()
        before = ws.store.snapshot()
        client = 모의응답클라이언트([
            ("get_price_history", 조회조건),
            ("get_price_history", {"follow_up": True, "interval_days": 14}),
            ("compare_shipment_options", {"expected_version": ws.store.version}),
        ])
        ws.session.client = client
        seven = ws.run("조생귤 특 소 상자 5kg의 과거 7일 경락가격을 보여줘")
        fourteen = ws.run("같은 조건으로 과거 비교 간격만 14일로 바꿔줘")
        comparison = ws.run("이제 저장된 7일 보관 손익분기 단가만 계산해줘")
        self.assertEqual([seven["status"], fourteen["status"], comparison["status"]], ["OK"] * 3)
        self.assertEqual(seven["visible_results"][0]["result"]["clean_pairs"], 20)
        self.assertEqual(fourteen["visible_results"][0]["result"]["clean_pairs"], 15)
        self.assertEqual(comparison["visible_results"][0]["result"]["options"][0]["minimum_whole_krw_per_kg"], "3109")
        self.assertEqual(ws.session.price_history_query["interval_days"], 14)
        self.assertEqual(ws.store.snapshot(), before)
        self.assertEqual(ws.store.scenario["options"][0]["delay_days"], 7)
        self.assertIsNone(ws.store.scenario["options"][0]["future_price_per_kg"])
        self.assertEqual(len(client.requests), 3)
        for request in client.requests:
            serialized = json.dumps(request, ensure_ascii=False, default=str)
            self.assertNotIn("99999999", serialized)
            self.assertNotIn("2299.30", serialized)
            self.assertNotIn("3101.12", serialized)
        for item in (seven, fourteen, comparison):
            self.assertNotIn("99999999", item["text"])

    def test_새_가격조회는_이전_규격_기본값을_이어받지_않음(self):
        from agents.common.price_history_followup import resolve_price_history_query
        selected = resolve_price_history_query({"follow_up": False, "variety": "극조생감귤", "interval_days": 3}, 조회조건)
        self.assertEqual(selected, {"variety": "극조생감귤", "interval_days": 3, "limit": 3})
        self.assertNotIn("grade", selected)
        self.assertNotIn("package_kg", selected)

    def test_검증되지_않은_가격쌍은_차단하되_자체_계산은_가능(self):
        with patch("agents.common.price_history_service.verify_integrated_price_sources",
                   return_value={"status": "DATA_ERROR", "reason": "VERSION_MISMATCH"}):
            history = get_integrated_price_history(조회조건)
        self.assertEqual(history["status"], "DATA_ERROR")
        self.assertNotIn("examples", history)
        ws = SharedWorkspace(AgentSession())
        self.assertEqual(ws.load_demo()["status"], "OK")
        calculated = ws.calculate()
        self.assertEqual(calculated["status"], "OK")
        self.assertEqual(calculated["options"][0]["minimum_whole_krw_per_kg"], "3109")

    def test_정현_신규_B안은_가정단가이지_예측값이_아님(self):
        result = get_price_scenarios(판매가정)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["clean_pairs"], 20)
        median = next(s for s in result["scenarios"] if s["basis"] == "median")
        self.assertEqual(median["option_b_price_per_kg"], "2409.37")
        self.assertTrue(all(s["price_type"] == "assumption" for s in result["scenarios"]))
        self.assertFalse(result["is_future_forecast"])

    def test_B안_변환값은_미입력_비용과_감모를_마음대로_채우지_않음(self):
        result = build_comparison_input(판매가정, delay_days=5)
        self.assertEqual(result["status"], "OK")
        options = result["comparison_input"]["options"]
        self.assertEqual(len(options), 3)
        self.assertEqual(result["comparison_input"]["current_price_per_kg"], "2500")
        median = next(option for option in options if option["id"] == "past_median_7d")
        self.assertEqual(median["future_price_per_kg"], "2409.37")
        self.assertEqual(median["delay_days"], 5)
        self.assertIsNone(median["loss_percent"])
        self.assertIsNone(median["daily_storage_cost"])
        self.assertEqual(result["evidence"][median["id"]]["price_interval_days"], 7)
        self.assertNotIn("loss_confirmed_for_period", median)

    def test_비지원_비교간격은_이전_B안_가격을_재사용하지_않음(self):
        first = build_comparison_input(판매가정, delay_days=5)
        unsupported = build_comparison_input({**판매가정, "option_b_sale_date": "2026-11-23"}, delay_days=5)
        self.assertEqual(first["status"], "OK")
        self.assertEqual(unsupported["status"], "INTERVAL_NOT_AVAILABLE")
        self.assertIsNone(unsupported["comparison_input"])
        self.assertTrue(unsupported["manual_price_required"])

    def test_과거가격과_계산이_보고서_PDF에서도_구분됨(self):
        from pypdf import PdfReader
        history = get_integrated_price_history(조회조건)
        store = ScenarioStore(demo_scenario())
        comparison = execute_tool("compare_shipment_options", {"expected_version": store.version}, store)
        self.assertEqual((history["status"], comparison["status"]), ("OK", "OK"))
        entries = [{"tool": "get_price_history", "result": history},
                   {"tool": "compare_shipment_options", "result": comparison}]
        report = render_report(entries)
        pdf = render_pdf_report(entries)
        extracted = " ".join(page.extract_text() for page in PdfReader(BytesIO(pdf)).pages)
        example = history["examples"][0]
        observed = f"{Decimal(example['target_price_kg']):,.2f}"
        for rendered in (report, extracted):
            self.assertIn(observed, rendered)
            self.assertIn("3,109", rendered)
            self.assertIn("과거", rendered)
        self.assertTrue(pdf.startswith(b"%PDF"))


if __name__ == "__main__":
    unittest.main()
