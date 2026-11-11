"""통합 Streamlit 화면의 가격 조회·손익 비교·보고서·초기화 전 과정을 검증합니다.

실제 저장된 자료 및 모의 모델 도구 호출만 사용하며 추가 유료 API 호출은 없습니다.
"""
from __future__ import annotations

import json
import os
import unittest
from copy import deepcopy
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from agents.juneok.config import Settings
from agents.juneok.reporting import render_report


ROOT = Path(__file__).resolve().parents[1]
비교조건 = {"variety": "조생귤", "grade": "특", "size": "소", "package": "상자",
            "package_kg": "5", "interval_days": 7, "limit": 3}


class 모의_대화_클라이언트:
    def __init__(self, name, arguments):
        self.responses = self
        self.name = name
        self.arguments = arguments
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(deepcopy(kwargs))
        call = SimpleNamespace(type="function_call", name=self.name,
                               arguments=json.dumps({**self.arguments, "finish_turn": True}, ensure_ascii=False),
                               call_id="화면_통합_후속_조회")
        return SimpleNamespace(output=[call], status="completed",
                               output_text="검증되지 않은 임의 가격 99999999원")


class 통합화면사용자흐름검수(unittest.TestCase):
    def _app(self, *, key=False):
        token = "TEST_ONLY_NOT_REAL_KEY" if key else ""
        with patch.dict(os.environ, {"OPENAI_API_KEY": token}):
            app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
        self.assertEqual(list(app.exception), [])
        return app

    def _button(self, app, label):
        return next(button for button in app.button if button.label == label)

    def _run(self, app, label, *, key=False):
        token = "TEST_ONLY_NOT_REAL_KEY" if key else ""
        with patch.dict(os.environ, {"OPENAI_API_KEY": token}):
            self._button(app, label).click().run()
        self.assertEqual(list(app.exception), [])
        return app

    def _fill(self, app, *, interval=7, **changes):
        fields = {"variety": "조생귤", "grade": "특", "size": "소", "package": "상자", "package_kg": "5"}
        fields.update(changes)
        for field, value in fields.items():
            next(widget for widget in app.text_input if widget.key == "price_history_" + field).set_value(value)
        next(widget for widget in app.number_input if widget.key == "price_history_interval_days").set_value(interval)

    def _pdf_buttons(self, app):
        return [button for button in app.get("download_button") if button.label == "PDF 보고서 내려받기"]

    def test_키가_없어도_경락가격과_손익계산_진입점이_있음(self):
        app = self._app()
        self.assertTrue(app.chat_input[0].disabled)
        for label in ("과거 경락가격 직접 조회", "입력 조건 비교", "과거 출하량 조회"):
            self.assertFalse(self._button(app, label).disabled)
        self.assertTrue(any(button.label == "과거 경락가격" for button in app.button))
        self.assertTrue(any("과거 경락가격 비교" in entry.label for entry in app.expander))

    def test_실제_가격조회와_미리보기_pdf가_같은_관측값을_표시(self):
        from pypdf import PdfReader
        app = self._app()
        self._fill(app)
        self._run(app, "과거 경락가격 직접 조회")
        ws = app.session_state.workspace
        entry = ws.current_entries()[0]
        self.assertEqual(entry["tool"], "get_price_history")
        self.assertEqual(entry["result"]["status"], "OK")
        self.assertEqual(entry["result"]["clean_pairs"], 20)
        self.assertIsNone(ws.store.scenario["current_price_per_kg"])
        self.assertEqual(len(self._pdf_buttons(app)), 1)
        report = render_report(ws.current_entries())
        pdf = app.session_state.pdf_cache[1]
        text = "".join(page.extract_text() for page in PdfReader(BytesIO(pdf)).pages)
        # 화면이 실제로 조회한 최신 사례의 관측가격을 기준으로 두 출력물의 일치를 검증합니다.
        sample = entry["result"]["examples"][0]
        display_price = f"{Decimal(sample['target_price_kg']):,.2f}"
        self.assertIn(display_price, report)
        self.assertIn(display_price, text)
        self.assertIn("과거", report)
        self.assertNotIn("미래 가격을 예측했습니다", report)

    def test_조회_간격_변경시_기존_보고서를_숨기고_재조회(self):
        app = self._app()
        self._fill(app)
        self._run(app, "과거 경락가격 직접 조회")
        self.assertEqual(app.session_state.workspace.current_entries()[0]["result"]["clean_pairs"], 20)
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            next(widget for widget in app.number_input
                 if widget.key == "price_history_interval_days").set_value(14).run()
        self.assertEqual(list(app.exception), [])
        self.assertEqual(app.session_state.workspace.current_entries(), [])
        self.assertFalse(self._pdf_buttons(app))
        self.assertTrue(any("이전 과거 가격 결과를 숨겼습니다" in info.value for info in app.info))
        self._run(app, "과거 경락가격 직접 조회")
        result = app.session_state.workspace.current_entries()[0]["result"]
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["interval_days"], 14)
        self.assertEqual(result["clean_pairs"], 15)
        self.assertEqual(len(self._pdf_buttons(app)), 1)

    def test_불완전한_규격은_이전값이나_영을_추측하지_않음(self):
        app = self._app()
        next(widget for widget in app.text_input if widget.key == "price_history_variety").set_value("조생귤")
        next(widget for widget in app.number_input if widget.key == "price_history_interval_days").set_value(7)
        self._run(app, "과거 경락가격 직접 조회")
        result = app.session_state.workspace.current_entries()[0]["result"]
        self.assertEqual(result["status"], "NEEDS_INPUT")
        self.assertIn("grade", result["missing_fields"])
        self.assertIn("package_kg", result["missing_fields"])
        self.assertNotIn("examples", result)
        self.assertNotIn("market_code", app.session_state.workspace.session.price_history_query)
        self.assertIsNone(app.session_state.workspace.store.scenario["current_price_per_kg"])

    def test_포장중량_충돌은_자동선택하지_않고_오류표시(self):
        app = self._app()
        self._fill(app, package="10kg 박스", package_kg="5")
        self._run(app, "과거 경락가격 직접 조회")
        result = app.session_state.workspace.current_entries()[0]["result"]
        self.assertEqual(result["status"], "INVALID_INPUT")
        self.assertIn("10kg", result["message"])
        self.assertIn("5kg", result["message"])
        self.assertNotIn("examples", result)
        self.assertIsNone(app.session_state.workspace.store.scenario["current_price_per_kg"])

    def test_과거가격_조회후_손익계산은_별개_결과로_교체됨(self):
        app = self._app()
        self._run(app, "가상 예시 조건 불러오기")
        before = deepcopy(app.session_state.workspace.store.snapshot())
        self._fill(app)
        self._run(app, "과거 경락가격 직접 조회")
        history = app.session_state.workspace.current_entries()[0]["result"]
        previous_price = f"{Decimal(history['examples'][0]['target_price_kg']):,.2f}"
        self.assertEqual(app.session_state.workspace.store.snapshot(), before)
        self._run(app, "출하 조건 비교")
        entries = app.session_state.workspace.current_entries()
        self.assertEqual([entry["tool"] for entry in entries], ["compare_shipment_options"])
        self.assertEqual(entries[0]["result"]["options"][0]["minimum_whole_krw_per_kg"], "3109")
        self.assertIsNone(app.session_state.workspace.store.scenario["options"][0]["future_price_per_kg"])
        report = render_report(entries)
        self.assertIn("3,109", report)
        self.assertNotIn(previous_price, report)
        self.assertEqual(len(self._pdf_buttons(app)), 1)

    def test_손익계산후_다시_과거가격_조회해도_계산값을_가져오지_않음(self):
        app = self._app()
        self._run(app, "가상 예시 조건 불러오기")
        self._run(app, "출하 조건 비교")
        before = deepcopy(app.session_state.workspace.store.snapshot())
        self._fill(app, interval=14)
        self._run(app, "과거 경락가격 직접 조회")
        entries = app.session_state.workspace.current_entries()
        self.assertEqual([entry["tool"] for entry in entries], ["get_price_history"])
        self.assertEqual(entries[0]["result"]["clean_pairs"], 15)
        self.assertEqual(app.session_state.workspace.store.snapshot(), before)
        self.assertNotIn("3,109원/kg", render_report(entries))

    def test_직접조회와_계산을_번갈아_실행해도_화면_pdf_json이_일치(self):
        import streamlit as st
        from pypdf import PdfReader
        from agents.juneok.casefiles import parse_case

        app = self._app()
        self._run(app, "가상 예시 조건 불러오기")
        self._fill(app)
        actions = [("과거 경락가격 직접 조회", "get_price_history"),
                   ("출하 조건 비교", "compare_shipment_options")] * 2
        with patch("streamlit.download_button", wraps=st.download_button) as download:
            for label, tool in actions:
                with self.subTest(action=label):
                    download.reset_mock()
                    self._run(app, label)
                    ws = app.session_state.workspace
                    entries = ws.current_entries()
                    self.assertEqual([entry["tool"] for entry in entries], [tool])
                    outputs = {call.args[0]: call.args[1] for call in download.call_args_list}
                    saved = parse_case(outputs["비교 JSON 내려받기"])
                    self.assertEqual(saved["archived_results"], entries)
                    self.assertEqual(saved["snapshot"], ws.store.snapshot())
                    self.assertTrue(any(item.value.strip() == render_report(entries).strip() for item in app.markdown))
                    pdf = outputs["PDF 보고서 내려받기"]
                    text = "\n".join(page.extract_text() for page in PdfReader(BytesIO(pdf)).pages)
                    if tool == "get_price_history":
                        price = Decimal(entries[0]["result"]["examples"][0]["target_price_kg"])
                        self.assertIn(f"{price:,.2f}", text)
                        self.assertNotIn("3,109원/kg", text)
                    else:
                        self.assertIn("3,109원/kg", text)

    def test_미저장_손익카드_수정중에는_가격조회와_pdf_차단(self):
        app = self._app()
        self._run(app, "가상 예시 조건 불러오기")
        self._fill(app)
        self._run(app, "과거 경락가격 직접 조회")
        self.assertEqual(len(self._pdf_buttons(app)), 1)
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            next(widget for widget in app.text_input if widget.key == "loss_0").set_value("4").run()
        self.assertEqual(list(app.exception), [])
        self.assertTrue(self._button(app, "과거 경락가격 직접 조회").disabled)
        self.assertFalse(self._pdf_buttons(app))
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            self._button(app, "입력 조건 비교").click().run()
        self.assertEqual(list(app.exception), [])
        self.assertFalse(self._button(app, "과거 경락가격 직접 조회").disabled)
        self.assertEqual(str(app.session_state.workspace.store.scenario["options"][0]["loss_percent"]), "4")
        self.assertIsNone(app.session_state.workspace.store.scenario["options"][0]["future_price_per_kg"])

    def test_원천검증_오류에서도_직전_가격을_보여주지_않음(self):
        app = self._app()
        self._fill(app)
        self._run(app, "과거 경락가격 직접 조회")
        previous = app.session_state.workspace.current_entries()[0]["result"]
        previous_price = f"{Decimal(previous['examples'][0]['target_price_kg']):,.2f}"
        with patch("agents.common.price_history_service.verify_integrated_price_sources",
                   return_value={"status": "DATA_ERROR", "reason": "VERSION_MISMATCH"}):
            self._run(app, "과거 경락가격 직접 조회")
        entries = app.session_state.workspace.current_entries()
        self.assertEqual(entries[0]["result"]["status"], "DATA_ERROR")
        self.assertNotIn("examples", entries[0]["result"])
        self.assertNotIn(previous_price, render_report(entries))
        self.assertIsNone(app.session_state.workspace.session.price_history_query)
        self.assertTrue(any("검증이 실패" in warning.value for warning in app.warning))

    def test_대화만_비우면_손익조건을_보존하고_가격조회기억은_삭제(self):
        app = self._app()
        self._run(app, "가상 예시 조건 불러오기")
        self._fill(app)
        self._run(app, "과거 경락가격 직접 조회")
        scenario = deepcopy(app.session_state.workspace.store.snapshot())
        self._run(app, "대화만 비우기 · 조건 유지")
        self.assertEqual(app.session_state.workspace.store.snapshot(), scenario)
        self.assertIsNone(app.session_state.workspace.session.price_history_query)
        self.assertEqual(app.session_state.workspace.current_entries(), [])
        self.assertFalse(self._pdf_buttons(app))
        self.assertEqual(app.session_state.price_history_variety, "조생귤")

    def test_새비교를_시작하면_손익조건과_가격조회_양쪽_초기화(self):
        app = self._app()
        self._run(app, "가상 예시 조건 불러오기")
        self._fill(app)
        self._run(app, "과거 경락가격 직접 조회")
        self._run(app, "새 비교 시작 · 조건도 초기화")
        self.assertIsNone(app.session_state.workspace.store.scenario["current_price_per_kg"])
        self.assertEqual(app.session_state.workspace.store.scenario["options"], [])
        self.assertIsNone(app.session_state.workspace.session.price_history_query)
        self.assertEqual(app.session_state.workspace.current_entries(), [])
        self.assertFalse(self._pdf_buttons(app))
        self.assertEqual(app.session_state.price_history_variety, "")
        self.assertEqual(app.session_state.price_history_grade, "")

    def test_직접조회_후_모의대화로_과거간격만_변경(self):
        app = self._app(key=True)
        self._fill(app)
        self._run(app, "과거 경락가격 직접 조회", key=True)
        ws = app.session_state.workspace
        before = deepcopy(ws.store.snapshot())
        client = 모의_대화_클라이언트("get_price_history", {"follow_up": True, "interval_days": 14})
        ws.session.client = client
        with patch.dict(os.environ, {"OPENAI_API_KEY": "TEST_ONLY_NOT_REAL_KEY"}):
            self.assertFalse(app.chat_input[0].disabled)
            app.chat_input[0].set_value("앞에서 선택한 규격 그대로 과거 14일로 바꿔줘").run()
        self.assertEqual(list(app.exception), [])
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(ws.session.price_history_query["interval_days"], 14)
        self.assertEqual(ws.current_entries()[0]["result"]["clean_pairs"], 15)
        self.assertEqual(ws.store.snapshot(), before)
        self.assertNotIn("99999999", render_report(ws.current_entries()))
        self.assertEqual(len(self._pdf_buttons(app)), 1)


if __name__ == "__main__":
    unittest.main()
