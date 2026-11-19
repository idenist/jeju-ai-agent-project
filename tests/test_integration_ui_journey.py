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

from core.config import Settings
from presenters.reporting import render_report


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
        fields = {"market_code": "110001", "variety": "조생귤", "grade": "특", "size": "소", "package": "상자", "package_kg": "5"}
        fields.update(changes)
        token = app.session_state.workspace.session.settings.api_key
        with patch.dict(os.environ, {"OPENAI_API_KEY": token}):
            for field, value in fields.items():
                app.selectbox(key="price_history_spec_" + field).select(value).run()
                self.assertFalse(app.exception)
            app.selectbox(key="price_history_interval_days").select(interval).run()

    def _pdf_buttons(self, app):
        return [button for button in app.get("download_button") if button.label in
                {"PDF 보고서 내려받기", "출하 비교 PDF 저장"}]

    def test_candidate_guided_intake_keeps_skipped_loss_unknown_in_the_report(self):
        app = AppTest.from_string('''
from datetime import date
from streamlit import session_state
from agents.shipment_candidates import ShipmentCandidateDraft
from ui.shipment_candidates import render_shipment_candidates
if "draft" not in session_state:
    session_state.draft = ShipmentCandidateDraft(
        tuple(date(2026, 11, day) for day in (23, 28, 30)), None, set(), 0, 0, date(2026, 11, 19),
        common={"start": date(2026, 11, 20), "quantity": 3000},
    )
render_shipment_candidates(session_state.draft)
''').run()
        labels = [widget.label for widget in app.number_input]
        self.assertEqual(labels, ["전체 비교 물량(kg)", *(f"11월 {day}일 단가(원/kg)" for day in (23, 28, 30))])
        self.assertFalse(list(app.selectbox) or list(app.date_input))
        self._run(app, "후보일 비교 시작")
        self.assertTrue(any("한 곳" in item.value for item in app.info))
        self._run(app, "견적 다시 입력")
        self.assertEqual(app.number_input[0].value, 3000)
        app.number_input[1].set_value(2500)
        app.number_input[3].set_value(2800)
        self._run(app, "후보일 비교 시작")
        self.assertEqual([widget.label for widget in app.selectbox], ["단가의 수수료 기준"])
        self.assertFalse(list(app.number_input))
        app.selectbox[0].select("수수료 차감 후")
        self._run(app, "확인하고 계속")
        for label, value in (("공통 하루 보관비(원)", 10000), ("공통 별도 비용(원)", 5000),
                             ("11월 23일 기간 전체 감모율(%)", 1), ("11월 28일 기간 전체 감모율(%)", 3)):
            self.assertEqual([widget.label for widget in app.number_input], [label])
            app.number_input[0].set_value(value)
            self._run(app, "확인하고 계속")
        self.assertEqual(app.number_input[0].label, "11월 30일 기간 전체 감모율(%)")
        self._run(app, "모르겠어요")
        result = app.session_state.draft.result
        self.assertEqual(Decimal(result["reference_proceeds_krw"]), 7390000)
        self.assertEqual(result["rows"][1]["minimum_whole_krw_per_kg"], "2569")
        self.assertIsNone(result["rows"][2]["loss_percent"])
        self.assertIsNone(result["rows"][2]["proceeds_krw"])
        self.assertEqual([widget.label for widget in app.get("download_button")],
                         ["후보일 비교 PDF 저장", "후보일 비교 JSON 저장"])

    def test_hub_855_chat_shows_the_integrated_card_without_a_second_pdf(self):
        app = self._app(key=True)
        client = 모의_대화_클라이언트("analyze_shipment_timing", {
            "shipment_variety": "노지온주", "option_a_date": "2026-11-18", "option_b_date": "2026-11-25",
        })
        app.session_state.workspace.session.client = client
        app.text_input(key="shipment_question").set_value(
            "#855 자료로 노지온주 11월 18일과 11월 25일 출하 시기를 비교해 주세요."
        )
        self._run(app, "보내기", key=True)
        self.assertEqual(client.requests[0]["tool_choice"], "auto")
        labels = [button.label for button in app.get("download_button")]
        self.assertEqual(labels.count("과거 출하량 PDF 저장"), 1)
        self.assertIn("과거 출하량 JSON 저장", labels)
        self.assertNotIn("PDF 보고서 내려받기", labels)

    def test_pending_price_survives_term_help_and_the_same_dates_feed_hub_855(self):
        from test_integration_future_pair import COMMON, FILTERS, OPTION_B

        app = self._app(key=True)
        ws = app.session_state.workspace
        client = 모의_대화_클라이언트("compare_future_pair", {
            "action": "new", "expected_version": 0,
            "patch": {**COMMON, "start_date": "2026-11-15", "option_b": {**OPTION_B, "delay_days": 9},
                      "price_mode": "history", "history_filters": FILTERS},
        })
        ws.session.client = client
        app.text_input(key="shipment_question").set_value("두 미래 출하안 손익 비교해 주세요.")
        self._run(app, "보내기", key=True)
        manual_key = f"future_pair_chat_manual_{ws.session.future_pair.version}"
        self.assertEqual(app.number_input(key=manual_key).label, "나중 출하의 가정 단가 (원/kg)")
        pending = ws.session.future_pair.pending
        self.assertTrue(pending["manual_price_required"])
        self.assertEqual(sum(widget.key == manual_key for widget in app.number_input), 1)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "TEST_ONLY_NOT_REAL_KEY"}):
            app.number_input(key=manual_key).set_value(2750).run()
        client.name, client.arguments = "explain_term", {"topic": "loss_rate"}
        app.text_input(key="shipment_question").set_value("감모율이 뭐예요?")
        self._run(app, "보내기", key=True)
        self.assertEqual(app.number_input(key=manual_key).value, 2750)
        self._run(app, "이 단가로 비교", key=True)
        self.assertFalse(any(w.key == manual_key for w in app.number_input))

        pair = ws.session.future_pair
        economic = pair.current_result()
        self.assertEqual(economic["options_b"][0]["sale_date"], "2026-11-24")
        self.assertIn("같은 날짜의 과거 출하량 보기", [x.label for x in app.expander])
        variety_box = app.selectbox(key=f"chat_timing_variety_{pair.version}")
        self.assertIsNone(variety_box.value)
        variety_box.select("노지온주")
        self._run(app, "과거 출하량 조회", key=True)
        self.assertEqual(len(client.requests), 2)
        self.assertEqual(pair.current_result(), economic)
        messages = app.session_state.agent_messages
        report = json.loads(messages[-1]["reply"]["chat_report"]["json"])
        self.assertEqual(report["status"], "INTEGRATED")
        self.assertEqual(report["shipment_analysis"]["input"]["option_a_date"], "2026-11-18")
        self.assertEqual(report["shipment_analysis"]["input"]["option_b_date"], "2026-11-24")
        self.assertIn("출하 시기·경제성 통합 PDF 저장", [x.label for x in app.get("download_button")])

    def test_chat_box_price_actions_requote_before_applying(self):
        from decimal import Decimal
        from test_integration_future_pair import COMMON, FILTERS, OPTION_B

        app = self._app(key=True)
        ws = app.session_state.workspace
        client = 모의_대화_클라이언트("compare_future_pair", {
            "action": "new", "expected_version": 0,
            "patch": {**COMMON, "option_b": OPTION_B, "price_mode": "history", "history_filters": FILTERS},
            "box_price": {"side": "A", "phrase": "상자당 12,500원"},
        })
        ws.session.client = client
        app.text_input(key="shipment_question").set_value(
            "감귤 3톤, 사흘 뒤와 열흘 뒤 출하 손익을 비교해 주세요. A안 상자당 12,500원, 상자는 5kg이에요. B안은 과거 경락가격을 참고해 주세요."
        )
        self._run(app, "보내기", key=True)
        pair = ws.session.future_pair
        next(x for x in app.number_input if x.label == "상자 하나의 감귤 무게 (kg)").set_value(10.0)
        self._run(app, "이 무게로 다시 환산", key=True)
        self.assertIsNone(pair.inputs["price_a"])
        self.assertTrue(any("1,250원/kg" in item.value for item in app.markdown))
        self.assertTrue(any("1,250원/kg 적용 여부" in item.value for item in app.caption))
        self._run(app, "A안에 1,250원/kg 적용", key=True)
        self.assertEqual(Decimal(pair.inputs["price_a"]), 1250)
        self.assertEqual(pair.inputs["history_filters"]["package_kg"], "5")
        self.assertEqual(len(client.requests), 1)
        self.assertFalse(any(x.label == "A안에 1,250원/kg 적용" for x in app.button))
        self.assertFalse(any("1,250원/kg 적용 여부" in item.value for item in app.caption))

    def test_키가_없어도_경락가격과_손익계산_진입점이_있음(self):
        app = self._app()
        self.assertTrue(app.text_input(key="shipment_question").disabled)
        self.assertTrue(self._button(app, "과거 경락가격 조회").disabled)
        for label in ("수정한 조건 저장 후 비교", "과거 출하량 조회"):
            self.assertFalse(self._button(app, label).disabled)
        self.assertTrue(any(button.label == "과거 경락가격" for button in app.button))
        self.assertTrue(any("과거 경락가격 비교" in entry.label for entry in app.expander))

    def test_실제_가격조회와_미리보기_pdf가_같은_관측값을_표시(self):
        from pypdf import PdfReader
        app = self._app()
        self._fill(app)
        self._run(app, "과거 경락가격 조회")
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
        self._run(app, "과거 경락가격 조회")
        self.assertEqual(app.session_state.workspace.current_entries()[0]["result"]["clean_pairs"], 20)
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            app.selectbox(key="price_history_interval_days").select(14).run()
        self.assertEqual(list(app.exception), [])
        self.assertEqual(app.session_state.workspace.current_entries(), [])
        self.assertFalse(self._pdf_buttons(app))
        self.assertTrue(any("이전 과거 가격 결과를 숨겼습니다" in info.value for info in app.info))
        self._run(app, "과거 경락가격 조회")
        result = app.session_state.workspace.current_entries()[0]["result"]
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["interval_days"], 14)
        self.assertEqual(result["clean_pairs"], 15)
        self.assertEqual(len(self._pdf_buttons(app)), 1)

    def test_불완전한_규격은_조회버튼을_비활성화(self):
        app = self._app()
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            app.selectbox(key="price_history_spec_market_code").select("110001").run()
            app.selectbox(key="price_history_spec_variety").select("조생귤").run()
        self.assertTrue(self._button(app, "과거 경락가격 조회").disabled)
        self.assertIsNone(app.selectbox(key="price_history_spec_grade").value)
        self.assertIsNone(app.selectbox(key="price_history_spec_package_kg").value)
        self.assertEqual(app.session_state.workspace.current_entries(), [])
        self.assertIsNone(app.session_state.workspace.store.scenario["current_price_per_kg"])

    def test_포장중량을_포장명에_섞어_입력할_수_없음(self):
        app = self._app()
        self._fill(app)
        self.assertNotIn("10kg 박스", app.selectbox(key="price_history_spec_package").options)
        self.assertEqual(app.selectbox(key="price_history_spec_package").value, "상자")
        self.assertEqual(app.selectbox(key="price_history_spec_package_kg").value, "5")
        self._run(app, "과거 경락가격 조회")
        self.assertEqual(app.session_state.workspace.current_entries()[0]["result"]["status"], "OK")

    def test_과거가격_조회후_손익계산은_별개_결과로_교체됨(self):
        app = self._app()
        self._run(app, "가상 예시 조건 불러오기")
        before = deepcopy(app.session_state.workspace.store.snapshot())
        self._fill(app)
        self._run(app, "과거 경락가격 조회")
        history = app.session_state.workspace.current_entries()[0]["result"]
        previous_price = f"{Decimal(history['examples'][0]['target_price_kg']):,.2f}"
        self.assertEqual(app.session_state.workspace.store.snapshot(), before)
        self._run(app, "현재 조건으로 다시 계산")
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
        self._run(app, "현재 조건으로 다시 계산")
        before = deepcopy(app.session_state.workspace.store.snapshot())
        self._fill(app, interval=14)
        self._run(app, "과거 경락가격 조회")
        entries = app.session_state.workspace.current_entries()
        self.assertEqual([entry["tool"] for entry in entries], ["get_price_history"])
        self.assertEqual(entries[0]["result"]["clean_pairs"], 15)
        self.assertEqual(app.session_state.workspace.store.snapshot(), before)
        self.assertNotIn("3,109원/kg", render_report(entries))

    def test_직접조회와_계산을_번갈아_실행해도_화면_pdf_json이_일치(self):
        import streamlit as st
        from pypdf import PdfReader
        from services.casefiles import parse_case

        app = self._app()
        self._run(app, "가상 예시 조건 불러오기")
        self._fill(app)
        actions = [("과거 경락가격 조회", "get_price_history"),
                   ("현재 조건으로 다시 계산", "compare_shipment_options")] * 2
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
                    pdf_label = "PDF 보고서 내려받기" if tool == "get_price_history" else "출하 비교 PDF 저장"
                    pdf = outputs[pdf_label]
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
        self._run(app, "과거 경락가격 조회")
        self.assertEqual(len(self._pdf_buttons(app)), 1)
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            next(widget for widget in app.text_input if widget.key == "loss_0").set_value("4").run()
        self.assertEqual(list(app.exception), [])
        self.assertTrue(self._button(app, "과거 경락가격 조회").disabled)
        self.assertFalse(self._pdf_buttons(app))
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            self._button(app, "수정한 조건 저장 후 비교").click().run()
        self.assertEqual(list(app.exception), [])
        self.assertFalse(self._button(app, "과거 경락가격 조회").disabled)
        self.assertEqual(str(app.session_state.workspace.store.scenario["options"][0]["loss_percent"]), "4")
        self.assertIsNone(app.session_state.workspace.store.scenario["options"][0]["future_price_per_kg"])

    def test_원천검증_오류에서도_직전_가격을_보여주지_않음(self):
        app = self._app()
        self._fill(app)
        self._run(app, "과거 경락가격 조회")
        previous = app.session_state.workspace.current_entries()[0]["result"]
        previous_price = f"{Decimal(previous['examples'][0]['target_price_kg']):,.2f}"
        with patch("services.price_history_service.verify_integrated_price_sources",
                   return_value={"status": "DATA_ERROR", "reason": "VERSION_MISMATCH"}):
            self._run(app, "과거 경락가격 조회")
        entries = app.session_state.workspace.current_entries()
        self.assertEqual(entries[0]["result"]["status"], "DATA_ERROR")
        self.assertNotIn("examples", entries[0]["result"])
        self.assertNotIn(previous_price, render_report(entries))
        self.assertIsNone(app.session_state.workspace.session.price_history_query)
        gap = entries[0]["result"]["data_gap"]
        self.assertEqual(gap["code"], "FILE_UNVERIFIED")
        self.assertTrue(any(gap["reason"] in item.value for item in app.markdown))

    def test_대화만_비우면_손익조건을_보존하고_가격조회기억은_삭제(self):
        app = self._app()
        self._run(app, "가상 예시 조건 불러오기")
        self._fill(app)
        self._run(app, "과거 경락가격 조회")
        scenario = deepcopy(app.session_state.workspace.store.snapshot())
        self._run(app, "대화만 비우기 · 조건 유지")
        self.assertEqual(app.session_state.workspace.store.snapshot(), scenario)
        self.assertIsNone(app.session_state.workspace.session.price_history_query)
        self.assertEqual(app.session_state.workspace.current_entries(), [])
        self.assertFalse(self._pdf_buttons(app))
        self.assertEqual(app.session_state.price_history_spec_variety, "조생귤")

    def test_새비교를_시작하면_손익조건과_가격조회_양쪽_초기화(self):
        app = self._app()
        self._run(app, "가상 예시 조건 불러오기")
        self._fill(app)
        self._run(app, "과거 경락가격 조회")
        self._run(app, "새 비교 시작 · 조건도 초기화")
        self.assertIsNone(app.session_state.workspace.store.scenario["current_price_per_kg"])
        self.assertEqual(app.session_state.workspace.store.scenario["options"], [])
        self.assertIsNone(app.session_state.workspace.session.price_history_query)
        self.assertEqual(app.session_state.workspace.current_entries(), [])
        self.assertFalse(self._pdf_buttons(app))
        self.assertIsNone(app.selectbox(key="price_history_spec_variety").value)
        self.assertIsNone(app.selectbox(key="price_history_spec_grade").value)

    def test_직접조회_후_모의대화로_과거간격만_변경(self):
        app = self._app(key=True)
        self._fill(app)
        self._run(app, "과거 경락가격 조회", key=True)
        ws = app.session_state.workspace
        before = deepcopy(ws.store.snapshot())
        client = 모의_대화_클라이언트("get_price_history", {"follow_up": True, "interval_days": 14})
        ws.session.client = client
        with patch.dict(os.environ, {"OPENAI_API_KEY": "TEST_ONLY_NOT_REAL_KEY"}):
            self.assertFalse(app.text_input(key="shipment_question").disabled)
            app.text_input(key="shipment_question").set_value("앞에서 선택한 규격 그대로 과거 14일로 바꿔줘")
            app.button(key="shipment_send").click().run()
        self.assertEqual(list(app.exception), [])
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(ws.session.price_history_query["interval_days"], 14)
        self.assertEqual(ws.current_entries()[0]["result"]["clean_pairs"], 15)
        self.assertEqual(ws.store.snapshot(), before)
        self.assertNotIn("99999999", render_report(ws.current_entries()))
        self.assertEqual(len(self._pdf_buttons(app)), 1)


if __name__ == "__main__":
    unittest.main()
