"""통합 서비스의 화면·대화·계산·보고·정산 실행 경로를 검증합니다.

유료 API를 호출하지 않고 모의 Responses 응답과 공통 Python 계산기를 사용합니다.
"""
from __future__ import annotations

import ast
import json
import os
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from agents.common.calculation_service import compare_shipment_options
from agents.juneok.agent import AgentSession
from agents.juneok.comparison import compare_shipment_options as 원래_계산
from agents.juneok.config import Settings
from agents.juneok.pdf_report import render_pdf_report
from agents.juneok.reporting import render_report
from agents.juneok.settlement import empty_values
from agents.juneok.workspace import SharedWorkspace


프로젝트_경로 = Path(__file__).resolve().parents[1]


def 도구_요청(이름, 인수, 번호=1):
    return SimpleNamespace(
        type="function_call",
        name=이름,
        arguments=json.dumps(인수, ensure_ascii=False),
        call_id=f"call_{번호}",
    )


def 모의_응답(*항목, 텍스트="모델이 만든 잘못된 숫자 99999999원"):
    return SimpleNamespace(output=list(항목), output_text=텍스트, status="completed")


class 모의_클라이언트:
    def __init__(self, 응답들):
        self.responses = self
        self._responses = iter(응답들)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(deepcopy(kwargs))
        return next(self._responses)


