"""통합 화면의 키 없는 경락가격 조회와 손익 카드의 독립성을 검증합니다."""
from __future__ import annotations

import json
import os
import unittest
from copy import deepcopy
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agents.agent import AgentSession
from core.config import Settings
from ui.workspace import SharedWorkspace


ROOT = Path(__file__).resolve().parents[1]
조회 = {"variety": "조생귤", "grade": "특", "size": "소", "package": "상자",
        "package_kg": "5", "interval_days": 7, "limit": 3}


class 도구_클라이언트:
    def __init__(self, args):
        self.responses = self
        self.args = args
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(deepcopy(kwargs))
        call = SimpleNamespace(type="function_call", name="get_price_history",
                               arguments=json.dumps({**self.args, "finish_turn": True}, ensure_ascii=False),
                               call_id="후속_가격_조회")
        return SimpleNamespace(output=[call], status="completed", output_text="임의 가격 99999999원")


class 직접가격조회검수(unittest.TestCase):
    def test_실제_동결_데이터를_키없이_직접조회(self):
        ws = SharedWorkspace(AgentSession(Settings(api_key="", model="mock-model")))
        before = ws.store.snapshot()
        reply = ws.price_history(조회)
        self.assertEqual(reply["status"], "OK", reply.get("reason"))
        self.assertEqual(reply["clean_pairs"], 20)
        self.assertEqual(reply["interval_days"], 7)
        self.assertFalse(reply["is_future_forecast"])
        self.assertEqual(ws.store.snapshot(), before)
        self.assertEqual(ws.session.price_history_query, 조회)
        self.assertEqual(ws.session.messages, [])
        self.assertEqual(ws.current_entries()[0]["result"]["ui_context"]["kind"], "evidence")

    def test_빠진_조건은_자동으로_채우지_않음(self):
        ws = SharedWorkspace(AgentSession())
        result = ws.price_history({"variety": "조생귤", "interval_days": 7})
        self.assertEqual(result["status"], "NEEDS_INPUT")
        self.assertIn("grade", result["missing_fields"])
        self.assertIn("package_kg", result["missing_fields"])
        self.assertNotIn("grade", ws.session.price_history_query)
        self.assertNotIn("market_code", ws.session.price_history_query)

    def test_직접조회는_손익_단가와_보관조건을_변경하지_않음(self):
        ws = SharedWorkspace(AgentSession())
        self.assertEqual(ws.load_demo()["status"], "OK")
        before = ws.store.snapshot()
        result = ws.price_history(조회)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(ws.store.snapshot(), before)
        self.assertEqual(ws.store.scenario["options"][0]["delay_days"], 7)
        self.assertIsNone(ws.store.scenario["options"][0]["future_price_per_kg"])
        compare = ws.calculate()
        self.assertEqual(compare["status"], "OK")
        self.assertEqual(compare["options"][0]["minimum_whole_krw_per_kg"], "3109")

    def test_직접조회_이후_대화에서_간격만_변경(self):
        ws = SharedWorkspace(AgentSession(Settings(model="mock-model")))
        ws.price_history(조회)
        before = ws.store.snapshot()
        client = 도구_클라이언트({"follow_up": True, "interval_days": 14})
        ws.session.client = client
        reply = ws.run("다른 조건은 그대로, 과거 비교 간격만 14일로 바꿔줘")
        self.assertEqual(reply["status"], "OK")
        self.assertEqual(reply["visible_results"][0]["result"]["clean_pairs"], 15)
        self.assertEqual(ws.session.price_history_query["interval_days"], 14)
        self.assertEqual(ws.session.price_history_query["package_kg"], "5")
        self.assertEqual(ws.store.snapshot(), before)
        self.assertEqual(len(client.requests), 1)
        self.assertNotIn("99999999", reply["text"])

    def test_잘못된_새조회는_이전_조회조건을_되살리지_않음(self):
        ws = SharedWorkspace(AgentSession())
        self.assertEqual(ws.price_history(조회)["status"], "OK")
        invalid = ws.price_history({"variety": "조생귤", "interval_days": 0})
        self.assertEqual(invalid["status"], "INVALID_INPUT")
        self.assertIsNone(ws.session.price_history_query)
        self.assertIsNone(ws.session.price_history_revision)
        self.assertEqual(ws.current_entries()[0]["result"]["status"], "INVALID_INPUT")

    def test_가격원천_검증실패시_과거가격과_이전조건을_폐기(self):
        ws = SharedWorkspace(AgentSession())
        self.assertEqual(ws.price_history(조회)["status"], "OK")
        with patch("services.price_history_service.verify_integrated_price_sources",
                   return_value={"status": "DATA_ERROR", "reason": "VERSION_MISMATCH"}):
            result = ws.price_history(조회)
        self.assertEqual(result["status"], "DATA_ERROR")
        self.assertNotIn("examples", result)
        self.assertIsNone(ws.session.price_history_query)
        self.assertEqual(ws.current_entries()[0]["result"]["status"], "DATA_ERROR")

    def test_조건변경과_대화초기화는_출처를_혼동하지_않음(self):
        ws = SharedWorkspace(AgentSession())
        ws.price_history(조회)
        ws.clear_conversation(preserve_conditions=True)
        self.assertIsNone(ws.session.price_history_query)
        self.assertFalse(ws.current_entries())