class 통합실행경로검수(TestCase):
    def setUp(self):
        self.세션 = AgentSession(Settings(model="mock-model"))
        self.작업공간 = SharedWorkspace(self.세션)

    def test_실서비스_계산_경로는_단일_진입점을_사용(self):
        # 기본 엔진을 직접 사용하는 코드는 공통 서비스 내부로 제한합니다.
        대상 = [
            "agents/juneok/tools.py", "agents/juneok/briefing.py",
            "agents/juneok/insights.py", "agents/juneok/sensitivity.py",
            "agents/juneok/limits.py", "agents/juneok/agent.py",
            "agents/juneok/settlement.py",
        ]
        for 상대경로 in 대상:
            with self.subTest(파일=상대경로):
                나무 = ast.parse((프로젝트_경로 / 상대경로).read_text(encoding="utf-8"))
                가져오기 = [
                    명령 for 명령 in ast.walk(나무)
                    if isinstance(명령, ast.ImportFrom)
                    and any(별칭.name == "compare_shipment_options" for 별칭 in 명령.names)
                ]
                self.assertTrue(가져오기, f"계산 호출 경로가 확인되지 않습니다: {상대경로}")
                self.assertTrue(all(
                    명령.level == 0 and 명령.module == "agents.common.calculation_service"
                    for 명령 in 가져오기
                ), f"공통 계산 서비스를 우회합니다: {상대경로}")

    def test_직접_비교와_도구_호출이_같은_계산기를_사용(self):
        from agents.juneok.tools import execute_tool

        with patch("agents.common.calculation_service._primary_calculate", wraps=원래_계산) as 추적:
            self.assertEqual(self.작업공간.load_demo()["status"], "OK")
            직접 = self.작업공간.current_entries()[0]["result"]
            함수호출 = execute_tool(
                "compare_shipment_options",
                {"expected_version": self.작업공간.store.version},
                self.작업공간.store,
            )
        self.assertEqual(추적.call_count, 2)
        self.assertEqual(직접["status"], "OK")
        self.assertEqual(함수호출["status"], "OK")
        self.assertEqual(
            직접["options"][0]["break_even_price_per_kg"],
            함수호출["options"][0]["break_even_price_per_kg"],
        )
        self.assertEqual(직접["options"][0]["minimum_whole_krw_per_kg"], "3109")

    def test_대화_도구_호출은_검증된_금액만_표시(self):
        self.작업공간.load_demo()
        버전 = self.작업공간.store.version
        클라이언트 = 모의_클라이언트([
            모의_응답(도구_요청(
                "compare_shipment_options",
                {"expected_version": 버전, "finish_turn": True},
            )),
        ])
        self.세션.client = 클라이언트

        with patch("agents.common.calculation_service._primary_calculate", wraps=원래_계산) as 추적:
            결과 = self.작업공간.run("7일 보관 대안의 손익분기를 계산해 주세요.")
        self.assertEqual(결과["status"], "OK")
        self.assertEqual(추적.call_count, 1)
        self.assertIn("3,109", 결과["text"])
        self.assertNotIn("99999999", 결과["text"])
        self.assertEqual(결과["visible_results"][0]["result"]["status"], "OK")
        self.assertEqual(self.작업공간.current_entries()[0]["result"]["options"][0]["minimum_whole_krw_per_kg"], "3109")

    def test_민감도_분석은_공통_계산기를_사용하고_기준값을_보존(self):
        self.작업공간.load_demo()
        이전 = deepcopy(self.작업공간.store.snapshot())
        with patch("agents.common.calculation_service._primary_calculate", wraps=원래_계산) as 추적:
            결과 = self.작업공간.sensitivity("seven_days", [2, 4], [3000, 3300])
        self.assertEqual(결과["status"], "OK")
        self.assertEqual(len(결과["rows"]), 4)
        self.assertEqual(추적.call_count, 4)
        self.assertEqual(self.작업공간.store.snapshot(), 이전)
        self.assertEqual(
            next(x for x in 결과["rows"] if x["loss_percent"] == "4" and x["future_price_per_kg"] == "3300")["minimum_whole_krw_per_kg"],
            "3174",
        )

    def test_역산과_종합_브리핑은_같은_계산기를_사용(self):
        self.작업공간.load_demo()
        이전 = deepcopy(self.작업공간.store.snapshot())
        with patch("agents.common.calculation_service._primary_calculate", wraps=원래_계산) as 추적:
            역산 = self.작업공간.offer_limits("seven_days", 3300, "loss_percent")
            횟수 = 추적.call_count
            브리핑 = self.작업공간.review()
        self.assertEqual(역산["status"], "OK")
        self.assertGreaterEqual(횟수, 1)
        self.assertEqual(브리핑["status"], "OK")
        self.assertEqual(브리핑["review_status"], "PARTIAL")
        self.assertGreater(추적.call_count, 횟수)
        self.assertEqual(
            브리핑["components"][0]["result"]["options"][0]["minimum_whole_krw_per_kg"],
            "3109",
        )
        self.assertEqual(self.작업공간.store.snapshot(), 이전)

    def test_보고서와_pdf가_확정된_계산결과를_공유(self):
        from pypdf import PdfReader

        self.작업공간.load_demo()
        목록 = self.작업공간.current_entries()
        원본 = deepcopy(목록)
        마크다운 = render_report(목록)
        피디에프 = render_pdf_report(목록)
        추출 = "".join(페이지.extract_text() for 페이지 in PdfReader(BytesIO(피디에프)).pages)

        self.assertIn("3,109", 마크다운)
        self.assertIn("3,109", 추출)
        self.assertTrue(피디에프.startswith(b"%PDF"))
        self.assertEqual(self.작업공간.current_entries(), 원본)

    def test_조건_버전이_달라지면_기존_결과가_숨겨짐(self):
        self.작업공간.load_demo()
        self.assertTrue(self.작업공간.current_entries())
        변경 = self.작업공간.store.update(
            {"quantity_kg": 4000}, self.작업공간.store.version,
        )
        self.assertEqual(변경["status"], "OK")
        self.assertEqual(self.작업공간.current_entries(), [])
        새결과 = self.작업공간.calculate()
        self.assertEqual(새결과["status"], "OK")
        self.assertNotEqual(새결과["current_proceeds_krw"], "9000000")

    def test_정산_연결시_독립_계산기_우회가_없음(self):
        self.작업공간.load_demo()
        보관파일 = self.작업공간.export_case("비교 조건")
        with patch("agents.common.calculation_service._primary_calculate", wraps=원래_계산) as 추적:
            self.작업공간.shipment.connect(보관파일, "seven_days")
        self.assertEqual(추적.call_count, 1)
        self.assertEqual(self.작업공간.shipment.revision, 0)

    def test_정산_상세금액은_모델에_전달되지_않음(self):
        self.작업공간.load_demo()
        보관파일 = self.작업공간.export_case("비교 조건")
        self.작업공간.shipment.connect(보관파일, "seven_days")

        입력값 = empty_values()
        입력값.update(
            input_kind="hypothetical",
            shipment_date="2026-11-09",
            sold_weight_kg="2940",
            gross_krw="9702000",
            statement_net_krw="9156900",
            received_krw="9156900",
            deductions_complete=True,
            external_complete=True,
            deductions=[
                {"id": "d1", "category": "commission", "amount_krw": "485100"},
                {"id": "d2", "category": "transport", "amount_krw": "60000"},
            ],
            external_costs=[
                {"id": "e1", "category": "storage", "amount_krw": "140000"},
            ],
        )
        self.작업공간.shipment.confirm(입력값, self.작업공간.shipment.revision)
        클라이언트 = 모의_클라이언트([
            모의_응답(도구_요청(
                "reconcile_settlement",
                {"expected_record_revision": 1, "finish_turn": False},
            )),
            모의_응답(도구_요청(
                "reconcile_settlement",
                {"expected_record_revision": 1, "finish_turn": True},
                번호=2,
            )),
        ])
        self.세션.client = 클라이언트
        결과 = self.작업공간.run("확인한 출하 정산 결과를 검산해 주세요.")

        self.assertEqual(결과["status"], "OK")
        self.assertIn("9,016,900.00", 결과["text"])
        self.assertEqual(len(클라이언트.requests), 2)
        for 요청 in 클라이언트.requests:
            전송내용 = json.dumps(요청, ensure_ascii=False, default=str)
            for 비공개값 in ("9702000", "9156900", "9016900", "485100", "gross_krw", "citations"):
                self.assertNotIn(비공개값, 전송내용)

    def test_화면_버튼도_공통_계산기를_사용(self):
        from streamlit.testing.v1 import AppTest

        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            with patch("agents.common.calculation_service._primary_calculate", wraps=원래_계산) as 추적:
                화면 = AppTest.from_file(str(프로젝트_경로 / "app.py"), default_timeout=25).run()
                next(버튼 for 버튼 in 화면.button if 버튼.label == "가상 예시 조건 불러오기").click().run()
                next(버튼 for 버튼 in 화면.button if 버튼.label == "입력 조건 비교").click().run()

        self.assertEqual(list(화면.exception), [])
        self.assertGreaterEqual(추적.call_count, 2)
        self.assertTrue(any("3,109원/kg" in str(x.value) for x in 화면.markdown))
        self.assertEqual(화면.session_state.workspace.current_entries()[0]["result"]["options"][0]["minimum_whole_krw_per_kg"], "3109")


if __name__ == "__main__":
    import unittest
    unittest.main()