class 직접가격조회화면검수(unittest.TestCase):
    def _app(self):
        from streamlit.testing.v1 import AppTest
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
        self.assertEqual(list(app.exception), [])
        return app

    def _fill(self, app):
        fields = {"market_code": "110001", "variety": "조생귤", "grade": "특", "size": "소", "package": "상자", "package_kg": "5"}
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            for field, value in fields.items():
                app.selectbox(key="price_history_spec_" + field).select(value).run()
            app.selectbox(key="price_history_interval_days").select(7).run()

    def test_키없는_화면에_과거_경락가격_조회가_있음(self):
        app = self._app()
        self.assertTrue(app.text_input(key="shipment_question").disabled)
        self.assertTrue(any(button.label == "과거 경락가격" for button in app.button))
        self.assertTrue(any(button.label == "과거 경락가격 조회" and button.disabled for button in app.button))

    def test_화면에서_조회후_보고서_내려받기_가능(self):
        app = self._app()
        self._fill(app)
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            next(button for button in app.button if button.label == "과거 경락가격 조회").click().run()
        self.assertEqual(list(app.exception), [])
        result = app.session_state.workspace.current_entries()[0]["result"]
        self.assertEqual(result["clean_pairs"], 20)
        self.assertTrue(any("과거 경락가격" in str(x.value) for x in app.markdown))
        self.assertTrue(any(button.label == "PDF 보고서 내려받기" for button in app.get("download_button")))
        self.assertIsNone(app.session_state.workspace.store.scenario["current_price_per_kg"])

    def test_관측이_없는_기준일에는_하나의_행동과_조회기록을_표시(self):
        app = self._app()
        self._fill(app)
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            app.date_input(key="price_history_base_date_picker").set_value(date(2026, 10, 4)).run()
            next(button for button in app.button if button.label == "과거 경락가격 조회").click().run()
        self.assertEqual(list(app.exception), [])
        gap = app.session_state.workspace.current_entries()[0]["result"]["data_gap"]
        self.assertEqual(gap["code"], "NO_OBSERVATION")
        screen = "\n".join(str(item.value) for item in app.markdown)
        self.assertIn(gap["reason"], screen)
        self.assertEqual(screen.count(gap["next_action"]), 1)
        labels = {button.label for button in app.get("download_button")}
        self.assertTrue({"가격 조회 기록 PDF 저장", "가격 조회 기록 JSON 저장"} <= labels)
        self.assertNotIn("PDF 보고서 내려받기", labels)

    def test_화면_조회필터를_수정하면_이전_보고서를_숨김(self):
        app = self._app()
        self._fill(app)
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            next(button for button in app.button if button.label == "과거 경락가격 조회").click().run()
            self.assertTrue(app.session_state.workspace.current_entries())
            app.selectbox(key="price_history_spec_grade").select(None).run()
        self.assertEqual(list(app.exception), [])
        self.assertEqual(app.session_state.workspace.current_entries(), [])
        self.assertFalse(any(button.label == "PDF 보고서 내려받기" for button in app.get("download_button")))
        self.assertTrue(any("이전 과거 가격 결과를 숨겼습니다" in x.value for x in app.info))


if __name__ == "__main__":
    unittest.main()
